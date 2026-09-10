import html
import logging
import os
import threading
import time
from contextlib import asynccontextmanager

import requests as _requests
from fastapi import FastAPI, Request
from pydantic import BaseModel

from src.formatters import format_alert

log = logging.getLogger("alerter.access")

_BOT_TOKEN: str | None = os.getenv("TELEGRAM_BOT_TOKEN")
_CHAT_ID: str | None = os.getenv("TELEGRAM_CHAT_ID")
_DAL_URL: str = os.getenv("DAL_URL", "http://dal:8080")
_CONNECTOR_URL: str = os.getenv("CONNECTOR_URL", "http://connector:8080")


def _funding_regel(funding_by_cfg: dict[int, float] | None, cfg_id: int,
                   realized: float) -> str:
    """Funding staat naast de gridwinst, nooit erin: het wordt per uur over de
    netto positie afgerekend en is niet aan een losse trade toe te rekenen."""
    funding = (funding_by_cfg or {}).get(cfg_id, 0.0)
    if not funding:
        return ""
    return f"\nFunding: {funding:+.2f}$ | Netto: {realized + funding:+.2f}$"


def _format_grid_status(configs: list[dict], orders: list[dict],
                         filled_orders: list[dict],
                         trades_by_cfg: dict[int, list], mids: dict,
                         funding_by_cfg: dict[int, float] | None = None) -> str:
    if not configs:
        return "📊 <b>Geen actieve grids</b>"

    lines = ["📊 <b>GRID STATUS</b>"]

    for cfg in configs:
        cfg_id = cfg["id"]
        coin = cfg["coin"]
        lower = float(cfg["lower"])
        upper = float(cfg["upper"])
        num_lines = cfg["num_lines"]
        strategy = cfg.get("strategy", "STATIC")
        shadow = cfg.get("shadow", True)
        leverage = int(cfg.get("leverage", 1))
        lev_tag = f" {leverage}x" if leverage > 1 else ""
        tag = " (shadow)" if shadow else ""
        current = mids.get(coin)

        spacing = (upper - lower) / (num_lines - 1) if num_lines > 1 else 0
        levels = [round(lower + i * spacing, 2) for i in range(num_lines)]

        # Matched by price, not by the stored "level" field: TrailingGrid uses
        # an absolute, never-resetting level number (anchored to its original
        # lower bound) that no longer lines up with a 0-indexed position once
        # the window has shifted.
        cfg_orders = [o for o in orders if o.get("grid_config_id") == cfg_id]
        buy_by_price = {round(float(o["price"]), 2): o for o in cfg_orders if o["side"] == "BUY"}
        sell_by_price = {round(float(o["price"]), 2): o for o in cfg_orders if o["side"] == "SELL"}

        cfg_filled = [o for o in filled_orders if o.get("grid_config_id") == cfg_id]
        buy_fills: dict[float, int] = {}
        sell_fills: dict[float, int] = {}
        for o in cfg_filled:
            px = round(float(o["price"]), 2)
            if o["side"] == "BUY":
                buy_fills[px] = buy_fills.get(px, 0) + 1
            else:
                sell_fills[px] = sell_fills.get(px, 0) + 1

        all_trades = trades_by_cfg.get(cfg_id, [])
        closed = [t for t in all_trades if t.get("status") == "CLOSED"]
        open_trades = [t for t in all_trades if t.get("status") == "OPEN"]

        realized = sum(float(t.get("profit_usd") or 0) for t in closed)
        unrealized = 0.0
        if current:
            for t in open_trades:
                buy_px = float(t.get("buy_price") or 0)
                size = float(t.get("size_usd") or 0)
                if buy_px and size:
                    unrealized += (current - buy_px) / buy_px * size * leverage

        price_str = f"${current:,.0f}" if current else "onbekend"
        real_str = f"{realized:+.2f}$" if closed else "–"
        unreal_str = f"{unrealized:+.2f}$" if current and open_trades else "–"

        lines.append(
            f"\n<b>── {coin} {num_lines}L{lev_tag} [{strategy}]{tag} ──</b>\n"
            f"${lower:,.0f} ↔ ${upper:,.0f} | Nu: {price_str}\n"
            f"Gerealiseerd: {real_str} | Open P&amp;L: {unreal_str}"
            + _funding_regel(funding_by_cfg, cfg_id, realized)
        )

        for i in range(num_lines - 1, -1, -1):
            px_level = levels[i]
            if px_level in sell_by_price:
                o = sell_by_price[px_level]
                icon = "💰"
                px = float(o["price"])
                sz = float(o.get("size_usd") or 0)
                label = f"SELL ${px:,.0f} (${sz * leverage:,.0f})"
            elif px_level in buy_by_price:
                o = buy_by_price[px_level]
                icon = "📋"
                px = float(o["price"])
                sz = float(o.get("size_usd") or 0)
                label = f"BUY ${px:,.0f} (${sz * leverage:,.0f})"
            else:
                icon = "▫️"
                label = ""

            price_marker = ""
            if current and i < num_lines - 1:
                if levels[i] <= current < levels[i + 1]:
                    price_marker = f" ← now"

            b = buy_fills.get(px_level, 0)
            s = sell_fills.get(px_level, 0)
            hits_str = f" [B:{b} S:{s}]" if b or s else ""
            lines.append(f"  {icon} L{i}  {label}{hits_str}{price_marker}")

    return "\n".join(lines)


