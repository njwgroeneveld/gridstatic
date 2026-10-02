import pytest
from unittest.mock import MagicMock, patch
from src.info_client import InfoClient


def test_get_all_mids_returns_float_dict_and_drops_none():
    client = InfoClient()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"BTC": "103500.0", "ETH": "2450.5", "DEAD": None}
    mock_resp.raise_for_status = MagicMock()
    with patch.object(client.session, "post", return_value=mock_resp):
        result = client.get_all_mids()
    assert result == {"BTC": 103500.0, "ETH": 2450.5}
    assert "DEAD" not in result


def test_prices_come_from_testnet_by_default(monkeypatch):
    # Orders and prices must come from the same network. Testnet orders priced
    # off mainnet would be placed at prices that do not exist there.
    monkeypatch.delenv("HYPERLIQUID_INFO_URL", raising=False)
    monkeypatch.delenv("HYPERLIQUID_TESTNET", raising=False)
    assert InfoClient().url == "https://api.hyperliquid-testnet.xyz/info"


def test_prices_come_from_mainnet_when_testnet_is_off(monkeypatch):
    monkeypatch.delenv("HYPERLIQUID_INFO_URL", raising=False)
    monkeypatch.setenv("HYPERLIQUID_TESTNET", "false")
    assert InfoClient().url == "https://api.hyperliquid.xyz/info"


def test_an_explicit_info_url_wins(monkeypatch):
    monkeypatch.setenv("HYPERLIQUID_TESTNET", "true")
    monkeypatch.setenv("HYPERLIQUID_INFO_URL", "https://example.test/info")
    assert InfoClient().url == "https://example.test/info"


def _ctxs(names, marks):
    return [{"universe": [{"name": n} for n in names]},
            [{"markPx": m, "midPx": "1"} for m in marks]]


def test_mark_price_of_a_hip3_coin_comes_from_its_dex():
    client = InfoClient()
    resp = MagicMock()
    resp.json.return_value = _ctxs(["xyz:XYZ100", "xyz:TSLA"], ["30499.0", "250.1"])
    with patch.object(client.session, "post", return_value=resp) as post:
        assert client.get_mark_price("xyz:XYZ100") == 30499.0
    assert post.call_args.kwargs["json"] == {"type": "metaAndAssetCtxs", "dex": "xyz"}


def test_mark_price_of_a_default_coin_asks_the_default_dex():
    client = InfoClient()
    resp = MagicMock()
    resp.json.return_value = _ctxs(["BTC"], ["86700.0"])
    with patch.object(client.session, "post", return_value=resp) as post:
        assert client.get_mark_price("BTC") == 86700.0
    assert post.call_args.kwargs["json"] == {"type": "metaAndAssetCtxs", "dex": ""}


def test_mark_price_of_an_unknown_coin_raises():
    client = InfoClient()
    resp = MagicMock()
    resp.json.return_value = _ctxs(["BTC"], ["1"])
    with patch.object(client.session, "post", return_value=resp):
        with pytest.raises(KeyError):
            client.get_mark_price("NOPE")
