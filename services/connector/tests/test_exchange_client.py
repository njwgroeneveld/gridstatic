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


def test_a_position_of_zero_is_no_position():
    client = _make_client()
    client._info.user_state.return_value = {
        "assetPositions": [
            {"position": {"coin": "BTC", "szi": "0.01", "entryPx": "103000.0"}},
            {"position": {"coin": "ETH", "szi": "0",    "entryPx": "2450.0"}},
        ]
    }
    client._info.spot_user_state.return_value = {"balances": []}
    result = client.get_account("")["positions"]
    assert "BTC" in result
    assert result["BTC"] == {"szi": 0.01, "entry_px": 103000.0, "unrealized_pnl": 0.0}
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
    client._info.user_fills_by_time.side_effect = RuntimeError("API down")
    with pytest.raises(RuntimeError):
        client.get_fills_since("BTC", since_ms=0)


def test_get_fills_since_filters_by_coin_and_time():
    client = _make_client()
    client._info.user_fills_by_time.return_value = [
        {"coin": "BTC", "time": 1000, "oid": 1},
        {"coin": "BTC", "time": 500,  "oid": 2},
        {"coin": "ETH", "time": 2000, "oid": 3},
    ]
    result = client.get_fills_since("BTC", since_ms=600)
    assert len(result) == 1
    assert result[0]["oid"] == 1


def test_fills_are_asked_from_the_start_time_not_the_whole_history():
    client = _make_client()
    client._info.user_fills_by_time.return_value = []
    client.get_fills_since("BTC", since_ms=1_800_000_000_000)
    client._info.user_fills_by_time.assert_called_once_with("0xTEST", 1_800_000_000_000)
    client._info.user_fills.assert_not_called()


def test_a_full_page_of_fills_is_followed_by_the_next():
    # Oldest first, at most 2000 an answer: without the next page the newest
    # fills -- the ones the grid needs -- would be missing.
    client = _make_client()
    first = [{"coin": "BTC", "time": 1000 + i, "tid": i, "oid": i} for i in range(2000)]
    second = [first[-1],                                   # the page boundary, again
              {"coin": "BTC", "time": 5000, "tid": 9999, "oid": 9999}]
    client._info.user_fills_by_time.side_effect = [first, second]
    fills = client.get_fills_since("BTC", since_ms=0)
    assert len(fills) == 2001                              # the boundary fill only once
    assert fills[-1]["tid"] == 9999
    assert client._info.user_fills_by_time.call_args_list[1].args == ("0xTEST", 2999)


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


# ── HIP-3: markets on a builder-deployed perp dex, e.g. xyz:XYZ100 ────────────

def _hip3_client() -> ExchangeClient:
    with patch("src.exchange_client.Account") as mock_acct, \
         patch("src.exchange_client.Info") as info, \
         patch("src.exchange_client.Exchange") as exchange:
        mock_acct.from_key.return_value = MagicMock(address="0xTEST")
        client = ExchangeClient(private_key="0x" + "a" * 64, testnet=True, perp_dexs=["xyz"])
    client._built_with = (info.call_args, exchange.call_args)
    client._info = MagicMock()
    client._exchange = MagicMock()
    return client


def test_the_sdk_learns_the_extra_dex_next_to_the_default_one():
    # Without it the SDK cannot turn "xyz:XYZ100" into an asset id, and every
    # order on that market fails.
    info_call, exchange_call = _hip3_client()._built_with
    assert info_call.kwargs["perp_dexs"] == ["", "xyz"]
    assert exchange_call.kwargs["perp_dexs"] == ["", "xyz"]


def test_without_extra_dexes_the_sdk_gets_the_default_only():
    with patch("src.exchange_client.Account") as mock_acct, \
         patch("src.exchange_client.Info") as info, patch("src.exchange_client.Exchange"):
        mock_acct.from_key.return_value = MagicMock(address="0xTEST")
        ExchangeClient(private_key="0x" + "a" * 64, testnet=True)
    assert info.call_args.kwargs["perp_dexs"] == [""]


def test_open_orders_of_a_hip3_coin_are_read_from_its_dex():
    client = _hip3_client()
    client._info.frontend_open_orders.return_value = [{"coin": "xyz:XYZ100", "oid": 1},
                                                      {"coin": "xyz:TSLA", "oid": 2}]
    assert [o["oid"] for o in client.get_open_orders("xyz:XYZ100")] == [1]
    client._info.frontend_open_orders.assert_called_once_with("0xTEST", dex="xyz")


