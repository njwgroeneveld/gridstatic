def _grid_label(payload: dict) -> str:
    coin = payload.get("coin", "?")
    num_lines = payload.get("num_lines")
    leverage = payload.get("leverage", 1)
    lev = f" {leverage}x" if leverage and leverage > 1 else ""
    lines = f" {num_lines}L" if num_lines else ""
    return f"{coin}{lines}{lev}"


def format_alert(alert_type: str, bot: str, payload: dict) -> str:
    shadow = payload.get("shadow", False)
    prefix = "[SHADOW] " if shadow else ""
    coin = payload.get("coin", "?")

    if alert_type == "grid_initialized":
        lower = payload.get("lower", 0)
        upper = payload.get("upper", 0)
        num_lines = payload.get("num_lines", 0)
        return (f"{prefix}🚀 <b>[{coin}] Grid geïnitialiseerd</b>\n"
                f"Range: ${lower:,.0f} – ${upper:,.0f}\n"
                f"Lines: {num_lines}")

    if alert_type == "buy_placed":
        price = payload.get("price", 0)
        level = payload.get("level", 0)
        return (f"{prefix}📋 <b>[{coin}] BUY limit gezet</b>\n"
                f"Prijs: ${price:,.2f} (level {level})")

    if alert_type == "buy_filled":
        filled_price = payload.get("filled_price", 0)
        sell_price = payload.get("sell_price", 0)
        label = _grid_label(payload)
        return (f"{prefix}✅ <b>[{label}] BUY gevuld</b>\n"
                f"Fill: ${filled_price:,.2f} → SELL gezet op ${sell_price:,.2f}")

    if alert_type == "trade_closed":
        buy_price = payload.get("buy_price", 0)
        sell_price = payload.get("sell_price", 0)
        profit = payload.get("profit_usd", 0)
        sign = "+" if profit >= 0 else ""
        label = _grid_label(payload)
        return (f"{prefix}💰 <b>[{label}] Trade CLOSED</b>\n"
                f"Buy: ${buy_price:,.2f} → Sell: ${sell_price:,.2f}\n"
                f"P&L: {sign}${profit:.2f}")

    if alert_type == "edge_warning":
        side = payload.get("side", "?")
        level_price = payload.get("level_price", 0)
        label = _grid_label(payload)
        return (f"⚠️ <b>[{label}] Rand bereikt ({side})</b>\n"
                f"Laatste level: ${level_price:,.2f}")

    if alert_type == "outside_grid":
        price = payload.get("price", 0)
        bound = payload.get("bound", 0)
        side = payload.get("side", "?")
        label = _grid_label(payload)
        return (f"🚨 <b>[{label}] Prijs buiten grid ({side})</b>\n"
                f"Prijs: ${price:,.2f} | Grens: ${bound:,.2f}")

    if alert_type == "error":
        msg = payload.get("message", "onbekende fout")
        return f"❌ <b>[{bot}] Fout</b>\n{msg}"

    return f"[{bot}] {alert_type}: {payload}"
