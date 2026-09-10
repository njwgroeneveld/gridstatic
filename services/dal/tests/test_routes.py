import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
from src.main import app
from src.database import get_db


@pytest.fixture
def client():
    mock_db = MagicMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    yield TestClient(app), mock_db
    app.dependency_overrides.clear()


def test_health(client):
    c, _ = client
    assert c.get("/health").json() == {"status": "ok"}


def test_insert_grid_config(client):
    c, db = client
    db.insert_grid_config.return_value = {"id": 1, "coin": "BTC"}
    r = c.post("/grid-configs", json={
        "coin": "BTC", "upper": 68000, "lower": 57000, "num_lines": 10
    })
    assert r.status_code == 201
    assert r.json()["coin"] == "BTC"


def test_get_grid_orders_calls_db(client):
    c, db = client
    db.get_orders.return_value = []
    r = c.get("/grid-orders", params={"coin": "BTC", "status": "OPEN"})
    assert r.status_code == 200
    db.get_orders.assert_called_once_with("BTC", "OPEN")


def test_patch_grid_order_skips_none_fields(client):
    c, db = client
    db.patch_order.return_value = None
    r = c.patch("/grid-orders/5", json={"status": "FILLED"})
    assert r.status_code == 200
    db.patch_order.assert_called_once_with(5, {"status": "FILLED"})


def test_patch_grid_config_skips_none_fields(client):
    c, db = client
    db.patch_grid_config.return_value = None
    r = c.patch("/grid-configs/9", json={"upper": 82.0})
    assert r.status_code == 200
    db.patch_grid_config.assert_called_once_with(9, {"upper": 82.0})


