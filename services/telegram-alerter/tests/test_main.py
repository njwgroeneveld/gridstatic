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
    assert "BUY limit gezet" in text


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


def test_handle_status_queries_only_static():
    # Deze stack kent alleen de static grid. /status hoort dus precies één
    # strategie op te vragen; vroeg hij ook TRAILING op, dan rapporteert hij over
    # grids die hier niet bestaan.
    import src.main as m

    def fake_get(url, params=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = [] if url.endswith("/mids") else []
        return resp

    with patch("src.main._requests.get", side_effect=fake_get) as mock_get, \
         patch("src.main._send_telegram"):
        m._handle_status()

    config_calls = [c for c in mock_get.call_args_list
                     if c.args and c.args[0].endswith("/grid-configs")]
    strategies_requested = {c.kwargs["params"]["strategy"] for c in config_calls}
    assert strategies_requested == {"STATIC"}


def test_format_grid_status_shows_strategy_label():
    from src.main import _format_grid_status

    configs = [{"id": 1, "coin": "SOL", "strategy": "TRAILING", "upper": 80, "lower": 71,
               "num_lines": 10, "shadow": True, "leverage": 1}]
    text = _format_grid_status(configs, [], [], {}, {"SOL": 75.0})
    assert "TRAILING" in text


def test_format_grid_status_matches_orders_by_price_not_level_index():
    # TrailingGrid stores an absolute, never-resetting level_seq (e.g. 37)
    # instead of a 0-indexed array position, so matching must go by price.
    from src.main import _format_grid_status

    configs = [{"id": 1, "coin": "SOL", "strategy": "TRAILING", "upper": 80, "lower": 71,
               "num_lines": 10, "shadow": True, "leverage": 1}]
    # level 3 in the current window is price 74.0; tag it with an unrelated level number
    orders = [{"grid_config_id": 1, "level": 37, "side": "BUY", "price": 74.0, "size_usd": 100.0}]
    text = _format_grid_status(configs, orders, [], {}, {"SOL": 75.0})

    assert "BUY $74" in text
    assert "▫️ L3" not in text


def test_handle_status_queries_grid_orders_with_explicit_status():
    # Regression test: /status must never fetch grid-orders without a status
    # filter, otherwise stale FILLED/CANCELLED rows can mask a currently OPEN
    # order at the same level in _format_grid_status.
    import src.main as m

    def fake_get(url, params=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        if url.endswith("/grid-configs"):
            resp.json.return_value = [{"id": 1, "coin": "BTC", "upper": 68000, "lower": 57000,
                                       "num_lines": 10, "shadow": True, "leverage": 1}]
        elif url.endswith("/mids"):
            resp.json.return_value = {"BTC": 60000.0}
        else:
            resp.json.return_value = []
        return resp

    with patch("src.main._requests.get", side_effect=fake_get) as mock_get, \
         patch("src.main._send_telegram"):
        m._handle_status()

    order_calls = [c for c in mock_get.call_args_list
                   if c.args and c.args[0].endswith("/grid-orders")]
    assert order_calls, "expected at least one /grid-orders call"
    for c in order_calls:
        assert c.kwargs["params"].get("status") in ("OPEN", "FILLED")


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
