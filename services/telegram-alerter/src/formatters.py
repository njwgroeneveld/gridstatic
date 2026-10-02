import html


def _grid_label(payload: dict) -> str:
    coin = payload.get("coin", "?")
    num_lines = payload.get("num_lines")
    leverage = payload.get("leverage", 1)
    lev = f" {leverage}x" if leverage and leverage > 1 else ""
    lines = f" {num_lines}L" if num_lines else ""
    return f"{coin}{lines}{lev}"


def format_alert(alert_type: str, bot: str, payload: dict) -> str:
    coin = payload.get("coin", "?")

    if alert_type == "grid_initialized":
        lower = payload.get("lower", 0)
        upper = payload.get("upper", 0)
        num_lines = payload.get("num_lines", 0)
        return (f"🚀 <b>[{coin}] Grid initialised</b>\n"
                f"Range: ${lower:,.0f} – ${upper:,.0f}\n"
                f"Lines: {num_lines}")

    if alert_type == "buy_placed":
        price = payload.get("price", 0)
        level = payload.get("level", 0)
        return (f"📋 <b>[{coin}] BUY limit placed</b>\n"
                f"Price: ${price:,.2f} (level {level})")

    if alert_type == "buy_filled":
        filled_price = payload.get("filled_price", 0)
        sell_price = payload.get("sell_price", 0)
        label = _grid_label(payload)
        return (f"✅ <b>[{label}] BUY filled</b>\n"
                f"Fill: ${filled_price:,.2f} → SELL placed at ${sell_price:,.2f}")

    if alert_type == "trade_closed":
        buy_price = payload.get("buy_price", 0)
        sell_price = payload.get("sell_price", 0)
        profit = payload.get("profit_usd", 0)
        sign = "+" if profit >= 0 else ""
        label = _grid_label(payload)
        return (f"💰 <b>[{label}] Trade CLOSED</b>\n"
                f"Buy: ${buy_price:,.2f} → Sell: ${sell_price:,.2f}\n"
                f"P&amp;L: {sign}${profit:.2f}")

    if alert_type == "grid_hold":
        reason = html.escape(str(payload.get("reason", "unknown")))
        label = _grid_label(payload)
        return (f"⏸ <b>[{label}] Grid on hold — no new buys</b>\n"
                f"{reason}\n"
                f"Sells still go out. The grid resumes by itself once the position adds up.")

    if alert_type == "hold_cleared":
        return f"▶️ <b>[{_grid_label(payload)}] Hold cleared</b> — buying again"

    if alert_type == "edge_warning":
        side = payload.get("side", "?")
        level_price = payload.get("level_price", 0)
        label = _grid_label(payload)
        return (f"⚠️ <b>[{label}] Edge reached ({side})</b>\n"
                f"Last level: ${level_price:,.2f}")

    if alert_type == "outside_grid":
        price = payload.get("price", 0)
        bound = payload.get("bound", 0)
        side = payload.get("side", "?")
        label = _grid_label(payload)
        return (f"🚨 <b>[{label}] Price outside grid ({side})</b>\n"
                f"Price: ${price:,.2f} | Bound: ${bound:,.2f}")

    if alert_type == "error":
        msg = html.escape(str(payload.get("message", "unknown error")))
        return f"❌ <b>[{bot}] Error</b>\n{msg}"

    return f"[{bot}] {alert_type}: {html.escape(str(payload))}"


def _cell_line(c: dict, price: float | None) -> str:
    state = c["state"]
    if state == "sell":
        text = f"💰 C{c['cell']}  SELL ${c['sell_price']:,.0f} ({c['sell_sz']:g})"
    elif state == "unsold":
        text = f"⚠️ C{c['cell']}  UNSOLD {c['unsold']:g} → ${c['sell_price']:,.0f}"
    elif state == "buy":
        text = f"📋 C{c['cell']}  BUY ${c['buy_price']:,.0f} ({c['buy_sz']:g})"
    else:
        text = f"▫️ C{c['cell']}  ${c['buy_price']:,.0f}"
    if price is not None and c["buy_price"] <= price < c["sell_price"]:
        text += "  ← now"
    return "  " + text


def format_grid_status(grids: list[dict]) -> str:
    """What grid-static saw in each grid's last round, one cell per line, the
    highest cell first so the list reads like a price ladder."""
    if not grids:
        return "📊 <b>No active grids</b>"

    lines = ["📊 <b>GRID STATUS</b>"]
    for g in grids:
        lev = g.get("leverage", 1)
        lev_tag = f" {lev}x" if lev and lev > 1 else ""
        lines.append(f"\n<b>── {g['coin']} {g['num_lines']}L{lev_tag} ──</b>")
        lines.append(f"${g['lower']:,.0f} ↔ ${g['upper']:,.0f} | ${g['size_usd']:,.2f} per line")

        if not g.get("cells"):
            lines.append("No round yet — the grid is starting up.")
            continue

        price = g.get("price")
        lines.append(f"Now: ${price:,.2f} | Position: {g.get('position') or 0:g} | "
                     f"Last 24h: {g.get('realized_24h') or 0:+.2f}$")
        if g.get("hold"):
            lines.append(f"⏸ <b>ON HOLD</b> — no new buys: {html.escape(g['hold'])}")
        for c in sorted(g["cells"], key=lambda c: -c["cell"]):
            lines.append(_cell_line(c, price))
    return "\n".join(lines)
