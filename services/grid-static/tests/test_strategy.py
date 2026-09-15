import pytest
from unittest.mock import AsyncMock, call


def test_static_grid_default_intervals(grid):
    assert grid.fill_interval == 5
    assert grid.health_interval == 30


def test_static_grid_configurable_intervals(dal, connector, alerter, config):
    from src.strategy import StaticGrid
    grid = StaticGrid(
        coin_key="BTC-10", config=config, strategy_allocation_pct=80,
        dal=dal, connector=connector, alerter=alerter,
        fill_interval=15, health_interval=60,
    )
    assert grid.fill_interval == 15
    assert grid.health_interval == 60


@pytest.mark.asyncio
async def test_initialize_calculates_correct_levels(grid):
    await grid.initialize()
    assert len(grid.levels) == 10
    assert grid.levels[0] == pytest.approx(57000, rel=1e-4)
    assert grid.levels[-1] == pytest.approx(68000, rel=1e-4)


@pytest.mark.asyncio
async def test_initialize_only_places_buy_orders(live_grid, connector):
    await live_grid.initialize()
    calls = connector.place_buy_limit.call_args_list
    assert len(calls) > 0
    connector.place_sell_limit.assert_not_called()


@pytest.mark.asyncio
async def test_initialize_skips_level_at_current_price(live_grid, connector):
    # current price = 63000, grid has level near 63000
    await live_grid.initialize()
    placed_prices = [c.kwargs["price"] for c in connector.place_buy_limit.call_args_list]
    # nothing at or above 63000 should be in buy orders
    assert all(p < 63000 for p in placed_prices)


@pytest.mark.asyncio
async def test_initialize_saves_config_to_dal(grid, dal):
    await grid.initialize()
    dal.insert_grid_config.assert_called_once()
    call_data = dal.insert_grid_config.call_args[0][0]
    assert call_data["coin"] == "BTC"
    assert call_data["num_lines"] == 10


@pytest.mark.asyncio
async def test_initialize_saves_orders_to_dal(grid, dal):
    await grid.initialize()
    assert dal.insert_grid_order.call_count > 0


@pytest.mark.asyncio
async def test_shadow_initialize_does_not_call_exchange(grid, connector):
    # shadow=True in config fixture
    await grid.initialize()
    connector.place_buy_limit.assert_not_called()
    connector.set_leverage.assert_not_called()


@pytest.mark.asyncio
async def test_recover_state_processes_missed_fills(live_grid, dal, connector):
    # DAL has 1 OPEN buy order, exchange has 0 open orders → missed fill
    dal.get_open_orders.return_value = [
        {"id": 5, "coin": "BTC", "side": "BUY", "level": 3,
         "price": 60666.0, "size_usd": 800.0,
         "exchange_order_id": "oid123", "shadow": False}
    ]
    connector.get_open_orders.return_value = []  # not on exchange → filled
    live_grid.levels = [57000, 58222, 59444, 60666, 61888, 63111, 64333, 65555, 66777, 68000]
    live_grid.grid_config_id = 1
    await live_grid.recover_state()
    dal.patch_order.assert_called()
    dal.insert_grid_trade.assert_called()


@pytest.mark.asyncio
async def test_recover_state_skips_when_exchange_unreadable(live_grid, dal, connector):
    """A failed exchange call must never read as an empty book. It used to: the
    connector swallowed the error into [], so every resting order looked filled
    and got a sell placed against a position that did not exist -- worst exactly
    when the network is flaky, which is when pods restart in the first place."""
    dal.get_open_orders.return_value = [
        {"id": 5, "coin": "BTC", "side": "BUY", "level": 3,
         "price": 60666.0, "size_usd": 800.0,
         "exchange_order_id": "oid123", "shadow": False}
    ]
    connector.get_open_orders.side_effect = RuntimeError("exchange onbereikbaar")
    live_grid.levels = [57000, 58222, 59444, 60666, 61888, 63111, 64333, 65555, 66777, 68000]
    live_grid.grid_config_id = 1

    await live_grid.recover_state()  # must not raise

    dal.insert_grid_trade.assert_not_called()
    connector.place_sell_limit.assert_not_called()


