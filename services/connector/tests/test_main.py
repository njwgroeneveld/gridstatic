from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from src.main import app

client = TestClient(app)


def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "timestamp" in data


def test_get_mids_returns_prices():
    with patch("src.main._info.get_all_mids", return_value={"BTC": 103500.0}):
        response = client.get("/mids")
    assert response.status_code == 200
    assert response.json()["BTC"] == 103500.0


def test_limit_order_route_passes_cloid_and_reduce_only():
    fake = MagicMock()
    fake.place_limit_order.return_value = {"status": "ok", "oid": 1, "cloid": "0xab",
                                           "sz": 0.01, "px": 100.0}
    with patch("src.main._ex", return_value=fake):
        r = client.post("/orders/limit", json={"coin": "BTC", "is_buy": False, "price": 100.0,
                                               "sz": 0.01, "cloid": "0xab", "reduce_only": True})
    assert r.status_code == 200
    fake.place_limit_order.assert_called_once_with("BTC", False, 100.0, 0.01, "0xab", True)


def test_limit_order_route_answers_502_on_a_rejection():
    fake = MagicMock()
    fake.place_limit_order.return_value = {"status": "error", "reason": "insufficient margin"}
    with patch("src.main._ex", return_value=fake):
        r = client.post("/orders/limit", json={"coin": "BTC", "is_buy": True,
                                               "price": 100.0, "sz": 0.01})
    assert r.status_code == 502
    assert "insufficient margin" in r.json()["detail"]


def test_order_status_route():
    fake = MagicMock()
    fake.get_order_status.return_value = {"oid": 7, "cloid": "0xab", "status": "filled"}
    with patch("src.main._ex", return_value=fake):
        r = client.get("/orders/status/7")
    assert r.status_code == 200
    assert r.json()["cloid"] == "0xab"
    fake.get_order_status.assert_called_once_with(7)


def test_order_status_route_answers_502_when_the_exchange_is_unreadable():
    fake = MagicMock()
    fake.get_order_status.side_effect = ConnectionError("down")
    with patch("src.main._ex", return_value=fake):
        r = client.get("/orders/status/7")
    assert r.status_code == 502


def test_positions_route_answers_502_when_the_exchange_is_unreadable():
    # An unreadable position must never look like "no position".
    fake = MagicMock()
    fake.get_open_positions.side_effect = ConnectionError("down")
    with patch("src.main._ex", return_value=fake):
        r = client.get("/positions")
    assert r.status_code == 502


@pytest.mark.parametrize("path", ["/orders/tp", "/orders/sl", "/positions/BTC/close"])
def test_routes_the_grid_never_used_are_gone(path):
    assert client.post(path, json={}).status_code in (404, 405)