def _handle_status() -> None:
    configs: list[dict] = []
    for strategy in ("STATIC",):
        try:
            resp = _requests.get(f"{_DAL_URL}/grid-configs",
                                 params={"strategy": strategy}, timeout=10)
            resp.raise_for_status()
            configs.extend(resp.json())
        except Exception as e:
            log.warning(f"/status grid-configs query mislukt voor strategy={strategy}: {e}")

    if not configs:
        _send_telegram("⚠️ Kon grid configs niet ophalen van DAL")
        return

    coins = list({c["coin"] for c in configs})
    orders: list[dict] = []
    filled_orders: list[dict] = []
    for coin in coins:
        try:
            resp = _requests.get(f"{_DAL_URL}/grid-orders",
                                 params={"coin": coin, "status": "OPEN"}, timeout=10)
            resp.raise_for_status()
            orders.extend(resp.json())
        except Exception as e:
            log.warning(f"/status grid-orders query mislukt voor {coin}: {e}")
        try:
            resp = _requests.get(f"{_DAL_URL}/grid-orders",
                                 params={"coin": coin, "status": "FILLED"}, timeout=10)
            resp.raise_for_status()
            filled_orders.extend(resp.json())
        except Exception as e:
            log.warning(f"/status filled-orders query mislukt voor {coin}: {e}")

    funding_by_cfg: dict[int, float] = {}
    for cfg in configs:
        try:
            resp = _requests.get(f"{_DAL_URL}/grid-funding/total",
                                 params={"coin": cfg["coin"],
                                         "grid_config_id": cfg["id"]}, timeout=10)
            resp.raise_for_status()
            funding_by_cfg[cfg["id"]] = float(resp.json().get("total_usdc") or 0)
        except Exception as e:
            log.warning(f"/status funding query mislukt voor config {cfg['id']}: {e}")

    trades_by_cfg: dict[int, list] = {}
    for cfg in configs:
        try:
            resp = _requests.get(f"{_DAL_URL}/grid-trades",
                                 params={"coin": cfg["coin"],
                                         "grid_config_id": cfg["id"]}, timeout=10)
            resp.raise_for_status()
            trades_by_cfg[cfg["id"]] = resp.json()
        except Exception as e:
            log.warning(f"/status grid-trades query mislukt voor config {cfg['id']}: {e}")

    try:
        resp = _requests.get(f"{_CONNECTOR_URL}/mids", timeout=5)
        resp.raise_for_status()
        mids = resp.json()
    except Exception as e:
        log.warning(f"/status connector query mislukt: {e}")
        mids = {}

    parts = []
    if configs:
        parts.append(_format_grid_status(configs, orders, filled_orders, trades_by_cfg,
                                         mids, funding_by_cfg))
    _send_telegram("\n".join(parts) if parts else "📊 <b>Geen actieve grids</b>")


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
        log.info("Telegram command listener gestart (/status)")
    else:
        log.warning("Telegram credentials niet geconfigureerd — command listener overgeslagen")
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
        log.warning("Telegram credentials niet geconfigureerd — alert overgeslagen")
        return
    for chunk in _split_message(text):
        try:
            resp = _requests.post(
                f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage",
                json={"chat_id": _CHAT_ID, "text": chunk, "parse_mode": "HTML"},
                timeout=5,
            )
            if not resp.ok:
                log.error(f"Telegram send mislukt: {resp.status_code} {resp.text}")
            resp.raise_for_status()
        except _requests.HTTPError:
            pass  # already logged above
        except Exception as e:
            log.warning(f"Telegram send tijdelijk mislukt: {e}")


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