@pytest.mark.asyncio
async def test_live_fill_check_keeps_watermark_when_fills_unreadable(live_grid, dal, connector):
    """The watermark used to move before the fills were in hand, so a failed call
    dropped that window for good. Those fills then resurfaced as 'missed fills'
    on the next restart, booked at the line price instead of the real one."""
    live_grid.grid_config_id = 1
    live_grid.last_fill_ms = 1000
    connector.get_fills.side_effect = RuntimeError("exchange onbereikbaar")

    with pytest.raises(RuntimeError):
        await live_grid._live_fill_check()

    assert live_grid.last_fill_ms == 1000


@pytest.mark.asyncio
async def test_process_buy_fill_still_sells_when_level_already_has_an_order(grid, dal):
    """Two different buy orders may both exit one level up. Skipping the second
    sell left the bought position with no exit."""
    grid.levels = [57000, 58222, 59444, 60666, 61888, 63111, 64333, 65555, 66777, 68000]
    grid.grid_config_id = 1
    dal.get_open_orders.return_value = [
        {"grid_config_id": 1, "level": 4, "side": "SELL", "price": 61888, "status": "OPEN"}
    ]
    dal.get_open_trades.return_value = []
    dal.insert_grid_trade.return_value = {"id": 500}
    order = {"id": 10, "level": 3, "size_usd": 800.0}

    await grid._process_buy_fill(order, 60666.0)

    prices = [c.args[0]["price"] for c in dal.insert_grid_order.call_args_list]
    assert 61888 in prices


@pytest.mark.asyncio
async def test_process_sell_fill_skips_buy_if_level_already_covered(grid, dal):
    grid.levels = [57000, 58222, 59444, 60666, 61888, 63111, 64333, 65555, 66777, 68000]
    grid.grid_config_id = 1
    dal.get_open_orders.return_value = [
        {"grid_config_id": 1, "level": 3, "side": "BUY", "price": 60666, "status": "OPEN"}
    ]
    dal.get_open_trades.return_value = []
    order = {"id": 20, "level": 4, "size_usd": 800.0}

    await grid._process_sell_fill(order, 61888.0)

    prices = [c.args[0]["price"] for c in dal.insert_grid_order.call_args_list]
    assert 60666 not in prices


@pytest.mark.asyncio
async def test_health_check_backfills_missing_buy_below_price(grid, connector, dal):
    grid.levels = [57000, 58222, 59444, 60666, 61888, 63111, 64333, 65555, 66777, 68000]
    grid.grid_config_id = 1
    connector.get_mids.return_value = {"BTC": 63000.0}  # between level 4 (61888) and level 5 (63111)
    dal.get_open_orders.return_value = [
        {"grid_config_id": 1, "level": 0, "side": "BUY", "price": 57000},
        {"grid_config_id": 1, "level": 2, "side": "SELL", "price": 59444},
    ]

    await grid._health_check()

    prices = [c.args[0]["price"] for c in dal.insert_grid_order.call_args_list]
    assert 58222 in prices  # level 1, uncovered, below price
    assert 60666 in prices  # level 3, uncovered, below price
    assert 61888 in prices  # level 4, uncovered, below price
    assert prices.count(57000) == 0  # level 0 already has an open BUY
    assert prices.count(59444) == 0  # level 2 already has an open SELL (position held)


@pytest.mark.asyncio
async def test_health_check_does_not_backfill_above_price(grid, connector, dal):
    grid.levels = [57000, 58222, 59444, 60666, 61888, 63111, 64333, 65555, 66777, 68000]
    grid.grid_config_id = 1
    connector.get_mids.return_value = {"BTC": 63000.0}
    dal.get_open_orders.return_value = []

    await grid._health_check()

    prices = [c.args[0]["price"] for c in dal.insert_grid_order.call_args_list]
    assert 64333 not in prices
    assert 68000 not in prices


@pytest.mark.asyncio
async def test_health_check_backfill_skips_level_with_open_trade(grid, connector, dal):
    # Regression test: a level can be "empty" of resting orders because its
    # position was bought and is now waiting to sell one level up — that is
    # NOT the same as "never seeded", and backfill must not stack a second
    # position there.
    #
    # buy_price is deliberately NOT 60666: a live fill never lands on the grid
    # line. The connector rounds the limit price, the exchange fills at its own,
    # and partial fills average the entry. Matching the line by price therefore
    # missed every live position and re-bought the level once per health cycle.
    grid.levels = [57000, 58222, 59444, 60666, 61888, 63111, 64333, 65555, 66777, 68000]
    grid.grid_config_id = 1
    connector.get_mids.return_value = {"BTC": 63000.0}
    dal.get_open_orders.return_value = []  # level 3 (60666) has no resting order...
    dal.get_open_trades.return_value = [
        {"buy_level": 3, "buy_price": 60641.0, "status": "OPEN", "sell_order_id": 555}
    ]  # ...because its position is open, waiting on its sell at 61888

    await grid._health_check()

    prices = [c.args[0]["price"] for c in dal.insert_grid_order.call_args_list]
    assert 60666.0 not in prices
    dal.get_open_trades.assert_called_with("BTC", 1)