def test_a_position_named_without_its_dex_prefix_still_gets_it():
    # Whether the exchange prefixes coin names in clearinghouseState is not
    # something to bet the position check on.
    client = _hip3_client()
    client._info.user_state.return_value = {"assetPositions": [
        {"position": {"coin": "XYZ100", "szi": "0.002", "entryPx": "30000"}}]}
    assert "xyz:XYZ100" in client.get_account("xyz")["positions"]


def test_a_dex_that_is_down_does_not_stop_the_account_of_another():
    client = _hip3_client()

    def state(addr, dex=""):
        if dex == "xyz":
            raise ConnectionError("down")
        return {"assetPositions": [{"position": {"coin": "BTC", "szi": "0.01", "entryPx": "1"}}]}

    client._info.user_state.side_effect = state
    client._info.spot_user_state.return_value = {"balances": []}
    assert "BTC" in client.get_account("")["positions"]
    with pytest.raises(ConnectionError):
        client.get_account("xyz")


def test_lot_size_of_a_hip3_coin_comes_from_its_dex():
    client = _hip3_client()
    client._info.meta.side_effect = lambda dex="": {
        "": {"universe": [{"name": "BTC", "szDecimals": 5}]},
        "xyz": {"universe": [{"name": "xyz:XYZ100", "szDecimals": 4}]},
    }[dex]
    assert client.get_sz_decimals("xyz:XYZ100") == 4
    assert client.get_sz_decimals("BTC") == 5


def test_an_unknown_coin_has_no_lot_size_rather_than_a_guess():
    # Guessing 3 decimals sized orders for a market nobody checked.
    client = _hip3_client()
    client._info.meta.return_value = {"universe": [{"name": "BTC", "szDecimals": 5}]}
    with pytest.raises(KeyError):
        client.get_sz_decimals("DOGE")


@pytest.mark.parametrize("dex, coin", [("xyz", "xyz:XYZ100"), ("", "BTC")])
def test_account_reads_the_state_of_its_dex_once_for_value_and_positions(dex, coin):
    client = _hip3_client()
    client._info.user_state.return_value = {
        "marginSummary": {"accountValue": "250.5"},
        "assetPositions": [{"position": {"coin": coin, "szi": "0.002", "entryPx": "30000",
                                         "unrealizedPnl": "-1.5"}}]}
    client._info.spot_user_state.return_value = {"balances": [
        {"coin": "USDC", "total": "100", "hold": "10"}]}
    account = client.get_account(dex)
    client._info.user_state.assert_called_once_with("0xTEST", dex=dex)
    # spot USDC counts on the default dex only
    assert account["account_value"] == (250.5 if dex else 340.5)
    assert account["positions"] == {"xyz:XYZ100" if dex else "BTC": {
        "szi": 0.002, "entry_px": 30000.0, "unrealized_pnl": -1.5}}


def test_an_unreadable_account_raises_rather_than_reads_as_empty():
    client = _hip3_client()
    client._info.user_state.side_effect = ConnectionError("down")
    with pytest.raises(ConnectionError):
        client.get_account("xyz")


# ── stop-loss and leverage ─────────────────────────────────────────────────────

def test_place_stop_order_is_a_reduce_only_stop_market_sell():
    client = _orderable_client(sz_decimals=4)
    res = client.place_stop_order("BTC", 0.0019, 78123.4, cloid=CLOID)
    args, kwargs = client._exchange.order.call_args
    assert args[0] == "BTC" and args[1] is False and args[2] == 0.0019
    assert args[4] == {"trigger": {"triggerPx": 78123.0, "isMarket": True, "tpsl": "sl"}}
    assert kwargs["reduce_only"] is True and str(kwargs["cloid"]) == CLOID
    assert res["status"] == "ok" and res["oid"] == 99


def test_place_stop_order_caps_slippage_ten_percent_below_the_trigger():
    # On a thin book a stop-market needs room to fill -- a bad fill beats none.
    client = _orderable_client()
    client.place_stop_order("BTC", 0.001, 80000.0, cloid=CLOID)
    assert client._exchange.order.call_args[0][3] == 72000.0


