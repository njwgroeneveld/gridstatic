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


# ── placing orders ─────────────────────────────────────────────────────────────

_OK_RESTING = {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 99}}]}}}
CLOID = "0x6773" + "01" + "aa" * 6 + "0003" + "01" + "deadbeef"


def _orderable_client(sz_decimals: int = 5) -> ExchangeClient:
    client = _make_client()
    client._info.meta.return_value = {"universe": [{"name": "BTC", "szDecimals": sz_decimals}]}
    client._exchange.order.return_value = _OK_RESTING
    return client


def test_place_limit_order_returns_the_oid_and_the_cloid():
    client = _orderable_client()
    result = client.place_limit_order("BTC", True, 80000.0, 0.0125, cloid=CLOID)
    assert result == {"status": "ok", "oid": 99, "cloid": CLOID, "sz": 0.0125, "px": 80000.0}


def test_place_limit_order_hands_the_cloid_to_the_exchange():
    # The cloid is how the bot recognises its own orders after a restart; an
    # order without it would be invisible to the grid.
    client = _orderable_client()
    client.place_limit_order("BTC", True, 80000.0, 0.0125, cloid=CLOID)
    sent = client._exchange.order.call_args.kwargs["cloid"]
    assert str(sent) == CLOID


def test_place_limit_order_without_cloid_sends_none():
    client = _orderable_client()
    client.place_limit_order("BTC", True, 80000.0, 0.0125)
    assert client._exchange.order.call_args.kwargs["cloid"] is None


def test_place_limit_order_passes_reduce_only_for_a_sell():
    # A reduce-only sell can never open a short, whatever the bot believes.
    client = _orderable_client()
    client.place_limit_order("BTC", False, 81000.0, 0.0125, cloid=CLOID, reduce_only=True)
    args, kwargs = client._exchange.order.call_args
    assert args[1] is False
    assert kwargs["reduce_only"] is True


def test_place_limit_order_takes_the_size_as_given_rounded_to_the_lot():
    # The bot sizes the order in coin; the connector no longer multiplies by
    # leverage -- doing it in both places would double every position.
    client = _orderable_client(sz_decimals=4)
    result = client.place_limit_order("BTC", True, 80000.0, 0.012345, cloid=CLOID)
    assert client._exchange.order.call_args[0][2] == 0.0123
    assert result["sz"] == 0.0123


def test_place_limit_order_no_longer_touches_leverage():
    client = _orderable_client()
    client.place_limit_order("BTC", True, 80000.0, 0.0125, cloid=CLOID)
    client._exchange.update_leverage.assert_not_called()


def test_place_limit_order_rounds_the_price_to_hyperliquid_precision():
    """Grid levels like 75421.05 have 7 significant digits; Hyperliquid accepts 5."""
    client = _orderable_client()
    client.place_limit_order("BTC", True, 75421.05, 0.001, cloid=CLOID)
    assert client._exchange.order.call_args[0][3] == 75421.0


def test_place_limit_order_reports_a_rejection_as_an_error():
    """Hyperliquid answers status ok with the rejection in statuses[0] -- not a successful order."""
    client = _orderable_client()
    client._exchange.order.return_value = {
        "status": "ok",
        "response": {"data": {"statuses": [{"error": "Order has invalid price."}]}},
    }
    result = client.place_limit_order("BTC", True, 75421.05, 0.001, cloid=CLOID)
    assert result["status"] == "error"
    assert "invalid price" in result["reason"]


def test_place_limit_order_refuses_a_size_that_rounds_to_zero():
    client = _orderable_client(sz_decimals=2)
    result = client.place_limit_order("BTC", True, 80000.0, 0.001, cloid=CLOID)
    assert result["status"] == "error"
    client._exchange.order.assert_not_called()


def test_place_limit_order_reads_the_oid_of_an_order_that_filled_at_once():
    client = _orderable_client()
    client._exchange.order.return_value = {
        "status": "ok",
        "response": {"data": {"statuses": [{"filled": {"oid": 42, "totalSz": "0.0125",
                                                        "avgPx": "80000"}}]}},
    }
    assert client.place_limit_order("BTC", True, 80000.0, 0.0125, cloid=CLOID)["oid"] == 42


# ── order status: which cell a fill belongs to ─────────────────────────────────

def test_get_order_status_returns_the_cloid():
    client = _make_client()
    client._info.query_order_by_oid.return_value = {
        "status": "order",
        "order": {"order": {"coin": "BTC", "oid": 99, "cloid": CLOID}, "status": "filled",
                  "statusTimestamp": 1},
    }
    assert client.get_order_status(99) == {"oid": 99, "cloid": CLOID, "status": "filled"}
    client._info.query_order_by_oid.assert_called_once_with("0xTEST", 99)


def test_get_order_status_of_an_unknown_oid():
    client = _make_client()
    client._info.query_order_by_oid.return_value = {"status": "unknownOid"}
    assert client.get_order_status(5) == {"oid": 5, "cloid": None, "status": "unknownOid"}


def test_get_order_status_raises_when_the_exchange_cannot_be_read():
    # "Could not ask" must never read as "this fill is not ours".
    client = _make_client()
    client._info.query_order_by_oid.side_effect = ConnectionError("down")
    with pytest.raises(ConnectionError):
        client.get_order_status(5)
