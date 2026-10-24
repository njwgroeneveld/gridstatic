import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

import yaml
from fastapi import FastAPI, Request
from prometheus_client import make_asgi_app

from shared.connector_client import ConnectorClient
from shared.alerter_client import AlerterClient
from src.strategy import StaticGrid, dex_of

log = logging.getLogger("grid-static")
_grids: list[StaticGrid] = []


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
    log.info(f"Started {len(_grids)} grid(s)")
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