def test_place_stop_order_reports_a_rejection():
    client = _orderable_client()
    client._exchange.order.return_value = {
        "status": "ok", "response": {"data": {"statuses": [{"error": "Reduce only order would increase position."}]}}}
    assert client.place_stop_order("BTC", 0.001, 80000.0, cloid=CLOID)["status"] == "error"


def test_set_leverage_raises_when_the_exchange_refuses():
    # The SDK answers a refusal with status "err" instead of raising; logging it
    # and carrying on left the bot sizing for a leverage that was never set.
    client = _make_client()
    client._exchange.update_leverage.return_value = {"status": "err", "response": "Invalid leverage value"}
    with pytest.raises(RuntimeError, match="Invalid leverage"):
        client.set_leverage("BTC", 99)


def test_set_leverage_accepts_ok():
    client = _make_client()
    client._exchange.update_leverage.return_value = {"status": "ok", "response": {"type": "default"}}
    client.set_leverage("BTC", 3)
    client._exchange.update_leverage.assert_called_once_with(3, "BTC", is_cross=False)


def test_a_position_carries_its_unrealised_pnl():
    # The grid sizes its orders from realised equity: account value minus this.
    client = _make_client()
    client._info.user_state.return_value = {"assetPositions": [
        {"position": {"coin": "BTC", "szi": "0.002", "entryPx": "83000", "unrealizedPnl": "-1.25"}}]}
    client._info.spot_user_state.return_value = {"balances": []}
    assert client.get_account("")["positions"]["BTC"]["unrealized_pnl"] == -1.25


def _built(key_address: str, wallet_address: str | None, roles: dict[str, str] | None = None):
    """How the SDK's Exchange was constructed, for a key, a wallet address and the roles
    the exchange reports through userRole."""
    roles = roles or {}
    with patch("src.exchange_client.Account") as mock_acct,          patch("src.exchange_client.Info") as info,          patch("src.exchange_client.Exchange") as exchange:
        mock_acct.from_key.return_value = MagicMock(address=key_address)
        info.return_value.post.side_effect = (
            lambda path, body: {"role": roles.get(body["user"], "user")})
        client = ExchangeClient(private_key="0x" + "a" * 64, wallet_address=wallet_address,
                                testnet=True)
    return client, exchange.call_args.kwargs


def test_the_main_key_for_a_subaccount_trades_as_before():
    # Niels' setup: the main key, wallet_address the subaccount.
    client, kw = _built("0xMAIN", "0xSUB", {"0xSUB": "subAccount"})
    assert kw["vault_address"] == "0xSUB"
    assert kw.get("account_address") is None
    assert client.uses_api_wallet is False


def test_an_api_wallet_for_a_subaccount_keeps_the_vault_address():
    client, kw = _built("0xAGENT", "0xSUB", {"0xAGENT": "agent", "0xSUB": "subAccount"})
    assert kw["vault_address"] == "0xSUB"
    assert kw.get("account_address") is None
    assert client.uses_api_wallet is True


def test_an_api_wallet_for_the_main_account_uses_account_address():
    # As a vault address the exchange would read the main account as a subaccount.
    client, kw = _built("0xAGENT", "0xMAIN", {"0xAGENT": "agent"})
    assert kw["account_address"] == "0xMAIN"
    assert kw.get("vault_address") is None
    assert client.uses_api_wallet is True


def test_the_account_s_own_key_asks_nothing_and_trades_as_before():
    client, kw = _built("0xMAIN", None)
    assert client.uses_api_wallet is False
    assert kw.get("account_address") is None


def test_addresses_are_compared_without_case():
    client, _ = _built("0xAbC", "0xabc", {"0xAbC": "agent"})
    assert client.uses_api_wallet is False


def test_an_unreadable_role_refuses_to_build():
    with patch("src.exchange_client.Account") as mock_acct,          patch("src.exchange_client.Info") as info,          patch("src.exchange_client.Exchange"):
        mock_acct.from_key.return_value = MagicMock(address="0xAGENT")
        info.return_value.post.side_effect = ConnectionError("down")
        with pytest.raises(ConnectionError):
            ExchangeClient(private_key="0x" + "a" * 64, wallet_address="0xSUB", testnet=True)
