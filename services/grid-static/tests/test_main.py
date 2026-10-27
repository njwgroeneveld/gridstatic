import pytest
from unittest.mock import AsyncMock, MagicMock

import src.main as m


def _grid(**over):
    g = {"coin": "BTC", "active": True, "allocation_pct": 100,
         "upper": 83000, "lower": 75000, "num_lines": 20, "leverage": 3}
    g.update(over)
    return g


def _cfg(coins, **top):
    cfg = {"connector_url": "http://c:8080", "alerter_url": "http://a:8080", "coins": coins}
    cfg.update(top)
    return cfg


# ── refusing to start ──────────────────────────────────────────────────────────

def test_a_valid_config_passes():
    grids, skipped = m._validate(_cfg({"BTC-20": _grid()}))
    assert [k for k, _ in grids] == ["BTC-20"]
    assert skipped == []


def test_two_grids_on_one_coin_refuse_to_start():
    with pytest.raises(m.ConfigError, match="both trade BTC"):
        m._validate(_cfg({"BTC-20": _grid(), "BTC-10": _grid(num_lines=10)}))


@pytest.mark.parametrize("pct", [0, -5, 101, None, "90"])
def test_allocation_pct_must_be_between_0_and_100(pct):
    with pytest.raises(m.ConfigError, match="allocation_pct"):
        m._validate(_cfg({"BTC-20": _grid(allocation_pct=pct)}))


def test_grids_on_one_dex_may_not_use_more_than_all_of_it():
    with pytest.raises(m.ConfigError, match="default dex.*110"):
        m._validate(_cfg({"BTC-20": _grid(allocation_pct=60),
                          "ETH-20": _grid(coin="ETH", allocation_pct=50)}))


def test_grids_on_different_dexes_each_have_their_own_100():
    grids, _ = m._validate(_cfg({"BTC-20": _grid(allocation_pct=90),
                                 "NDX-20": _grid(coin="xyz:XYZ100", allocation_pct=90,
                                                 upper=32000, lower=29000)}))
    assert [k for k, _ in grids] == ["BTC-20", "NDX-20"]


def test_every_problem_is_reported_at_once():
    with pytest.raises(m.ConfigError) as e:
        m._validate(_cfg({"BTC-20": _grid(allocation_pct=150), "BTC-10": _grid(num_lines=10)}))
    assert "allocation_pct" in str(e.value) and "both trade BTC" in str(e.value)


def test_inactive_grids_are_left_out():
    grids, _ = m._validate(_cfg({"BTC-20": _grid(), "ETH-20": _grid(coin="ETH", active=False)}))
    assert [k for k, _ in grids] == ["BTC-20"]


def test_other_strategy_types_are_skipped_not_fatal():
    grids, skipped = m._validate(_cfg({"BTC-20": _grid(),
                                       "SOL-T": _grid(coin="SOL", strategy_type="TRAILING")}))
    assert [k for k, _ in grids] == ["BTC-20"]
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
                                                     "    leverage: 3\n    stop_loss: 999999\n"))
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


@pytest.mark.parametrize("stop", [75000, 76000, 0, -5, "low"])
def test_a_stop_loss_not_below_the_grid_refuses_to_start(stop):
    with pytest.raises(m.ConfigError, match="stop_loss"):
        m._validate(_cfg({"BTC-20": _grid(stop_loss=stop)}))


def test_a_stop_loss_below_the_grid_is_accepted():
    grids, _ = m._validate(_cfg({"BTC-20": _grid(stop_loss=72000)}))
    assert grids[0][1]["stop_loss"] == 72000


@pytest.mark.parametrize("lev", [None, 0, -2, "3x", True])
def test_an_invalid_leverage_refuses_to_start(lev):
    # An empty leverage used to crash the start with a TypeError instead of
    # saying what is wrong.
    with pytest.raises(m.ConfigError, match="leverage"):
        m._validate(_cfg({"BTC-20": _grid(leverage=lev)}))


def test_leverage_left_out_means_1x():
    g = _grid()
    del g["leverage"]
    grids, _ = m._validate(_cfg({"BTC-20": g}))
    assert grids[0][0] == "BTC-20"


@pytest.mark.parametrize("cap", [0, -100, "500", True])
def test_max_equity_usd_must_be_a_positive_number(cap):
    with pytest.raises(m.ConfigError, match="max_equity_usd"):
        m._validate(_cfg({"BTC-20": _grid(max_equity_usd=cap)}))


def test_max_equity_usd_is_optional():
    grids, _ = m._validate(_cfg({"BTC-20": _grid(max_equity_usd=500)}))
    assert grids[0][1]["max_equity_usd"] == 500


def test_stop_route_stops_every_grid(monkeypatch):
    from fastapi.testclient import TestClient
    grid = MagicMock()
    grid.shutdown = AsyncMock(return_value={"coin": "BTC", "cancelled": 3, "failed": []})
    monkeypatch.setattr(m, "_grids", [grid])
    r = TestClient(m.app).post("/stop")
    assert r.status_code == 200
    assert r.json()["stopped"] is True
    grid.shutdown.assert_awaited_once()


