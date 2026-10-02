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


def _perp_dexs() -> list[str]:
    """HIP-3 dexes the grids trade on, e.g. "xyz" for xyz:XYZ100. The chart
    derives them from the configured coins."""
    return [d.strip() for d in os.getenv("HYPERLIQUID_PERP_DEXS", "").split(",") if d.strip()]


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
        _exchange = ExchangeClient(_PRIVATE_KEY, _WALLET_ADDRESS, _TESTNET,
                                   perp_dexs=_perp_dexs())
    except Exception as e:
        log.error(f"building ExchangeClient failed: {e}")
        raise HTTPException(status_code=503, detail=f"exchange unreachable: {e}")
    log.info("ExchangeClient built")
    return _exchange


class LimitOrderReq(BaseModel):
    coin: str
    is_buy: bool
    price: float
    sz: float
    cloid: str | None = None
    reduce_only: bool = False


class StopOrderReq(BaseModel):
    coin: str
    sz: float
    trigger_px: float
    cloid: str | None = None


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
def get_account_value(dex: str = Query("")):
    return {"account_value": _ex().get_account_value(dex=dex)}


@app.get("/price/{coin}")
def get_price(coin: str):
    """The mark price. A grid decides which lines sit below the price from this,
    not from the mid: on a thin book the mid moves with the grid's own orders."""
    try:
        return {"coin": coin, "mark_px": _info.get_mark_price(coin)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/positions")
def get_positions():
    # An unreadable position must never look like "no position".
    try:
        return _ex().get_open_positions()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


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
    result = _ex().place_limit_order(req.coin, req.is_buy, req.price, req.sz,
                                     req.cloid, req.reduce_only)
    if result["status"] != "ok":
        raise HTTPException(status_code=502, detail=result.get("reason"))
    return result


@app.post("/orders/stop")
def place_stop(req: StopOrderReq):
    result = _ex().place_stop_order(req.coin, req.sz, req.trigger_px, req.cloid)
    if result["status"] != "ok":
        raise HTTPException(status_code=502, detail=result.get("reason"))
    return result


@app.get("/orders/status/{oid}")
def order_status(oid: int):
    try:
        return _ex().get_order_status(oid)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.delete("/orders/{coin}/{oid}")
def cancel_order(coin: str, oid: str):
    result = _ex().cancel_order(coin, oid)
    if result["status"] != "ok":
        raise HTTPException(status_code=502, detail=result.get("reason"))
    return result


@app.put("/leverage/{coin}")
def set_leverage(coin: str, req: LeverageReq):
    try:
        _ex().set_leverage(coin, req.leverage)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"status": "ok"}
