import html
import logging
import os
import threading
import time
from contextlib import asynccontextmanager

import requests as _requests
from fastapi import FastAPI, Request
from pydantic import BaseModel

from src.formatters import format_alert, format_grid_status

log = logging.getLogger("alerter.access")

_BOT_TOKEN: str | None = os.getenv("TELEGRAM_BOT_TOKEN")
_CHAT_ID: str | None = os.getenv("TELEGRAM_CHAT_ID")
_GRID_STATIC_URL: str = os.getenv("GRID_STATIC_URL", "http://grid-static:8080")


def _handle_status() -> None:
    """grid-static knows its grids and what it saw in the last round, the hold
    included; this only formats its answer."""
    try:
        resp = _requests.get(f"{_GRID_STATIC_URL}/status", timeout=10)
        resp.raise_for_status()
        grids = resp.json().get("grids", [])
    except Exception as e:
        log.warning(f"/status: grid-static unreachable: {e}")
        _send_telegram("⚠️ Could not reach grid-static for /status")
        return
    _send_telegram(format_grid_status(grids))


def _poll_commands() -> None:
    offset = 0
    while True:
        try:
            resp = _requests.get(
                f"https://api.telegram.org/bot{_BOT_TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 20},
                timeout=25,
            )
            for update in resp.json().get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                if str(msg.get("chat", {}).get("id", "")) != str(_CHAT_ID):
                    continue
                if msg.get("text", "").strip() == "/status":
                    _handle_status()
        except Exception as e:
            log.warning(f"Telegram poll fout: {e}")
            time.sleep(10)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if _BOT_TOKEN and _CHAT_ID:
        threading.Thread(target=_poll_commands, daemon=True, name="tg-commands").start()
        log.info("Telegram command listener started (/status)")
    else:
        log.warning("Telegram credentials not configured -- command listener skipped")
    yield


app = FastAPI(title="Telegram Alerter", lifespan=lifespan)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    t0 = time.time()
    response = await call_next(request)
    ms = (time.time() - t0) * 1000
    bot = request.headers.get("X-Bot-Name", "-")
    host = request.client.host if request.client else "unknown"
    log.info(
        f"{host} [{bot}] \"{request.method} {request.url.path}\" "
        f"{response.status_code} {ms:.0f}ms"
    )
    return response


class AlertIn(BaseModel):
    type: str
    bot: str
    payload: dict = {}


_TG_MAX = 4096


def _split_message(text: str) -> list[str]:
    chunks, current = [], []
    current_len = 0
    for line in text.split("\n"):
        # +1 for the newline we'll re-add between lines
        needed = len(line) + (1 if current else 0)
        if current and current_len + needed > _TG_MAX:
            chunks.append("\n".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += needed
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def _send_telegram(text: str) -> None:
    if not _BOT_TOKEN or not _CHAT_ID:
        log.warning("Telegram credentials not configured -- alert skipped")
        return
    for chunk in _split_message(text):
        try:
            resp = _requests.post(
                f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage",
                json={"chat_id": _CHAT_ID, "text": chunk, "parse_mode": "HTML"},
                timeout=5,
            )
            if not resp.ok:
                log.error(f"Telegram send failed: {resp.status_code} {resp.text}")
            resp.raise_for_status()
        except _requests.HTTPError:
            pass  # already logged above
        except Exception as e:
            log.warning(f"Telegram send temporarily failed: {e}")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/alert")
def post_alert(body: AlertIn) -> dict:
    try:
        text = format_alert(body.type, body.bot, body.payload)
        _send_telegram(text)
    except Exception as e:
        log.error(f"post_alert error: {e}")
    return {"status": "ok"}
