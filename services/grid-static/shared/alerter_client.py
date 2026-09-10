import httpx

_HEADERS = {"X-Bot-Name": "grid-engine"}


class AlerterClient:
    def __init__(self, base_url: str) -> None:
        self._url = base_url.rstrip("/")

    async def send_alert(self, alert_type: str, payload: dict) -> None:
        try:
            async with httpx.AsyncClient() as c:
                await c.post(f"{self._url}/alert",
                             json={"type": alert_type, "bot": "grid-engine", "payload": payload},
                             headers=_HEADERS, timeout=5)
        except Exception:
            pass  # alerts are best-effort, never crash the grid loop
