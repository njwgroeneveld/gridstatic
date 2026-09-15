import httpx

_HEADERS = {"X-Bot-Name": "grid-engine"}


class DALClient:
    def __init__(self, base_url: str) -> None:
        self._url = base_url.rstrip("/")

    async def health(self) -> dict:
        """Only checks whether the dal answers. On a fresh install its initContainer
        applies the schema first, so it comes up later than we do."""
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self._url}/health", headers=_HEADERS, timeout=5)
            r.raise_for_status()
            return r.json()

    async def insert_grid_config(self, data: dict) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{self._url}/grid-configs", json=data, headers=_HEADERS, timeout=10)
            r.raise_for_status()
            return r.json()

    async def get_active_configs(self, coin: str, strategy: str = "STATIC") -> list[dict]:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self._url}/grid-configs",
                            params={"coin": coin, "strategy": strategy, "active": "true"},
                            headers=_HEADERS, timeout=10)
            r.raise_for_status()
            return r.json()

    async def patch_grid_config(self, config_id: int, fields: dict) -> None:
        async with httpx.AsyncClient() as c:
            r = await c.patch(f"{self._url}/grid-configs/{config_id}",
                              json=fields, headers=_HEADERS, timeout=10)
            r.raise_for_status()

    async def insert_grid_order(self, data: dict) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{self._url}/grid-orders", json=data, headers=_HEADERS, timeout=10)
            r.raise_for_status()
            return r.json()

    async def get_open_orders(self, coin: str) -> list[dict]:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self._url}/grid-orders",
                            params={"coin": coin, "status": "OPEN"},
                            headers=_HEADERS, timeout=10)
            r.raise_for_status()
            return r.json()

    async def patch_order(self, order_id: int, fields: dict) -> None:
        async with httpx.AsyncClient() as c:
            r = await c.patch(f"{self._url}/grid-orders/{order_id}",
                              json=fields, headers=_HEADERS, timeout=10)
            r.raise_for_status()

    async def insert_grid_funding(self, data: dict) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{self._url}/grid-funding", json=data, timeout=15)
            r.raise_for_status()
            return r.json()

    async def get_last_funding_ms(self, coin: str, shadow: bool = False) -> int | None:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self._url}/grid-funding/last",
                            params={"coin": coin, "shadow": shadow}, timeout=15)
            r.raise_for_status()
            return r.json().get("last_ms")

    async def insert_grid_trade(self, data: dict) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{self._url}/grid-trades", json=data, headers=_HEADERS, timeout=10)
            r.raise_for_status()
            return r.json()

    async def get_open_trades(self, coin: str, grid_config_id: int | None = None) -> list[dict]:
        params = {"coin": coin, "status": "OPEN"}
        if grid_config_id is not None:
            params["grid_config_id"] = grid_config_id
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self._url}/grid-trades",
                            params=params,
                            headers=_HEADERS, timeout=10)
            r.raise_for_status()
            return r.json()

    async def get_closed_trades(self, coin: str, grid_config_id: int) -> list[dict]:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self._url}/grid-trades",
                            params={"coin": coin, "status": "CLOSED",
                                    "grid_config_id": grid_config_id},
                            headers=_HEADERS, timeout=10)
            r.raise_for_status()
            return r.json()

    async def patch_trade(self, trade_id: int, fields: dict) -> None:
        async with httpx.AsyncClient() as c:
            r = await c.patch(f"{self._url}/grid-trades/{trade_id}",
                              json=fields, headers=_HEADERS, timeout=10)
            r.raise_for_status()
