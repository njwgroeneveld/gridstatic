import logging
import os
import time
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel
from src.info_client import InfoClient
from src.exchange_client import ExchangeClient

log = logging.getLogger("connector.access")

app = FastAPI(title="Connector", version="1.0.0")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    t0 = time.time()
    response = await call_next(request)
    ms = (time.time() - t0) * 1000
    bot = request.headers.get("X-Bot-Name", "-")
    log.info(
        f"{request.client.host} [{bot}] \"{request.method} {request.url.path}\" "
        f"{response.status_code} {ms:.0f}ms"
    )
    return response
_info = InfoClient()

_PRIVATE_KEY = os.getenv("HL_PRIVATE_KEY", "")
_WALLET_ADDRESS = os.getenv("WALLET_ADDRESS") or None
_TESTNET = os.getenv("HYPERLIQUID_TESTNET", "true").lower() != "false"
_exchange: ExchangeClient | None = None


def _ex() -> ExchangeClient:
    """Built on first use, not at import. The SDK fetches metadata over the
    network while constructing, so a DNS hiccup during pod start used to leave
    _exchange None for the life of the pod -- every order endpoint 503 while
    /health happily reported 200. Building here lets it recover by itself as
    soon as the network is back, with no restart."""
    global _exchange
    if _exchange is not None:
        return _exchange
    if not _PRIVATE_KEY:
        raise HTTPException(status_code=503, detail="HL_PRIVATE_KEY not configured")
    try:
        _exchange = ExchangeClient(_PRIVATE_KEY, _WALLET_ADDRESS, _TESTNET)
    except Exception as e:
        log.error(f"ExchangeClient opbouwen mislukt: {e}")
        raise HTTPException(status_code=503, detail=f"exchange niet bereikbaar: {e}")
    log.info("ExchangeClient opgebouwd")
    return _exchange


class LimitOrderReq(BaseModel):
    coin: str
    direction: str
    price: float
    size_usd: float
    leverage: int = 3


class TpOrderReq(BaseModel):
    coin: str
    direction: str
    sz_coin: float
    limit_price: float


class SlOrderReq(BaseModel):
    coin: str
    direction: str
    sz_coin: float
    trigger_price: float


class ClosePositionReq(BaseModel):
    direction: str
    size_usd: float
    entry_price: float


class LeverageReq(BaseModel):
    leverage: int


@app.get("/health")
async def health():
    # Deliberately still 200 when the exchange is unreachable: the liveness probe
    # reads this, and restarting a pod does not fix someone else's network. The
    # field makes the state visible; _ex() recovers on its own.
    return {"status": "ok",
            "exchange": "ok" if _exchange is not None else "not_connected",
            "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/mids")
def get_mids():
    try:
        return _info.get_all_mids()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/account/value")
def get_account_value():
    return {"account_value": _ex().get_account_value()}


@app.get("/positions")
def get_positions():
    return _ex().get_open_positions()


@app.get("/orders/{coin}")
def get_orders(coin: str):
    try:
        return _ex().get_open_orders(coin)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/funding")
def get_funding(since_ms: int = Query(0)):
    return _ex().get_funding_since(since_ms)


@app.get("/fills/{coin}")
def get_fills(coin: str, since_ms: int = Query(0)):
    try:
        return _ex().get_fills_since(coin, since_ms)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/meta/{coin}")
def get_meta(coin: str):
    return {"coin": coin, "sz_decimals": _info.get_sz_decimals(coin)}


@app.post("/orders/limit")
def place_limit(req: LimitOrderReq):
    result = _ex().place_limit_order(req.coin, req.direction, req.price, req.size_usd, req.leverage)
    if result["status"] != "ok":
        raise HTTPException(status_code=502, detail=result.get("reden"))
    return result


@app.post("/orders/tp")
def place_tp(req: TpOrderReq):
    result = _ex().place_tp_limit_order(req.coin, req.direction, req.sz_coin, req.limit_price)
    if result["status"] != "ok":
        raise HTTPException(status_code=502, detail=result.get("reden"))
    return result


@app.post("/orders/sl")
def place_sl(req: SlOrderReq):
    result = _ex().place_sl_trigger_order(req.coin, req.direction, req.sz_coin, req.trigger_price)
    if result["status"] != "ok":
        raise HTTPException(status_code=502, detail=result.get("reden"))
    return result


@app.delete("/orders/{coin}/{oid}")
def cancel_order(coin: str, oid: str):
    result = _ex().cancel_order(coin, oid)
    if result["status"] != "ok":
        raise HTTPException(status_code=502, detail=result.get("reden"))
    return result


@app.post("/positions/{coin}/close")
def close_position(coin: str, req: ClosePositionReq):
    result = _ex().close_position_market(coin, req.direction, req.size_usd, req.entry_price)
    if result["status"] not in ("ok", "not_found"):
        raise HTTPException(status_code=502, detail=result.get("reden"))
    return result


@app.put("/leverage/{coin}")
def set_leverage(coin: str, req: LeverageReq):
    _ex().set_leverage(coin, req.leverage)
    return {"status": "ok"}
