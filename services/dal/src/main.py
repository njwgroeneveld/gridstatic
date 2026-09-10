import logging
import time
from functools import lru_cache

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel

from src.database import Database, get_db

log = logging.getLogger("dal")
app = FastAPI(title="Gridtrading DAL")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    t0 = time.time()
    response = await call_next(request)
    ms = (time.time() - t0) * 1000
    log.info(f'"{request.method} {request.url.path}" {response.status_code} {ms:.0f}ms')
    return response


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ── grid_configs ──────────────────────────────────────────────────────────────

class GridConfigIn(BaseModel):
    coin: str
    strategy: str = "STATIC"
    upper: float
    lower: float
    num_lines: int
    leverage: float = 1.0
    shadow: bool = True
    active: bool = True


@app.post("/grid-configs", status_code=201)
def insert_grid_config(body: GridConfigIn, db: Database = Depends(get_db)) -> dict:
    return db.insert_grid_config(body.model_dump())


@app.get("/grid-configs")
def get_grid_configs(coin: str | None = None, strategy: str = "STATIC",
                     active: bool = True, db: Database = Depends(get_db)) -> list[dict]:
    return db.get_active_configs(coin, strategy)


class PatchGridConfigIn(BaseModel):
    upper: float | None = None
    lower: float | None = None


@app.patch("/grid-configs/{config_id}")
def patch_grid_config(config_id: int, body: PatchGridConfigIn,
                      db: Database = Depends(get_db)) -> dict:
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if fields:
        db.patch_grid_config(config_id, fields)
    return {"status": "ok"}


# ── grid_orders ───────────────────────────────────────────────────────────────

class GridOrderIn(BaseModel):
    grid_config_id: int | None = None
    coin: str
    strategy: str = "STATIC"
    side: str
    level: int
    price: float
    size_usd: float
    exchange_order_id: str | None = None
    status: str = "OPEN"
    shadow: bool = True
    fee_usd: float | None = None


class PatchOrderIn(BaseModel):
    status: str | None = None
    filled_price: float | None = None
    filled_at: str | None = None
    exchange_order_id: str | None = None
    fee_usd: float | None = None


@app.post("/grid-orders", status_code=201)
def insert_grid_order(body: GridOrderIn, db: Database = Depends(get_db)) -> dict:
    return db.insert_grid_order(body.model_dump())


@app.get("/grid-orders")
def get_grid_orders(coin: str, status: str | None = None,
                    db: Database = Depends(get_db)) -> list[dict]:
    return db.get_orders(coin, status)


@app.patch("/grid-orders/{order_id}")
def patch_grid_order(order_id: int, body: PatchOrderIn,
                     db: Database = Depends(get_db)) -> dict:
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if fields:
        db.patch_order(order_id, fields)
    return {"status": "ok"}


# ── grid_trades ───────────────────────────────────────────────────────────────

class GridFundingIn(BaseModel):
    coin: str
    grid_config_id: int | None = None
    shadow: bool = False
    time: int
    usdc: float
    funding_rate: float | None = None
    szi: float | None = None


@app.post("/grid-funding", status_code=201)
def insert_grid_funding(body: GridFundingIn, db: Database = Depends(get_db)) -> dict:
    return db.insert_grid_funding(body.model_dump()) or {"status": "bestond_al"}


@app.get("/grid-funding/total")
def get_funding_total(coin: str, grid_config_id: int | None = None,
                      db: Database = Depends(get_db)) -> dict:
    return {"total_usdc": db.get_funding_total(coin, grid_config_id)}


@app.get("/grid-funding/last")
def get_last_funding(coin: str, shadow: bool = False,
                     db: Database = Depends(get_db)) -> dict:
    return {"last_ms": db.get_last_funding_ms(coin, shadow)}


class GridTradeIn(BaseModel):
    coin: str
    strategy: str = "STATIC"
    buy_order_id: int | None = None
    buy_price: float | None = None
    size_usd: float | None = None
    status: str = "OPEN"
    shadow: bool = True
    fee_usd: float | None = None


class PatchTradeIn(BaseModel):
    sell_order_id: int | None = None
    sell_price: float | None = None
    profit_usd: float | None = None
    fee_usd: float | None = None
    status: str | None = None
    closed_at: str | None = None


@app.post("/grid-trades", status_code=201)
def insert_grid_trade(body: GridTradeIn, db: Database = Depends(get_db)) -> dict:
    return db.insert_grid_trade(body.model_dump())


@app.get("/grid-trades")
def get_grid_trades(coin: str, status: str | None = None,
                    grid_config_id: int | None = None,
                    db: Database = Depends(get_db)) -> list[dict]:
    return db.get_trades(coin, status, grid_config_id)


@app.patch("/grid-trades/{trade_id}")
def patch_grid_trade(trade_id: int, body: PatchTradeIn,
                     db: Database = Depends(get_db)) -> dict:
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if fields:
        db.patch_trade(trade_id, fields)
    return {"status": "ok"}
