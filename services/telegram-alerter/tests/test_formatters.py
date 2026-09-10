from src.formatters import format_alert


def test_grid_initialized():
    msg = format_alert("grid_initialized", "grid-static",
                       {"coin": "BTC", "lower": 57000, "upper": 68000, "num_lines": 10, "shadow": False})
    assert "BTC" in msg
    assert "57,000" in msg or "57.000" in msg
    assert "🚀" in msg


def test_buy_filled():
    msg = format_alert("buy_filled", "grid-static",
                       {"coin": "BTC", "filled_price": 61234.5, "sell_price": 62456.7, "shadow": False})
    assert "✅" in msg
    assert "61,234" in msg or "61.234" in msg


def test_trade_closed_profit():
    msg = format_alert("trade_closed", "grid-static",
                       {"coin": "BTC", "buy_price": 61000, "sell_price": 62222,
                        "profit_usd": 15.30, "shadow": False})
    assert "💰" in msg
    assert "+$15.30" in msg


def test_shadow_prefix():
    msg = format_alert("buy_placed", "grid-static",
                       {"coin": "BTC", "price": 61000, "level": 4, "shadow": True})
    assert msg.startswith("[SHADOW]")


def test_edge_warning():
    msg = format_alert("edge_warning", "grid-static",
                       {"coin": "BTC", "side": "bottom", "level_price": 57000})
    assert "⚠️" in msg


def test_outside_grid():
    msg = format_alert("outside_grid", "grid-static",
                       {"coin": "BTC", "price": 55000, "bound": 57000, "side": "bottom"})
    assert "🚨" in msg


def test_funding_regel_staat_naast_de_winst_niet_erin():
    """Funding wordt per uur over de netto positie afgerekend en is niet aan een
    losse trade toe te rekenen, dus het hoort niet in profit_usd te verdwijnen.
    De regel toont het apart en zet er het echte nettoresultaat naast."""
    from src.main import _funding_regel

    regel = _funding_regel({15: -1.74}, 15, realized=3.46)

    assert "Funding: -1.74$" in regel
    assert "Netto: +1.72$" in regel


def test_geen_funding_regel_zonder_funding():
    """Shadow-grids houden geen positie aan waarover funding wordt gerekend, dus
    daar hoort de regel helemaal niet te verschijnen."""
    from src.main import _funding_regel

    assert _funding_regel({}, 4, realized=1.78) == ""
    assert _funding_regel({4: 0.0}, 4, realized=1.78) == ""