@pytest.mark.asyncio
async def test_health_check_backfill_does_not_stack_across_cycles(grid, connector, dal):
    """Reproduces the live incident of 2026-09-09: eight buys on one level in
    eight minutes, each spawning its own sell one level up. Every health cycle
    re-bought the line because the filled buy left no resting order and the
    trade's buy_price did not match the line."""
    grid.levels = [57000, 58222, 59444, 60666, 61888, 63111, 64333, 65555, 66777, 68000]
    grid.grid_config_id = 1
    connector.get_mids.return_value = {"BTC": 63000.0}
    dal.get_open_orders.return_value = []
    dal.get_open_trades.return_value = [
        {"buy_level": 3, "buy_price": 60641.0, "status": "OPEN", "sell_order_id": 555}
    ]

    for _ in range(8):
        await grid._health_check()

    prices = [c.args[0]["price"] for c in dal.insert_grid_order.call_args_list]
    assert prices.count(60666) == 0


@pytest.mark.asyncio
async def test_process_sell_fill_reports_profit_net_of_fees(grid, dal):
    """profit_usd is what the account actually changes by: gross minus the fee on
    both sides. The buy fee is already on the trade; the sell fee is added."""
    from src.strategy import MAKER_FEE_RATE

    grid.levels = [57000, 58222, 59444, 60666, 61888]
    grid.grid_config_id = 1
    dal.get_open_orders.return_value = []
    dal.get_open_trades.return_value = [
        {"id": 500, "sell_order_id": 20, "buy_price": 60666.0,
         "size_usd": 800.0, "fee_usd": 0.12},
    ]
    order = {"id": 20, "level": 4, "size_usd": 800.0}

    await grid._process_sell_fill(order, 61888.0)

    patched = dal.patch_trade.call_args[0][1]
    sell_fee = 800.0 * 1 * MAKER_FEE_RATE
    gross = (61888.0 - 60666.0) / 60666.0 * 800.0 * 1
    assert patched["fee_usd"] == pytest.approx(0.12 + sell_fee)
    assert patched["profit_usd"] == round(gross - (0.12 + sell_fee), 2)


@pytest.mark.asyncio
async def test_process_buy_fill_live_takes_size_and_fee_from_fill(grid, dal, connector):
    """Live computes nothing: the fill says exactly how much was bought and what
    it cost, so the SELL goes to market with exactly that size."""
    grid.shadow = False
    grid.levels = [57000, 58222, 59444, 60666, 61888]
    grid.grid_config_id = 1
    dal.get_open_orders.return_value = []
    dal.insert_grid_trade.return_value = {"id": 500}
    order = {"id": 10, "level": 3, "size_usd": 800.0}
    fill = {"sz": "0.039", "fee": "0.0176", "px": "60666.0"}

    await grid._process_buy_fill(order, 60666.0, fill=fill)

    assert dal.insert_grid_trade.call_args[0][0]["fee_usd"] == 0.0176
    assert connector.place_sell_limit.call_args[0][1] == 0.039


@pytest.mark.asyncio
async def test_process_buy_fill_shadow_sizes_position_by_leverage(grid, dal):
    """Shadow has no fill and has to model one -- including leverage, otherwise it
    simulates a smaller position than live would take."""
    grid.config["leverage"] = 3
    grid.levels = [57000, 58222, 59444, 60666, 61888]
    grid.grid_config_id = 1
    dal.get_open_orders.return_value = []
    dal.insert_grid_trade.return_value = {"id": 500}
    order = {"id": 10, "level": 3, "size_usd": 800.0}

    await grid._process_buy_fill(order, 60000.0)

    assert dal.insert_grid_trade.call_args[0][0]["fee_usd"] == pytest.approx(0.36)


