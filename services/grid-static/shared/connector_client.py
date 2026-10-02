import httpx

_HEADERS = {"X-Bot-Name": "grid-static"}


class ConnectorClient:
    """Every read raises when the connector cannot answer. The grid treats an
    exception as "do nothing this round" -- never as an empty book or a flat
    position."""

    def __init__(self, base_url: str) -> None:
        self._url = base_url.rstrip("/")

    async def _get(self, path: str, timeout: float = 10, **params):
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self._url}{path}", params=params or None,
                            headers=_HEADERS, timeout=timeout)
            r.raise_for_status()
            return r.json()

    async def get_mids(self) -> dict[str, float]:
        return await self._get("/mids")

    async def get_account_value(self, dex: str = "") -> float:
        return float((await self._get("/account/value", dex=dex))["account_value"])

    async def get_price(self, coin: str) -> float:
        """The mark price: on a thin book the mid moves with the grid's own orders."""
        return float((await self._get(f"/price/{coin}"))["mark_px"])

    async def get_positions(self) -> dict[str, dict]:
        return await self._get("/positions")

    async def get_open_orders(self, coin: str) -> list[dict]:
        return await self._get(f"/orders/{coin}")

    async def get_fills(self, coin: str, since_ms: int) -> list[dict]:
        return await self._get(f"/fills/{coin}", since_ms=since_ms)

    async def get_order_status(self, oid: int) -> dict:
        return await self._get(f"/orders/status/{oid}")

    async def get_sz_decimals(self, coin: str) -> int:
        return int((await self._get(f"/meta/{coin}"))["sz_decimals"])

    async def place_limit(self, coin: str, is_buy: bool, price: float, sz: float,
                          cloid: str, reduce_only: bool) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{self._url}/orders/limit", headers=_HEADERS, timeout=15,
                             json={"coin": coin, "is_buy": is_buy, "price": price, "sz": sz,
                                   "cloid": cloid, "reduce_only": reduce_only})
            r.raise_for_status()
            return r.json()

    async def cancel_order(self, coin: str, oid: int) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.delete(f"{self._url}/orders/{coin}/{oid}",
                               headers=_HEADERS, timeout=10)
            r.raise_for_status()
            return r.json()

    async def set_leverage(self, coin: str, leverage: int) -> None:
        async with httpx.AsyncClient() as c:
            r = await c.put(f"{self._url}/leverage/{coin}",
                            json={"leverage": leverage},
                            headers=_HEADERS, timeout=10)
            r.raise_for_status()
