import httpx

_HEADERS = {"X-Bot-Name": "grid-static"}


class ConnectorClient:
    def __init__(self, base_url: str) -> None:
        self._url = base_url.rstrip("/")

    async def get_mids(self) -> dict[str, float]:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self._url}/mids", headers=_HEADERS, timeout=10)
            r.raise_for_status()
            return r.json()

    async def get_account_value(self) -> float:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self._url}/account/value", headers=_HEADERS, timeout=10)
            r.raise_for_status()
            return float(r.json()["account_value"])

    async def get_fills(self, coin: str, since_ms: int) -> list[dict]:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self._url}/fills/{coin}",
                            params={"since_ms": since_ms},
                            headers=_HEADERS, timeout=10)
            r.raise_for_status()
            return r.json()

    async def get_open_orders(self, coin: str) -> list[dict]:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self._url}/orders/{coin}", headers=_HEADERS, timeout=10)
            r.raise_for_status()
            return r.json()

    async def place_buy_limit(self, coin: str, price: float,
                              size_usd: float, leverage: int) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{self._url}/orders/limit", headers=_HEADERS, timeout=15,
                             json={"coin": coin, "direction": "BUY",
                                   "price": price, "size_usd": size_usd,
                                   "leverage": leverage})
            r.raise_for_status()
            return r.json()

    async def place_sell_limit(self, coin: str, sz_coin: float, price: float) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{self._url}/orders/tp", headers=_HEADERS, timeout=15,
                             json={"coin": coin, "direction": "BUY",
                                   "sz_coin": sz_coin, "limit_price": price})
            r.raise_for_status()
            return r.json()

    async def get_funding(self, since_ms: int) -> list[dict]:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self._url}/funding", headers=_HEADERS, timeout=20,
                            params={"since_ms": since_ms})
            r.raise_for_status()
            return r.json()

    async def cancel_order(self, coin: str, oid: str) -> dict:
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