def test_stop_route_fails_when_a_grid_could_not_cancel_everything(monkeypatch):
    # The pre-delete hook fails on a 500, and with it helm uninstall: orders that
    # stayed behind must not go unnoticed.
    from fastapi.testclient import TestClient
    ok = MagicMock()
    ok.shutdown = AsyncMock(return_value={"coin": "BTC", "cancelled": 3, "failed": []})
    unreadable = MagicMock(coin_key="NDX", coin="xyz:XYZ100")
    unreadable.shutdown = AsyncMock(side_effect=RuntimeError("timeout"))
    monkeypatch.setattr(m, "_grids", [ok, unreadable])
    r = TestClient(m.app).post("/stop")
    assert r.status_code == 500
    assert r.json()["stopped"] is False
    assert "timeout" in r.json()["grids"][1]["error"]
    ok.shutdown.assert_awaited_once()            # the other grid still stopped


# ── switched-off grids on uninstall ───────────────────────────────────────────

def _tagged(oid, side="BUY"):
    from src.cloid import encode, fingerprint
    return {"coin": "xyz:XYZ100", "oid": oid, "cloid": encode(fingerprint("xyz:XYZ100", 29000, 32000, 10, 5), 1, side)}


def _clients_with(orders, position=0.0):
    connector = AsyncMock()
    connector.get_open_orders.return_value = orders
    connector.get_account.return_value = (100.0, {"xyz:XYZ100": {"szi": position}} if position else {})
    connector.cancel_order.return_value = {"status": "ok"}
    return {"connector": connector, "alerter": AsyncMock()}


def test_stop_route_also_cancels_the_orders_of_a_switched_off_grid(monkeypatch):
    # Switched off, a grid runs no rounds -- but the orders it placed before may
    # still rest on the exchange, and an uninstall should not leave them there.
    from fastapi.testclient import TestClient
    clients = _clients_with([_tagged(77), {"coin": "xyz:XYZ100", "oid": 78, "cloid": None}])
    monkeypatch.setattr(m, "_grids", [])
    monkeypatch.setattr(m, "_switched_off", [("NASDAQ", "xyz:XYZ100")])
    monkeypatch.setattr(m, "_clients", clients)
    r = TestClient(m.app).post("/stop")
    assert r.status_code == 200
    clients["connector"].cancel_order.assert_awaited_once_with("xyz:XYZ100", 77)   # not the untagged one
    assert r.json()["grids"][0]["switched_off"] is True
    assert "switched off" in clients["alerter"].send_alert.call_args.args[1]["message"]


def test_a_switched_off_grid_keeps_its_stop_while_there_is_a_position(monkeypatch):
    from fastapi.testclient import TestClient
    clients = _clients_with([_tagged(77), _tagged(79, side="STOP")], position=0.0004)
    monkeypatch.setattr(m, "_grids", [])
    monkeypatch.setattr(m, "_switched_off", [("NASDAQ", "xyz:XYZ100")])
    monkeypatch.setattr(m, "_clients", clients)
    r = TestClient(m.app).post("/stop")
    clients["connector"].cancel_order.assert_awaited_once_with("xyz:XYZ100", 77)
    assert r.json()["grids"][0]["kept_stop"] is True


def test_a_coin_with_a_running_grid_is_not_cleared_twice(monkeypatch):
    from fastapi.testclient import TestClient
    running = MagicMock(coin_key="NDX-20", coin="xyz:XYZ100")
    running.shutdown = AsyncMock(return_value={"coin": "xyz:XYZ100", "cancelled": 1, "failed": []})
    clients = _clients_with([_tagged(77)])
    monkeypatch.setattr(m, "_grids", [running])
    monkeypatch.setattr(m, "_switched_off", [("NASDAQ-OLD", "xyz:XYZ100")])
    monkeypatch.setattr(m, "_clients", clients)
    TestClient(m.app).post("/stop")
    clients["connector"].get_open_orders.assert_not_awaited()


def test_stop_route_fails_when_a_switched_off_grids_book_is_unreadable(monkeypatch):
    from fastapi.testclient import TestClient
    clients = _clients_with([])
    clients["connector"].get_open_orders.side_effect = RuntimeError("timeout")
    monkeypatch.setattr(m, "_grids", [])
    monkeypatch.setattr(m, "_switched_off", [("NASDAQ", "xyz:XYZ100")])
    monkeypatch.setattr(m, "_clients", clients)
    r = TestClient(m.app).post("/stop")
    assert r.status_code == 500
    assert "timeout" in r.json()["grids"][0]["error"]


async def test_lifespan_remembers_the_switched_off_grids(monkeypatch, tmp_path):
    _settings(tmp_path, monkeypatch, _VALID + (
        "  NASDAQ:\n"
        "    coin: xyz:XYZ100\n"
        "    active: false\n"
        "    allocation_pct: 90\n"
        "    upper: 32000\n"
        "    lower: 29000\n"
        "    num_lines: 10\n"))

    async def fake_wait(name, probe, **kwargs):
        pass

    monkeypatch.setattr(m, "_wait_for", fake_wait)
    monkeypatch.setattr(m, "AlerterClient", MagicMock(return_value=AsyncMock()))
    grid = MagicMock()
    grid.run_loop = AsyncMock()
    monkeypatch.setattr(m, "StaticGrid", MagicMock(return_value=grid))
    m._grids.clear()
    try:
        async with m.lifespan(None):
            assert m._switched_off == [("NASDAQ", "xyz:XYZ100")]
    finally:
        m._grids.clear()
        m._switched_off.clear()
