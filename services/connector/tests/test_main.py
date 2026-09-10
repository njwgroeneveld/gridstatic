from unittest.mock import patch
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


