import pytest
from unittest.mock import MagicMock, patch
from src.exchange_client import ExchangeClient


def _make_client() -> ExchangeClient:
    with patch("src.exchange_client.Account") as mock_acct, \
         patch("src.exchange_client.Info"), \
         patch("src.exchange_client.Exchange"):
        mock_acct.from_key.return_value = MagicMock(address="0xTEST")
        client = ExchangeClient(private_key="0x" + "a" * 64, testnet=True)
    client._info = MagicMock()
    client._exchange = MagicMock()
    return client


def test_get_open_positions_excludes_zero_szi():
    client = _make_client()
    client._info.user_state.return_value = {
        "assetPositions": [
            {"position": {"coin": "BTC", "szi": "0.01", "entryPx": "103000.0"}},
            {"position": {"coin": "ETH", "szi": "0",    "entryPx": "2450.0"}},
        ]
    }
    result = client.get_open_positions()
    assert "BTC" in result
    assert result["BTC"] == {"szi": 0.01, "entry_px": 103000.0}
    assert "ETH" not in result


def test_get_open_orders_filters_by_coin():
    client = _make_client()
    client._info.frontend_open_orders.return_value = [
        {"coin": "BTC", "oid": 1},
        {"coin": "ETH", "oid": 2},
    ]
    result = client.get_open_orders("BTC")
    assert len(result) == 1
    assert result[0]["oid"] == 1


def test_get_open_orders_raises_instead_of_reporting_an_empty_book():
    # Regression test: this used to return [] on any error, which recover_state
    # reads as "every resting order has filled" -- one API hiccup at pod start
    # and it books the whole book as filled and sells against positions that do
    # not exist. An unreadable exchange has to be loud.
    client = _make_client()
    client._info.frontend_open_orders.side_effect = RuntimeError("API down")
    with pytest.raises(RuntimeError):
        client.get_open_orders("BTC")


def test_get_fills_since_raises_instead_of_reporting_no_fills():
    client = _make_client()
    client._info.user_fills.side_effect = RuntimeError("API down")
    with pytest.raises(RuntimeError):
        client.get_fills_since("BTC", since_ms=0)


def test_get_fills_since_filters_by_coin_and_time():
    client = _make_client()
    client._info.user_fills.return_value = [
        {"coin": "BTC", "time": 1000, "oid": 1},
        {"coin": "BTC", "time": 500,  "oid": 2},
        {"coin": "ETH", "time": 2000, "oid": 3},
    ]
    result = client.get_fills_since("BTC", since_ms=600)
    assert len(result) == 1
    assert result[0]["oid"] == 1


def test_cancel_order_returns_ok():
    client = _make_client()
    client._exchange.cancel.return_value = {"status": "ok"}
    result = client.cancel_order("BTC", "12345")
    assert result == {"status": "ok"}
    client._exchange.cancel.assert_called_once_with("BTC", 12345)


def test_place_limit_order_returns_order_id():
    client = _make_client()
    client._info.meta.return_value = {"universe": [{"name": "BTC", "szDecimals": 4}]}
    client._exchange.order.return_value = {
        "status": "ok",
        "response": {"data": {"statuses": [{"resting": {"oid": 99}}]}},
    }
    result = client.place_limit_order("BTC", "BUY", 103000.0, 1000.0, leverage=3)
    assert result["status"] == "ok"
    assert result["hl_order_id"] == "99"


def test_place_limit_order_recovers_oid_via_open_orders():
    """OID ontbreekt in response → hersteld via open orders (order staat nog resting)."""
    client = _make_client()
    client._info.meta.return_value = {"universe": [{"name": "ETH", "szDecimals": 4}]}
    client._exchange.order.return_value = {
        "status": "ok",
        "response": {"data": {"statuses": [{"filled": {"totalSz": "0.5", "avgPx": "2000"}}]}},
    }
    client._info.frontend_open_orders.return_value = [
        {"coin": "ETH", "isBuy": True, "limitPx": "2000.0",
         "reduceOnly": False, "oid": 42},
    ]

    with patch("time.sleep"):
        result = client.place_limit_order("ETH", "BUY", 2000.0, 1000.0, leverage=3)

    assert result["status"] == "ok"
    assert result["hl_order_id"] == "42"


