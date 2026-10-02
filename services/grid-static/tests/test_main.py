import pytest
from unittest.mock import AsyncMock, MagicMock

import src.main as m


def _grid(**over):
    g = {"coin": "BTC", "active": True, "allocation_pct": 100,
         "upper": 83000, "lower": 75000, "num_lines": 20, "leverage": 3}
    g.update(over)
    return g


def _cfg(coins, **top):
    cfg = {"connector_url": "http://c:8080", "alerter_url": "http://a:8080",
           "start_balance": 1000, "strategy_allocation_pct": 80, "coins": coins}
    cfg.update(top)
    return cfg


# ── refusing to start ──────────────────────────────────────────────────────────

def test_a_valid_config_passes():
    grids, skipped = m._validate(_cfg({"BTC-20": _grid()}))
    assert [k for k, _, _ in grids] == ["BTC-20"]
    assert skipped == []


def test_shadow_true_refuses_to_start():
    # Shadow mode is gone. Running anyway would place real orders for someone
    # who believes they are simulating.
    with pytest.raises(m.ConfigError, match="shadow mode was removed"):
        m._validate(_cfg({"BTC-20": _grid(shadow=True)}))


def test_shadow_false_is_accepted():
    m._validate(_cfg({"BTC-20": _grid(shadow=False)}))


def test_two_grids_on_one_coin_refuse_to_start():
    with pytest.raises(m.ConfigError, match="both trade BTC"):
        m._validate(_cfg({"BTC-20": _grid(), "BTC-10": _grid(num_lines=10)}))


def test_an_order_under_the_minimum_refuses_to_start():
    # $100 * 80% / 20 lines = $4 per line, at 1x a $4 order.
    with pytest.raises(m.ConfigError, match="minimum"):
        m._validate(_cfg({"BTC-20": _grid(leverage=1)}, start_balance=100))


def test_leverage_counts_toward_the_minimum():
    # $4 a line at 3x is a $12 order.
    m._validate(_cfg({"BTC-20": _grid(leverage=3)}, start_balance=100))


def test_a_missing_start_balance_refuses_to_start():
    cfg = _cfg({"BTC-20": _grid()})
    del cfg["start_balance"]
    with pytest.raises(m.ConfigError, match="no start_balance"):
        m._validate(cfg)


def test_a_start_balance_per_grid_wins():
    grids, _ = m._validate(_cfg({"BTC-20": _grid(start_balance=5000)}))
    assert grids[0][2] == 5000


def test_every_problem_is_reported_at_once():
    with pytest.raises(m.ConfigError) as e:
        m._validate(_cfg({"BTC-20": _grid(shadow=True), "BTC-10": _grid(num_lines=10)}))
    assert "shadow" in str(e.value) and "both trade BTC" in str(e.value)


def test_inactive_grids_are_left_out():
    grids, _ = m._validate(_cfg({"BTC-20": _grid(), "ETH-20": _grid(coin="ETH", active=False)}))
    assert [k for k, _, _ in grids] == ["BTC-20"]


def test_other_strategy_types_are_skipped_not_fatal():
    grids, skipped = m._validate(_cfg({"BTC-20": _grid(),
                                       "SOL-T": _grid(coin="SOL", strategy_type="TRAILING")}))
    assert [k for k, _, _ in grids] == ["BTC-20"]
    assert "TRAILING" in skipped[0]


# ── waiting for the connector ──────────────────────────────────────────────────

async def test_wait_for_returns_as_soon_as_the_probe_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(m.asyncio, "sleep", AsyncMock(side_effect=lambda d: sleeps.append(d)))
    attempts = {"n": 0}

    async def probe():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("not yet")

    await m._wait_for("connector", probe)
    assert attempts["n"] == 3
    assert sleeps == [2.0, 2.0]


async def test_wait_for_gives_up_loudly_after_all_attempts(monkeypatch):
    monkeypatch.setattr(m.asyncio, "sleep", AsyncMock())

    async def probe():
        raise ConnectionError("still down")

    with pytest.raises(RuntimeError, match="connector not reachable after 60s"):
        await m._wait_for("connector", probe, attempts=30, delay=2.0)


# ── the app ────────────────────────────────────────────────────────────────────

def _settings(tmp_path, monkeypatch, text):
    settings = tmp_path / "settings.yaml"
    settings.write_text(text, encoding="utf-8")
    monkeypatch.setenv("SETTINGS_FILE", str(settings))


_VALID = (
    "connector_url: http://c:8080\n"
    "alerter_url: http://a:8080\n"
    "start_balance: 1000\n"
    "coins:\n"
    "  BTC-20:\n"
    "    coin: BTC\n"
    "    active: true\n"
    "    allocation_pct: 100\n"
    "    upper: 83000\n"
    "    lower: 75000\n"
    "    num_lines: 20\n"
    "    leverage: 3\n"
)


async def test_lifespan_waits_for_the_connector_and_runs_one_loop_per_grid(monkeypatch, tmp_path):
    _settings(tmp_path, monkeypatch, _VALID)
    waited_for = []

    async def fake_wait(name, probe, **kwargs):
        waited_for.append(name)

    monkeypatch.setattr(m, "_wait_for", fake_wait)
    monkeypatch.setattr(m, "AlerterClient", MagicMock(return_value=AsyncMock()))
    grid = MagicMock()
    grid.run_loop = AsyncMock()
    monkeypatch.setattr(m, "StaticGrid", MagicMock(return_value=grid))
    m._grids.clear()
    try:
        async with m.lifespan(None):
            pass
    finally:
        m._grids.clear()
    assert waited_for == ["connector"]          # no database to wait for any more
    grid.run_loop.assert_called_once()        # started as a task; the exit cancels it


async def test_lifespan_refuses_a_bad_config_before_touching_anything(monkeypatch, tmp_path):
    _settings(tmp_path, monkeypatch, _VALID.replace("    leverage: 3\n",
                                                     "    leverage: 3\n    shadow: true\n"))
    connector = MagicMock()
    monkeypatch.setattr(m, "ConnectorClient", connector)
    with pytest.raises(m.ConfigError):
        async with m.lifespan(None):
            pass
    connector.assert_not_called()


def test_status_route_reports_every_grid(monkeypatch):
    from fastapi.testclient import TestClient
    grid = MagicMock()
    grid.status.return_value = {"coin": "BTC", "hold": None, "cells": []}
    monkeypatch.setattr(m, "_grids", [grid])
    # Bypass the lifespan: it would read settings and start loops.
    client = TestClient(m.app)
    r = client.get("/status")
    assert r.status_code == 200
    assert r.json() == {"grids": [{"coin": "BTC", "hold": None, "cells": []}]}
