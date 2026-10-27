import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app

from shared.connector_client import ConnectorClient
from shared.alerter_client import AlerterClient
from src.strategy import StaticGrid, cancel_own_orders, describe, dex_of, stop_message

log = logging.getLogger("grid-static")
_grids: list[StaticGrid] = []
# Grids in the settings with active: false, as (label, coin). They run no rounds,
# but their orders may still rest on the exchange -- placed before they were
# switched off -- so /stop cancels those too.
_switched_off: list[tuple[str, str]] = []
_clients: dict = {}


class ConfigError(ValueError):
    """A configuration the bot refuses to trade with."""


def _load_config() -> dict:
    path = os.getenv("SETTINGS_FILE", "config/settings.yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _validate(cfg: dict) -> tuple[list[tuple[str, dict]], list[str]]:
    """The grids to run and the entries skipped.

    Raises ConfigError for anything that must not trade: refusing to start is
    loud and costs nothing, trading on a wrong assumption costs money."""
    errors, grids, skipped = [], [], []
    owner_of: dict[str, str] = {}
    share_of_dex: dict[str, float] = {}

    for key, c in (cfg.get("coins") or {}).items():
        if not c.get("active"):
            continue
        strategy_type = c.get("strategy_type", "STATIC")
        if strategy_type != "STATIC":
            # This image runs the static grid only. Skip loudly: crashing would
            # take the other grids down, skipping silently would leave you
            # believing a grid runs that doesn't.
            skipped.append(f"{key}: strategy_type={strategy_type} is not supported "
                           f"by grid-static; grid not started")
            continue
        coin = c["coin"]
        if coin in owner_of:
            errors.append(f"{key} and {owner_of[coin]} both trade {coin}. One grid per coin: "
                          f"the exchange keeps one position per coin per account, and two "
                          f"grids cannot tell theirs apart.")
        owner_of.setdefault(coin, key)
        lev = c.get("leverage", 1)
        if not isinstance(lev, (int, float)) or isinstance(lev, bool) or lev < 1:
            errors.append(f"{key}: leverage {lev!r} must be a number of at least 1 "
                          f"(1 = no leverage). Leave it out for 1x.")
            continue
        stop = c.get("stop_loss")
        if stop is not None:
            if (not isinstance(stop, (int, float)) or isinstance(stop, bool)
                    or not 0 < stop < c["lower"]):
                errors.append(f"{key}: stop_loss {stop!r} must be a price between 0 and the "
                              f"grid's lower bound {c['lower']}. Above it, the stop would close "
                              f"positions the grid is meant to hold.")
        cap = c.get("max_equity_usd")
        if cap is not None and (not isinstance(cap, (int, float)) or isinstance(cap, bool)
                                or cap <= 0):
            errors.append(f"{key}: max_equity_usd {cap!r} must be a number above 0 -- the most "
                          f"realised equity the grid sizes from -- or left out for no limit.")
        pct = c.get("allocation_pct")
        if not isinstance(pct, (int, float)) or isinstance(pct, bool) or not 0 < pct <= 100:
            errors.append(f"{key}: allocation_pct {pct!r} must be a number above 0 and at most "
                          f"100: the share of its dex's realised equity this grid may use.")
            continue
        dex = dex_of(coin)
        share_of_dex[dex] = share_of_dex.get(dex, 0.0) + pct
        grids.append((key, c))

    for dex, total in share_of_dex.items():
        if total > 100:
            name = f"dex '{dex}'" if dex else "the default dex"
            errors.append(f"the grids on {name} use {total:g}% of its equity together; at most "
                          f"100. Lower their allocation_pct.")

    if errors:
        raise ConfigError("\n".join(errors))
    return grids, skipped


async def _clear_switched_off(connector, alerter, attempts: int = 5, delay: float = 30) -> None:
    """At startup: cancel the buys and sells a grid left behind when it was switched
    off. Its stop stays while there is a position. Says something only when there
    was something to cancel, and keeps trying a few times when the exchange does
    not answer -- in the background, so the running grids do not wait for it."""
    active = {g.coin for g in _grids}
    todo = [(k, c) for k, c in _switched_off if c not in active]
    for attempt in range(attempts):
        left = []
        for key, coin in todo:
            try:
                r = await cancel_own_orders(connector, coin)
            except Exception as e:
                log.warning(f"[{key}] switched off; its orders could not be read yet: {describe(e)}")
                left.append((key, coin))
                continue
            if r["failed"]:
                left.append((key, coin))
            if r["cancelled"] or r["failed"] or r["kept_stop"]:
                msg = stop_message(f"{key} is switched off and", r).replace(
                    " stopped for uninstall:", "")
                (log.error if r["failed"] else log.warning)(f"[{key}] {msg}")
                await alerter.send_alert("error", {"coin": coin, "message": msg})
        if not left:
            return
        todo = left
        await asyncio.sleep(delay)
    for key, coin in todo:
        msg = (f"{key} is switched off, but its orders on {coin} could not be cancelled -- "
               f"cancel them on the exchange")
        log.error(f"[{key}] {msg}")
        await alerter.send_alert("error", {"coin": coin, "message": msg})


async def _wait_for(name: str, probe, attempts: int = 30, delay: float = 2.0) -> None:
    """Wait until `probe()` returns without raising, or give up loudly."""
    for attempt in range(attempts):
        try:
            await probe()
            return
        except Exception:
            if attempt == 0:
                log.info(f"Waiting for {name}...")
            await asyncio.sleep(delay)
    raise RuntimeError(f"{name} not reachable after {int(attempts * delay)}s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    cfg = _load_config()
    try:
        grids, skipped = _validate(cfg)
    except ConfigError as e:
        log.error(f"Refusing to start:\n{e}")
        raise

    connector = ConnectorClient(cfg["connector_url"])
    alerter = AlerterClient(cfg["alerter_url"])
    _clients.update(connector=connector, alerter=alerter)
    _switched_off[:] = [(key, c["coin"]) for key, c in (cfg.get("coins") or {}).items()
                        if not c.get("active") and c.get("coin")]
    for msg in skipped:
        log.error(msg)
        await alerter.send_alert("error", {"coin": msg.split(":")[0], "message": msg})

    interval = cfg.get("round_interval_seconds", cfg.get("fills_poll_interval_seconds", 30))
    lookback = cfg.get("fill_lookback_hours", 72)
    for key, c in grids:
        _grids.append(StaticGrid(key, c, connector, alerter, interval=interval,
                                 lookback_hours=lookback))

    await _wait_for("connector", connector.get_mids)
    tasks = [asyncio.create_task(g.run_loop()) for g in _grids]
    tasks.append(asyncio.create_task(_clear_switched_off(connector, alerter)))
    log.info(f"Started {len(_grids)} grid(s)")
    if not _grids:
        log.warning("No active grid: nothing trades. Set active: true on a grid in your "
                    "values file and run helm upgrade.")
    yield

    for t in tasks:
        t.cancel()


app = FastAPI(title="grid-static", lifespan=lifespan)
app.mount("/metrics", make_asgi_app())


@app.middleware("http")
async def log_requests(request: Request, call_next):
    t0 = time.time()
    response = await call_next(request)
    ms = (time.time() - t0) * 1000
    log.info(f'"{request.method} {request.url.path}" {response.status_code} {ms:.0f}ms')
    return response


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "grids": len(_grids)}


@app.get("/status")
def status() -> dict:
    """What each grid saw in its last round. Reads nothing from the exchange."""
    return {"grids": [g.status() for g in _grids]}


@app.post("/stop")
async def stop() -> JSONResponse:
    """Stop every grid for good and cancel its orders; helm uninstall calls this
    from a pre-delete hook. Switched-off grids too: their orders may still rest
    on the exchange. 500 when a book could not be read or a cancel failed: the
    uninstall then stops too, instead of leaving orders behind unnoticed.
    Calling it again retries what is left."""
    results, ok = [], True
    for g in _grids:
        try:
            r = await g.shutdown()
        except Exception as e:
            r = {"coin_key": g.coin_key, "coin": g.coin, "error": describe(e)}
        ok = ok and not (r.get("error") or r.get("failed"))
        results.append(r)

    # A running grid already cleared its coin; one coin is handled once.
    done = {g.coin for g in _grids}
    for key, coin in _switched_off:
        if coin in done:
            continue
        done.add(coin)
        label = f"{key} (switched off)"
        try:
            r = {"coin_key": key, "coin": coin, "switched_off": True,
                 **await cancel_own_orders(_clients["connector"], coin)}
            msg = stop_message(label, r)
            (log.error if r["failed"] else log.warning)(f"[{key}] {msg}")
            await _clients["alerter"].send_alert("error", {"coin": coin, "message": msg})
        except Exception as e:
            r = {"coin_key": key, "coin": coin, "switched_off": True, "error": describe(e)}
        ok = ok and not (r.get("error") or r.get("failed"))
        results.append(r)
    return JSONResponse({"stopped": ok, "grids": results}, status_code=200 if ok else 500)