@pytest.mark.asyncio
async def test_partial_fills_grow_one_trade_instead_of_creating_more(grid, dal, connector):
    """A limit order can fill in pieces. Giving every piece its own trade left
    positions without a sell order; now exactly one trade grows with them."""
    grid.shadow = False
    grid.levels = [57000, 58222, 59444, 60666, 61888]
    grid.grid_config_id = 1
    grid.config["leverage"] = 3
    dal.get_open_orders.return_value = []
    dal.get_open_trades.return_value = []
    dal.insert_grid_trade.return_value = {"id": 500, "buy_order_id": 10}
    dal.insert_grid_order.return_value = {"id": 77, "level": 4}
    order = {"id": 10, "level": 3, "size_usd": 800.0}

    # eerste stukje
    await grid._process_buy_fill(order, 60666.0, fill={"sz": "0.02", "px": "60666.0", "fee": "0.5"})
    assert dal.insert_grid_trade.call_count == 1
    assert connector.place_sell_limit.call_args[0][1] == 0.02

    # second piece of the same order
    dal.get_open_trades.return_value = [
        {"id": 500, "buy_order_id": 10, "buy_price": 60666.0,
         "size_usd": 404.44, "fee_usd": 0.5, "sell_order_id": 77},
    ]
    dal.get_open_orders.return_value = [
        {"id": 77, "grid_config_id": 1, "level": 4, "side": "SELL",
         "price": 61888, "status": "OPEN", "exchange_order_id": "999"},
    ]
    await grid._process_buy_fill(order, 60666.0, fill={"sz": "0.01", "px": "60666.0", "fee": "0.25"})

    # no second trade, but a larger sell, and the old one was cancelled
    assert dal.insert_grid_trade.call_count == 1
    connector.cancel_order.assert_awaited_with("BTC", "999")
    assert connector.place_sell_limit.call_args[0][1] == 0.03

    patched = dal.patch_trade.call_args_list[-2].args[1]
    assert patched["fee_usd"] == 0.75


@pytest.mark.asyncio
async def test_record_funding_stores_new_records(grid, dal, connector):
    """Funding comes from the exchange, not from a formula: the rate changes every
    hour and can flip sign, so modelling it would get the sign wrong."""
    grid.shadow = False
    grid.grid_config_id = 15
    dal.get_last_funding_ms.return_value = 1_000_000
    connector.get_funding.return_value = [
        {"time": 1_003_600, "coin": "BTC", "usdc": -0.0031,
         "funding_rate": 0.0000125, "szi": 0.00312},
        {"time": 1_007_200, "coin": "BTC", "usdc": 0.0226,
         "funding_rate": -0.0000918, "szi": 0.00312},
        {"time": 1_007_200, "coin": "ETH", "usdc": -0.5,
         "funding_rate": 0.0001, "szi": 1.0},
    ]

    await grid._record_funding()

    connector.get_funding.assert_awaited_with(1_000_001)
    stored = [c.args[0] for c in dal.insert_grid_funding.call_args_list]
    assert len(stored) == 2          # the ETH record does not belong to this grid
    assert stored[0]["usdc"] == -0.0031
    assert stored[1]["usdc"] == 0.0226
    assert all(r["grid_config_id"] == 15 and r["shadow"] is False for r in stored)


@pytest.mark.asyncio
async def test_record_funding_does_nothing_in_shadow(grid, dal, connector):
    """A shadow grid holds no position, so there is no funding to fetch."""
    grid.shadow = True

    await grid._record_funding()

    connector.get_funding.assert_not_awaited()
    dal.insert_grid_funding.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_sizing_uses_fixed_start_capital_not_account_value(grid, dal, connector):
    """Account value moves with unrealised P&L, so sizing on it shrinks the lines
    exactly while the grid is buying its way down. Fixed start capital plus realised
    profit keeps the lines stable -- and matches how shadow sizes, so the two stay
    comparable."""
    grid.shadow = False
    grid.grid_config_id = 1
    grid.start_balance = 464.0
    connector.get_account_value.return_value = 50.0  # would make lines 9x smaller
    dal.get_closed_trades.return_value = [{"profit_usd": 16.0}]

    size = await grid._current_size_usd()

    connector.get_account_value.assert_not_awaited()
    # (464 + 16) * 80% / 10 lines
    assert size == 38.4