def test_place_limit_order_recovers_oid_via_fills():
    """OID ontbreekt in response, niet in open orders → hersteld via recente fills."""
    client = _make_client()
    client._info.meta.return_value = {"universe": [{"name": "ETH", "szDecimals": 4}]}
    client._exchange.order.return_value = {
        "status": "ok",
        "response": {"data": {"statuses": [{"filled": {"totalSz": "0.5", "avgPx": "2000"}}]}},
    }
    client._info.frontend_open_orders.return_value = []
    client._info.user_fills.return_value = [
        {"coin": "ETH", "side": "B", "time": 9999999999999, "oid": 77},
    ]

    with patch("time.sleep"):
        result = client.place_limit_order("ETH", "BUY", 2000.0, 1000.0, leverage=3)

    assert result["status"] == "ok"
    assert result["hl_order_id"] == "77"


def test_place_limit_order_oid_none_when_recovery_fails():
    """OID niet te herstellen → hl_order_id=None maar status=ok."""
    client = _make_client()
    client._info.meta.return_value = {"universe": [{"name": "ETH", "szDecimals": 4}]}
    client._exchange.order.return_value = {
        "status": "ok",
        "response": {"data": {"statuses": [{"filled": {"totalSz": "0.5", "avgPx": "2000"}}]}},
    }
    client._info.frontend_open_orders.return_value = []
    client._info.user_fills.return_value = []

    with patch("time.sleep"):
        result = client.place_limit_order("ETH", "BUY", 2000.0, 1000.0, leverage=3)

    assert result["status"] == "ok"
    assert result["hl_order_id"] is None


def test_recover_oid_ignores_reduce_only_orders():
    """TP orders (reduce_only=True) worden niet als entry OID gezien."""
    client = _make_client()
    client._info.frontend_open_orders.return_value = [
        {"coin": "ETH", "isBuy": True, "limitPx": "2000.0",
         "reduceOnly": True, "oid": 55},   # TP order — moet genegeerd worden
    ]
    client._info.user_fills.return_value = []

    with patch("time.sleep"):
        result = client._recover_oid("ETH", "BUY", 2000.0, 0)

    assert result is None


def test_place_limit_order_rondt_prijs_af_naar_hl_precisie():
    """Gridlevels als 75421.05 hebben 7 significante cijfers; HL accepteert er 5."""
    client = _make_client()
    client._info.meta.return_value = {"universe": [{"name": "BTC", "szDecimals": 5}]}
    client._exchange.order.return_value = {
        "status": "ok",
        "response": {"data": {"statuses": [{"resting": {"oid": 7}}]}},
    }
    result = client.place_limit_order("BTC", "BUY", 75421.05, 18.56, leverage=3)
    assert result["status"] == "ok"
    assert client._exchange.order.call_args[0][3] == 75421.0


def test_place_limit_order_meldt_afwijzing_als_fout():
    """HL antwoordt met status ok en de afwijzing in statuses[0] -- geen geslaagde order."""
    client = _make_client()
    client._info.meta.return_value = {"universe": [{"name": "BTC", "szDecimals": 5}]}
    client._exchange.order.return_value = {
        "status": "ok",
        "response": {"data": {"statuses": [{"error": "Order has invalid price."}]}},
    }
    result = client.place_limit_order("BTC", "BUY", 75421.05, 18.56, leverage=3)
    assert result["status"] == "error"
    assert "invalid price" in result["reden"]


def test_place_limit_order_multiplies_size_by_leverage():
    """De hefboom vergroot de positie. De leverage-instelling op de exchange
    verlaagt alleen de margin-eis en maakt de order zelf niet groter, dus de
    vermenigvuldiging moet hier gebeuren."""
    client = _make_client()
    client._info.meta.return_value = {"universe": [{"name": "BTC", "szDecimals": 5}]}
    client._exchange.order.return_value = {
        "status": "ok",
        "response": {"data": {"statuses": [{"resting": {"oid": 11}}]}},
    }

    client.place_limit_order("BTC", "BUY", 80000.0, 1000.0, leverage=1)
    sz_1x = client._exchange.order.call_args[0][2]

    client.place_limit_order("BTC", "BUY", 80000.0, 1000.0, leverage=3)
    sz_3x = client._exchange.order.call_args[0][2]

    assert sz_1x == 0.0125
    assert sz_3x == 0.0375
    assert sz_3x == round(sz_1x * 3, 5)
