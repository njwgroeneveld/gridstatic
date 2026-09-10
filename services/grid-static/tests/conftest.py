import pytest
from unittest.mock import AsyncMock


@pytest.fixture
def dal():
    m = AsyncMock()
    m.insert_grid_config.return_value = {"id": 1, "coin": "BTC"}
    m.get_active_configs.return_value = [{"id": 1, "coin": "BTC"}]
    m.insert_grid_order.return_value = {"id": 10, "level": 0}
    m.get_open_orders.return_value = []
    m.get_open_trades.return_value = []
    m.get_closed_trades.return_value = []
    return m


@pytest.fixture
def connector():
    m = AsyncMock()
    m.get_mids.return_value = {"BTC": 63000.0, "SOL": 75.0}
    m.get_account_value.return_value = 10000.0
    m.place_buy_limit.return_value = {"status": "ok", "hl_order_id": "abc123", "sz_coin": 0.01}
    m.place_sell_limit.return_value = {"status": "ok", "hl_order_id": "def456"}
    m.get_open_orders.return_value = []
    return m


@pytest.fixture
def alerter():
    return AsyncMock()


@pytest.fixture
def config():
    return {
        "coin": "BTC",
        "active": True,
        "shadow": True,
        "allocation_pct": 100,
        "upper": 68000,
        "lower": 57000,
        "num_lines": 10,
        "leverage": 1,
    }


@pytest.fixture
def grid(dal, connector, alerter, config):
    from src.strategy import StaticGrid
    return StaticGrid(
        coin_key="BTC-10",
        config=config,
        strategy_allocation_pct=80,
        dal=dal,
        connector=connector,
        alerter=alerter,
    )


@pytest.fixture
def live_config():
    return {
        "coin": "BTC",
        "active": True,
        "shadow": False,
        "allocation_pct": 100,
        "upper": 68000,
        "lower": 57000,
        "num_lines": 10,
        "leverage": 1,
    }


@pytest.fixture
def live_grid(dal, connector, alerter, live_config):
    from src.strategy import StaticGrid
    return StaticGrid(
        coin_key="BTC-10",
        config=live_config,
        strategy_allocation_pct=80,
        dal=dal,
        connector=connector,
        alerter=alerter,
    )

