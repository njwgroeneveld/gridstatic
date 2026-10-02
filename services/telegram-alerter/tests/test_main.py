import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


def _get_client():
    from src.main import app
    return TestClient(app)


def test_health():
    client = _get_client()
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_post_alert_buy_placed_returns_ok():
    client = _get_client()
    with patch("src.main._send_telegram") as mock_send:
        resp = client.post("/alert", json={
            "type": "buy_placed",
            "bot": "grid-static",
            "payload": {
                "coin": "BTC",
                "price": 60000.0,
                "level": 3,
            },
        })
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    mock_send.assert_called_once()
    text = mock_send.call_args[0][0]
    assert "BTC" in text
    assert "BUY limit placed" in text


def test_post_alert_unknown_type_uses_fallback():
    client = _get_client()
    with patch("src.main._send_telegram") as mock_send:
        resp = client.post("/alert", json={
            "type": "some_new_type",
            "bot": "future_bot",
            "payload": {"key": "value"},
        })
    assert resp.status_code == 200
    mock_send.assert_called_once()
    text = mock_send.call_args[0][0]
    assert "some_new_type" in text
    assert "future_bot" in text


def test_post_alert_empty_payload_no_crash():
    client = _get_client()
    with patch("src.main._send_telegram"):
        resp = client.post("/alert", json={
            "type": "bot_error",
            "bot": "sats",
            "payload": {},
        })
    assert resp.status_code == 200


def test_post_alert_telegram_failure_still_returns_ok():
    client = _get_client()
    with patch("src.main._send_telegram", side_effect=Exception("boom")):
        resp = client.post("/alert", json={
            "type": "bot_error",
            "bot": "sats",
            "payload": {"error_msg": "test", "consecutive_count": 1},
        })
    assert resp.status_code == 200


def test_send_telegram_no_credentials_no_crash():
    import src.main as m
    original_token, original_chat = m._BOT_TOKEN, m._CHAT_ID
    m._BOT_TOKEN = None
    m._CHAT_ID = None
    try:
        m._send_telegram("test message")  # must not raise
    finally:
        m._BOT_TOKEN = original_token
        m._CHAT_ID = original_chat


def test_handle_status_formats_what_grid_static_reports():
    import src.main as m
    report = {"grids": [{"coin_key": "BTC-4", "coin": "BTC", "lower": 100.0, "upper": 140.0,
                         "num_lines": 5, "leverage": 1, "size_usd": 50.0, "price": 125.0,
                         "position": 0.0, "hold": None, "residual": 0.0, "realized_24h": 0.0,
                         "last_round_ms": 1, "cells": []}]}
    resp = MagicMock()
    resp.json.return_value = report
    with patch("src.main._requests.get", return_value=resp) as get,          patch("src.main._send_telegram") as send:
        m._handle_status()
    assert get.call_args.args[0] == f"{m._GRID_STATIC_URL}/status"
    assert get.call_count == 1          # one call; no database, no connector
    assert "BTC 5L" in send.call_args.args[0]


def test_handle_status_when_grid_static_is_unreachable():
    import src.main as m
    with patch("src.main._requests.get", side_effect=ConnectionError("down")),          patch("src.main._send_telegram") as send:
        m._handle_status()
    assert "Could not reach grid-static" in send.call_args.args[0]


def test_send_telegram_api_error_no_crash():
    import src.main as m
    import requests
    original_token, original_chat = m._BOT_TOKEN, m._CHAT_ID
    m._BOT_TOKEN = "fake_token"
    m._CHAT_ID = "fake_chat"
    try:
        with patch("src.main._requests.post", side_effect=requests.RequestException("timeout")):
            m._send_telegram("test message")  # must not raise
    finally:
        m._BOT_TOKEN = original_token
        m._CHAT_ID = original_chat
