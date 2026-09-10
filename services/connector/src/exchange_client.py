import logging
import math
import os

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

log = logging.getLogger(__name__)

_TESTNET_URL = "https://api.hyperliquid-testnet.xyz"
_MAINNET_URL = "https://api.hyperliquid.xyz"


def _round_price(price: float, sig: int = 5) -> float:
    if price == 0:
        return 0.0
    d = math.ceil(math.log10(abs(price)))
    factor = 10 ** (sig - d)
    return round(price * factor) / factor


class ExchangeClient:
    def __init__(self, private_key: str, wallet_address: str = None, testnet: bool = True):
        base_url = _TESTNET_URL if testnet else _MAINNET_URL
        self._account = Account.from_key(private_key)
        self._wallet_address = wallet_address or self._account.address
        self._info = Info(base_url, skip_ws=True)
        self._exchange = Exchange(self._account, base_url, vault_address=wallet_address)
        self._sz_cache: dict[str, int] = {}

    def get_sz_decimals(self, coin: str) -> int:
        if coin not in self._sz_cache:
            meta = self._info.meta()
            for asset in meta.get("universe", []):
                if asset["name"] == coin:
                    self._sz_cache[coin] = asset["szDecimals"]
                    break
            else:
                self._sz_cache[coin] = 3
        return self._sz_cache[coin]

    def get_account_value(self) -> float:
        spot = self._info.spot_user_state(self._wallet_address)
        usdc_total, usdc_hold = 0.0, 0.0
        for b in spot.get("balances", []):
            if b.get("coin") == "USDC":
                usdc_total = float(b.get("total", 0.0))
                usdc_hold = float(b.get("hold", 0.0))
                break
        state = self._info.user_state(self._wallet_address)
        perps_value = float(state.get("marginSummary", {}).get("accountValue", 0.0))
        return (usdc_total - usdc_hold) + perps_value

    def get_open_positions(self) -> dict[str, dict]:
        state = self._info.user_state(self._wallet_address)
        result = {}
        for pos in state.get("assetPositions", []):
            p = pos.get("position", {})
            coin = p.get("coin")
            szi = float(p.get("szi") or 0)
            if coin and abs(szi) > 0:
                result[coin] = {"szi": szi, "entry_px": float(p.get("entryPx") or 0)}
        return result

    def get_open_orders(self, coin: str) -> list[dict]:
        """Raises when the exchange cannot be read. Swallowing that into an empty
        list made "API onbereikbaar" indistinguishable from "geen orders", and
        recover_state reads an empty book as "alles is gevuld"."""
        orders = self._info.frontend_open_orders(self._wallet_address)
        return [o for o in (orders or []) if o.get("coin") == coin]

    def get_fills_since(self, coin: str, since_ms: int) -> list[dict]:
        """Raises for the same reason: the caller advances a watermark on the
        strength of this answer, so an empty list has to mean there were no
        fills -- never that we failed to ask."""
        fills = self._info.user_fills(self._wallet_address)
        return [f for f in (fills or [])
                if f.get("coin") == coin and f.get("time", 0) >= since_ms]

    def get_funding_since(self, since_ms: int) -> list[dict]:
        """Funding is charged per hour on the net position and cannot be derived
        from fills, so it has to be read separately. usdc is signed from the
        account's point of view: negative means paid, positive means received."""
        try:
            recs = self._info.user_funding_history(self._wallet_address, since_ms) or []
        except Exception as e:
            log.warning(f"funding ophalen mislukt: {e}")
            return []
        uit = []
        for r in recs:
            d = r.get("delta", {})
            if d.get("type") != "funding":
                continue
            uit.append({"time": int(r["time"]), "coin": d["coin"],
                        "usdc": float(d["usdc"]),
                        "funding_rate": float(d["fundingRate"]),
                        "szi": float(d["szi"])})
        return uit

    def set_leverage(self, coin: str, leverage: int) -> None:
        try:
            self._exchange.update_leverage(leverage, coin, is_cross=False)
        except Exception as e:
            log.warning(f"[{coin}] leverage instellen mislukt: {e}")

    def cancel_order(self, coin: str, oid: str) -> dict:
        try:
            result = self._exchange.cancel(coin, int(oid))
            if result.get("status") == "ok":
                return {"status": "ok"}
            return {"status": "error", "reden": str(result.get("response", "onbekend"))}
        except Exception as e:
            return {"status": "error", "reden": str(e)}

    def place_limit_order(self, coin: str, direction: str, price: float,
                          size_usd: float, leverage: int = 3) -> dict:
        try:
            self.set_leverage(coin, leverage)
            px = _round_price(price)
            # Leverage multiplies the position: the exchange-side leverage
            # setting only lowers the margin requirement, it never sizes the
            # order. Notional = size_usd * leverage.
            sz = round(size_usd * leverage / px, self.get_sz_decimals(coin))
            if sz <= 0:
                return {"status": "error", "reden": "Positiegrootte te klein"}
            import time as _time
            placed_at_ms = int(_time.time() * 1000)
            result = self._exchange.order(coin, direction == "BUY", sz, px,
                                          {"limit": {"tif": "Gtc"}})
            if result.get("status") == "ok":
                statuses = result["response"]["data"]["statuses"]
                if statuses and "error" in statuses[0]:
                    return {"status": "error", "reden": statuses[0]["error"]}
                oid = (statuses[0].get("resting", {}).get("oid")
                       or statuses[0].get("filled", {}).get("oid"))
                if oid is None:
                    log.warning(f"[{coin}] Limit order OK maar geen OID in response: {statuses[0]}")
                    oid = self._recover_oid(coin, direction, px, placed_at_ms)
                return {"status": "ok", "hl_order_id": str(oid) if oid is not None else None,
                        "sz_coin": sz}
            return {"status": "error", "reden": str(result.get("response", "onbekend"))}
        except Exception as e:
            return {"status": "error", "reden": str(e)}

    def _recover_oid(self, coin: str, direction: str, price: float,
                     placed_at_ms: int) -> int | None:
        """Herstel OID via open orders of recente fills wanneer initieel ontbreekt."""
        import time as _time
        _time.sleep(0.5)
        is_buy = direction == "BUY"

        # Stap 1: open orders (order staat nog als resting)
        try:
            orders = self._info.frontend_open_orders(self._wallet_address)
            for o in orders:
                if (o.get("coin") == coin
                        and bool(o.get("isBuy")) == is_buy
                        and not o.get("reduceOnly", False)
                        and abs(float(o.get("limitPx", 0)) - price) / price < 0.002):
                    oid = o.get("oid")
                    if oid is not None:
                        log.info(f"[{coin}] OID hersteld via open orders: {oid}")
                        return oid
        except Exception as e:
            log.warning(f"[{coin}] Open orders query mislukt bij OID recovery: {e}")

        # Stap 2: recente fills (order direct gevuld)
        try:
            fills = self._info.user_fills(self._wallet_address)
            fill_side = "B" if is_buy else "A"
            for f in fills:
                if (f.get("coin") == coin
                        and f.get("side") == fill_side
                        and int(f.get("time", 0)) >= placed_at_ms):
                    oid = f.get("oid")
                    if oid is not None:
                        log.info(f"[{coin}] OID hersteld via fills: {oid}")
                        return oid
        except Exception as e:
            log.warning(f"[{coin}] Fills query mislukt bij OID recovery: {e}")

        log.error(f"[{coin}] OID recovery mislukt — order niet te traceren op HL")
        return None

    def place_tp_limit_order(self, coin: str, direction: str,
                             sz_coin: float, limit_price: float) -> dict:
        try:
            is_buy = direction == "SELL"
            sz = round(sz_coin, self.get_sz_decimals(coin))
            if sz <= 0:
                return {"status": "error", "reden": "TP size te klein"}
            result = self._exchange.order(coin, is_buy, sz, _round_price(limit_price),
                                          {"limit": {"tif": "Gtc"}}, reduce_only=True)
            if result.get("status") == "ok":
                statuses = result["response"]["data"]["statuses"]
                if statuses and "error" in statuses[0]:
                    return {"status": "error", "reden": statuses[0]["error"]}
                oid = (statuses[0].get("resting", {}).get("oid")
                       or statuses[0].get("filled", {}).get("oid"))
                if oid is None:
                    log.warning(f"[{coin}] TP order OK maar geen OID in response: {statuses[0]}")
                return {"status": "ok", "hl_order_id": str(oid) if oid is not None else None}
            return {"status": "error", "reden": str(result.get("response", "onbekend"))}
        except Exception as e:
            return {"status": "error", "reden": str(e)}

    def place_sl_trigger_order(self, coin: str, direction: str,
                               sz_coin: float, trigger_price: float) -> dict:
        try:
            is_buy = direction == "SELL"
            sz = round(sz_coin, self.get_sz_decimals(coin))
            if sz <= 0:
                return {"status": "error", "reden": "SL size te klein"}
            tp = _round_price(trigger_price)
            lp = _round_price(tp * (1.05 if is_buy else 0.95))
            result = self._exchange.order(
                coin, is_buy, sz, lp,
                {"trigger": {"triggerPx": tp, "isMarket": True, "tpsl": "sl"}},
                reduce_only=True,
            )
            if result.get("status") == "ok":
                statuses = result["response"]["data"]["statuses"]
                if statuses and "error" in statuses[0]:
                    return {"status": "error", "reden": statuses[0]["error"]}
                oid = (statuses[0].get("resting", {}).get("oid")
                       or statuses[0].get("filled", {}).get("oid")
                       or statuses[0].get("triggered", {}).get("oid"))
                if oid is None:
                    log.warning(f"[{coin}] SL trigger order OK maar geen OID in response: {statuses[0]}")
                return {"status": "ok", "hl_order_id": str(oid) if oid is not None else None}
            return {"status": "error", "reden": str(result.get("response", "onbekend"))}
        except Exception as e:
            return {"status": "error", "reden": str(e)}

    def close_position_market(self, coin: str, direction: str,
                              size_usd: float, entry_price: float) -> dict:
        try:
            import time as _time
            close_ms = int(_time.time() * 1000)
            result = self._exchange.market_close(coin)
            if result is None:
                return {"status": "not_found", "reden": "Positie niet gevonden op exchange"}
            if result.get("status") == "ok":
                statuses = result.get("response", {}).get("data", {}).get("statuses", [{}])
                if statuses and "error" in statuses[0]:
                    return {"status": "error", "reden": statuses[0]["error"]}
                oid = str(statuses[0].get("filled", {}).get("oid", "")) if statuses else ""
                try:
                    fills = self._info.user_fills_by_time(self._wallet_address, close_ms - 3000) or []
                    close_fills = [f for f in fills
                                   if f.get("coin") == coin
                                   and str(f.get("oid", "")) == oid
                                   and f.get("closedPnl") is not None]
                    closed_pnl = round(sum(float(f["closedPnl"]) for f in close_fills), 2) if close_fills else None
                except Exception as e:
                    log.warning(f"[{coin}] closedPnl ophalen mislukt: {e}")
                    closed_pnl = None
                return {"status": "ok", "closed_pnl": closed_pnl}
            return {"status": "error", "reden": str(result.get("response", "onbekend"))}
        except Exception as e:
            return {"status": "error", "reden": str(e)}
