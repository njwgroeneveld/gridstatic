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


def test_funding_line_sits_next_to_profit_not_inside_it():
    """Funding is charged hourly on the net position and cannot be attributed to a
    single trade, so it must not disappear into profit_usd. The line shows it
    separately, with the real net result next to it."""
    from src.main import _funding_line

    line = _funding_line({15: -1.74}, 15, realized=3.46)

    assert "Funding: -1.74$" in line
    assert "Net: +1.72$" in line


def test_no_funding_line_without_funding():
    """Shadow grids hold no position that funding is charged on, so the line must not
    appear there at all."""
    from src.main import _funding_line

    assert _funding_line({}, 4, realized=1.78) == ""
    assert _funding_line({4: 0.0}, 4, realized=1.78) == ""
