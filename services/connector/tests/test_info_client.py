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
