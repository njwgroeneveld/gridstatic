import itertools

import pytest
from unittest.mock import AsyncMock

# A small grid: lines 100, 110, ..., 200 -- 11 lines, 10 cells. $550 over 11
# lines at 1x is $50 a line, so a full buy on cell 3 (130) is 0.38 coin.
START_MS = 1_800_000_000_000


@pytest.fixture
def config():
    return {
        "coin": "BTC",
        "active": True,
        "allocation_pct": 100,
        "upper": 200,
        "lower": 100,
        "num_lines": 11,
        "leverage": 1,
    }


@pytest.fixture
def connector():
    m = AsyncMock()
    m.get_open_orders.return_value = []
    m.get_positions.return_value = {}
    m.get_mids.return_value = {"BTC": 155.0}
    m.get_fills.return_value = []
    m.get_sz_decimals.return_value = 2
    m.get_account_value.return_value = 10_000.0
    m.cancel_order.return_value = {"status": "ok"}
    m.get_order_status.return_value = {"oid": 0, "cloid": None, "status": "unknownOid"}
    oids = itertools.count(500)

    async def place(coin, is_buy, price, sz, cloid, reduce_only):
        return {"status": "ok", "oid": next(oids), "cloid": cloid, "sz": sz, "px": price}

    m.place_limit.side_effect = place
    return m


@pytest.fixture
def alerter():
    return AsyncMock()


class Clock:
    """Seconds since the epoch, moved by hand."""

    def __init__(self, ms: int) -> None:
        self.ms = ms

    def __call__(self) -> float:
        return self.ms / 1000


@pytest.fixture
def clock():
    return Clock(START_MS)


@pytest.fixture
def grid(config, connector, alerter, clock):
    from src.strategy import StaticGrid
    return StaticGrid("BTC-11", config, strategy_allocation_pct=100, start_balance=550,
                      connector=connector, alerter=alerter, clock=clock)
