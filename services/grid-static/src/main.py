import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

import yaml
from fastapi import FastAPI, Request
from prometheus_client import make_asgi_app

from shared.dal_client import DALClient
from shared.connector_client import ConnectorClient
from shared.alerter_client import AlerterClient
from src.strategy import StaticGrid
from src.grid_math import calculate_levels

log = logging.getLogger("grid-static")
_grids: list[StaticGrid] = []


def _load_config() -> dict:
    path = os.getenv("SETTINGS_FILE", "config/settings.yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


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
    dal = DALClient(cfg["dal_url"])
    connector = ConnectorClient(cfg["connector_url"])
    alerter = AlerterClient(cfg["alerter_url"])
    strategy_pct = cfg.get("strategy_allocation_pct", 100)
    fill_interval = cfg.get("fills_poll_interval_seconds", 5)
    health_interval = cfg.get("health_check_interval_seconds", 30)
    default_balance = cfg.get("start_balance", cfg.get("shadow_start_balance", 10000.0))

    active = {k: v for k, v in cfg.get("coins", {}).items() if v.get("active")}

    for coin_key, coin_cfg in active.items():
        # This image runs the static grid only. A trailing or dynamic entry is a
        # configuration mistake: skip it loudly rather than crash the pod, which
        # would take the live grids down with it, and never skip it silently,
        # which would leave you believing a grid runs that doesn't.
        strategy_type = coin_cfg.get("strategy_type", "STATIC")
        if strategy_type != "STATIC":
            log.error(f"[{coin_key}] strategy_type={strategy_type} is not supported "
                      f"by grid-static; grid not started")
            await alerter.send_alert("error", {
                "coin": coin_cfg.get("coin", coin_key),
                "message": (f"{coin_key}: strategy_type={strategy_type} is not supported "
                            f"by grid-static; grid not started"),
            })
            continue

        # Capital is per grid: a shadow grid is its own simulation with its own
        # wallet, and two live grids must not each assume they own the account.
        start_balance = coin_cfg.get("start_balance", default_balance)
        grid = StaticGrid(coin_key, coin_cfg, strategy_pct, dal, connector, alerter,
                          fill_interval, health_interval, start_balance)
        _grids.append(grid)

    # The order matters. On a fresh install the dal comes up after us: its
    # initContainer applies the schema first. Without this wait, startup fails on the
    # first get_active_configs, uvicorn exits and the pod restarts. That heals itself,
    # but it looks like a broken install and costs a cycle -- and after a node outage
    # every pod starts at once, which is exactly when the race is most likely.
    await _wait_for("connector", connector.get_mids)
    await _wait_for("dal", dal.health)

    for grid in _grids:
        active_configs = await grid.dal.get_active_configs(grid.coin)
        matched = next((c for c in active_configs
                        if c["num_lines"] == grid.config["num_lines"]
                        and c["upper"] == grid.config["upper"]
                        and c["lower"] == grid.config["lower"]
                        and c["leverage"] == grid.config.get("leverage", 1)), None)
        if matched:
            grid.grid_config_id = matched["id"]
            grid.levels = calculate_levels(
                grid.config["lower"], grid.config["upper"], grid.config["num_lines"]
            )
            await grid.recover_state()
        else:
            await grid.initialize()

    tasks = []
    for grid in _grids:
        tasks.append(asyncio.create_task(grid.run_fill_loop()))
        tasks.append(asyncio.create_task(grid.run_health_loop()))

    log.info(f"Started {len(_grids)} grid(s), {len(tasks)} loop tasks")
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
