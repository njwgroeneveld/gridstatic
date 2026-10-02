from src.formatters import format_alert, format_grid_status


def test_grid_initialized():
    msg = format_alert("grid_initialized", "grid-static",
                       {"coin": "BTC", "lower": 57000, "upper": 68000, "num_lines": 10})
    assert "BTC" in msg
    assert "57,000" in msg or "57.000" in msg
    assert "🚀" in msg


def test_buy_filled():
    msg = format_alert("buy_filled", "grid-static",
                       {"coin": "BTC", "filled_price": 61234.5, "sell_price": 62456.7})
    assert "✅" in msg
    assert "61,234" in msg or "61.234" in msg


def test_trade_closed_profit():
    msg = format_alert("trade_closed", "grid-static",
                       {"coin": "BTC", "buy_price": 61000, "sell_price": 62222,
                        "profit_usd": 15.30})
    assert "💰" in msg
    assert "+$15.30" in msg


def test_no_shadow_prefix_any_more():
    # Shadow mode is gone; an old payload that still says so must not suggest
    # the order was simulated.
    msg = format_alert("buy_filled", "grid-static",
                       {"coin": "BTC", "filled_price": 1, "sell_price": 2, "shadow": True})
    assert "SHADOW" not in msg


def test_edge_warning():
    msg = format_alert("edge_warning", "grid-static",
                       {"coin": "BTC", "side": "bottom", "level_price": 57000})
    assert "⚠️" in msg


def test_outside_grid():
    msg = format_alert("outside_grid", "grid-static",
                       {"coin": "BTC", "price": 55000, "bound": 57000, "side": "bottom"})
    assert "🚨" in msg


def test_grid_hold_names_the_reason():
    msg = format_alert("grid_hold", "grid-static",
                       {"coin": "BTC", "num_lines": 20, "leverage": 3,
                        "reason": "position 0.5 has 0.5 that no cell accounts for"})
    assert "hold" in msg.lower()
    assert "no cell accounts for" in msg
    assert "BTC 20L 3x" in msg


def test_grid_hold_escapes_html():
    msg = format_alert("grid_hold", "grid-static", {"coin": "BTC", "reason": "a < b & c"})
    assert "a &lt; b &amp; c" in msg


def test_hold_cleared():
    msg = format_alert("hold_cleared", "grid-static", {"coin": "BTC", "num_lines": 20})
    assert "BTC" in msg and "cleared" in msg.lower()


# ── /status ────────────────────────────────────────────────────────────────────

def _cell(i, state, buy_sz=0.0, sell_sz=0.0, unsold=0.0):
    return {"cell": i, "buy_price": 100.0 + 10 * i, "sell_price": 110.0 + 10 * i,
            "buy_sz": buy_sz, "sell_sz": sell_sz, "unsold": unsold, "state": state}


def _grid(**over):
    g = {"coin_key": "BTC-4", "coin": "BTC", "lower": 100.0, "upper": 140.0, "num_lines": 5,
         "leverage": 1, "size_usd": 50.0, "price": 125.0, "position": 0.42, "hold": None,
         "residual": 0.0, "realized_24h": 3.77, "last_round_ms": 1,
         "cells": [_cell(0, "buy", buy_sz=0.5), _cell(1, "sell", sell_sz=0.42),
                   _cell(2, "unsold", unsold=0.05), _cell(3, "empty")]}
    g.update(over)
    return g


def test_status_shows_every_cell_top_down():
    text = format_grid_status([_grid()])
    rows = [l for l in text.splitlines() if l.strip().startswith(("📋", "💰", "⚠️", "▫️"))]
    assert len(rows) == 4
    assert "C3" in rows[0] and "C0" in rows[-1]


def test_status_shows_each_cell_state():
    text = format_grid_status([_grid()])
    assert "BUY $100" in text
    assert "SELL $120" in text           # cell 1 sells at the line above it
    assert "UNSOLD 0.05" in text


def test_status_marks_the_price():
    text = format_grid_status([_grid()])
    now = [l for l in text.splitlines() if "← now" in l]
    assert len(now) == 1 and "C2" in now[0]   # 125 sits between 120 and 130


def test_status_shows_position_and_last_24h():
    text = format_grid_status([_grid()])
    assert "0.42" in text
    assert "+3.77$" in text


def test_status_shows_a_hold_prominently():
    text = format_grid_status([_grid(hold="sells exceed the position")])
    assert "ON HOLD" in text
    assert "sells exceed the position" in text


def test_status_of_a_grid_that_has_not_run_a_round():
    text = format_grid_status([_grid(cells=[], price=None, position=None, last_round_ms=None)])
    assert "no round yet" in text.lower()


def test_status_without_grids():
    assert "No active grids" in format_grid_status([])
