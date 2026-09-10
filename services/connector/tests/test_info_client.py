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


