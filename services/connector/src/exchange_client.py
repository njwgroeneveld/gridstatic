import logging
import math
import os

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.types import Cloid

log = logging.getLogger(__name__)

_TESTNET_URL = "https://api.hyperliquid-testnet.xyz"
_MAINNET_URL = "https://api.hyperliquid.xyz"


def dex_of(coin: str) -> str:
    """The perp dex a coin trades on: "xyz" for a HIP-3 market such as
    "xyz:XYZ100", "" for the default dex."""
    return coin.split(":", 1)[0] if ":" in coin else ""


def _full_name(coin: str, dex: str) -> str:
    return f"{dex}:{coin}" if dex and ":" not in coin else coin


def _round_price(price: float, sig: int = 5) -> float:
    if price == 0:
        return 0.0
    d = math.ceil(math.log10(abs(price)))
    factor = 10 ** (sig - d)
    return round(price * factor) / factor


class ExchangeClient:
    def __init__(self, private_key: str, wallet_address: str = None, testnet: bool = True,
                 perp_dexs: list[str] | None = None):
        base_url = _TESTNET_URL if testnet else _MAINNET_URL
        self._account = Account.from_key(private_key)
        self._wallet_address = wallet_address or self._account.address
        # The default dex plus every HIP-3 dex a grid trades on. The SDK needs
        # them to turn a name like "xyz:XYZ100" into an asset id.
        self._dexes = [""] + [d for d in (perp_dexs or []) if d]
        self._info = Info(base_url, skip_ws=True, perp_dexs=self._dexes)
        self._exchange = Exchange(self._account, base_url, vault_address=wallet_address,
                                  perp_dexs=self._dexes)
        self._sz_cache: dict[str, int] = {}

    def get_sz_decimals(self, coin: str) -> int:
        """Raises KeyError for a coin the dex does not list: guessing a lot size
        sizes orders for a market nobody checked."""
        if coin not in self._sz_cache:
            meta = self._info.meta(dex=dex_of(coin))
            for asset in meta.get("universe", []):
                if asset["name"] == coin:
                    self._sz_cache[coin] = asset["szDecimals"]
                    break
            else:
                raise KeyError(f"{coin} is not listed on dex '{dex_of(coin)}'")
        return self._sz_cache[coin]

    def get_account_value(self, dex: str = "") -> float:
        """A HIP-3 dex keeps a balance of its own; only the default dex shares
        with spot USDC."""
        if dex:
            state = self._info.user_state(self._wallet_address, dex=dex)
            return float(state.get("marginSummary", {}).get("accountValue", 0.0))
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
        """Positions on every dex this client knows, under their full name. Any dex
        that cannot be read raises: an unreadable position is never a flat one."""
        result = {}
        for dex in self._dexes:
            state = self._info.user_state(self._wallet_address, dex=dex)
            for pos in state.get("assetPositions", []):
                p = pos.get("position", {})
                coin = p.get("coin")
                szi = float(p.get("szi") or 0)
                if coin and abs(szi) > 0:
                    result[_full_name(coin, dex)] = {"szi": szi,
                                                     "entry_px": float(p.get("entryPx") or 0)}
        return result

    def get_open_orders(self, coin: str) -> list[dict]:
        """Raises when the exchange cannot be read. Swallowing that into an empty
        list made "API unreachable" indistinguishable from "no orders", and
        recover_state reads an empty book as "alles is gevuld"."""
        dex = dex_of(coin)
        orders = self._info.frontend_open_orders(self._wallet_address, dex=dex)
        return [o for o in (orders or []) if _full_name(o.get("coin", ""), dex) == coin]

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
            log.warning(f"fetching funding failed: {e}")
            return []
        records = []
        for r in recs:
            d = r.get("delta", {})
            if d.get("type") != "funding":
                continue
            records.append({"time": int(r["time"]), "coin": d["coin"],
                            "usdc": float(d["usdc"]),
                            "funding_rate": float(d["fundingRate"]),
                            "szi": float(d["szi"])})
        return records

    def set_leverage(self, coin: str, leverage: int) -> None:
        try:
            self._exchange.update_leverage(leverage, coin, is_cross=False)
        except Exception as e:
            log.warning(f"[{coin}] setting leverage failed: {e}")

    def cancel_order(self, coin: str, oid: str) -> dict:
        try:
            result = self._exchange.cancel(coin, int(oid))
            if result.get("status") == "ok":
                return {"status": "ok"}
            return {"status": "error", "reason": str(result.get("response", "unknown"))}
        except Exception as e:
            return {"status": "error", "reason": str(e)}

    def place_limit_order(self, coin: str, is_buy: bool, price: float, sz: float,
                          cloid: str | None = None, reduce_only: bool = False) -> dict:
        """A GTC limit order, sized in coin by the caller. The caller also picks
        the cloid, so an order can always be found again by it -- there is no
        "ok but no oid" case to recover from any more."""
        try:
            px = _round_price(price)
            sz = round(sz, self.get_sz_decimals(coin))
            if sz <= 0:
                return {"status": "error", "reason": "size rounds to zero"}
            result = self._exchange.order(coin, is_buy, sz, px, {"limit": {"tif": "Gtc"}},
                                          reduce_only=reduce_only,
                                          cloid=Cloid.from_str(cloid) if cloid else None)
            if result.get("status") != "ok":
                return {"status": "error", "reason": str(result.get("response", "unknown"))}
            statuses = result["response"]["data"]["statuses"]
            if statuses and "error" in statuses[0]:
                return {"status": "error", "reason": statuses[0]["error"]}
            oid = (statuses[0].get("resting", {}).get("oid")
                   or statuses[0].get("filled", {}).get("oid"))
            return {"status": "ok", "oid": oid, "cloid": cloid, "sz": sz, "px": px}
        except Exception as e:
            return {"status": "error", "reason": str(e)}

    def get_order_status(self, oid: int) -> dict:
        """Which cloid an order carried. Fills only name the oid, so this is how a
        fill of an order that has left the book is tied back to its grid cell.
        Raises when the exchange cannot be read: "could not ask" must never read
        as "not one of ours"."""
        resp = self._info.query_order_by_oid(self._wallet_address, int(oid))
        if resp.get("status") != "order":
            return {"oid": int(oid), "cloid": None, "status": resp.get("status", "unknown")}
        entry = resp.get("order", {})
        return {"oid": int(oid), "cloid": entry.get("order", {}).get("cloid"),
                "status": entry.get("status", "unknown")}
