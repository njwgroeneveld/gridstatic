"""The thin layer around reconcile: read, tie fills to cells, act, report.

What reconcile decides is tested in test_reconcile.py. These tests are about
what can go wrong around it -- an exchange that cannot be read, a cancel that
fails, a fill that cannot be tied to a cell -- and about what gets reported.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock

from src.cloid import BUY, SELL, STOP, decode, encode
from tests.conftest import START_MS


def _book_order(grid, cell, side, sz, oid, ts=START_MS - 60_000, orig=None):
    return {"coin": "BTC", "oid": oid, "cloid": encode(grid.fp, cell, side),
            "side": "B" if side == BUY else "A",
            "limitPx": str(grid.levels[cell] if side == BUY else grid.levels[cell + 1]),
            "sz": str(sz), "origSz": str(orig if orig is not None else sz), "timestamp": ts}


def _fill(oid, side, sz, px, t, fee="0.01"):
    return {"coin": "BTC", "oid": oid, "side": "B" if side == BUY else "A",
            "sz": str(sz), "px": str(px), "time": t, "fee": fee, "closedPnl": "0"}


def _resting_buys(grid, *cells):
    return [_book_order(grid, c, BUY, round(50 / grid.levels[c], 2), oid=100 + c) for c in cells]


def _placed(connector):
    out = []
    for call in connector.place_limit.call_args_list:
        kw = call.kwargs
        tag = decode(kw["cloid"])
        out.append((tag.cell, tag.side, kw["price"], kw["sz"], kw["reduce_only"], kw["is_buy"]))
    return out


def _alert_types(alerter):
    return [c.args[0] for c in alerter.send_alert.call_args_list]


# ── starting up ────────────────────────────────────────────────────────────────

async def test_start_reads_the_lot_size_and_sets_leverage(grid, connector):
    await grid.start()
    connector.get_sz_decimals.assert_awaited_once_with("BTC")
    connector.set_leverage.assert_awaited_once_with("BTC", 1)
    assert grid.sz_decimals == 2


async def test_start_ties_every_recent_fill_before_the_first_round(grid, connector):
    # After a restart the oid -> cloid memory is empty. Filling it before the
    # first round keeps that round from going on hold over fills it could have
    # tied to a cell.
    connector.get_fills.return_value = [_fill(7, BUY, 0.38, 130, START_MS - 5000),
                                        _fill(8, BUY, 0.42, 120, START_MS - 4000)]
    await grid.start()
    asked = sorted(c.args[0] for c in connector.get_order_status.call_args_list)
    assert asked == [7, 8]


async def test_start_warns_when_the_account_cannot_carry_the_grid(grid, connector, alerter):
    # Six cells below 155 at $50 margin each need $300.
    connector.get_account_value.return_value = 120.0
    await grid.start()
    assert "error" in _alert_types(alerter)
    assert "margin" in alerter.send_alert.call_args.args[1]["message"]


# ── a round ────────────────────────────────────────────────────────────────────

async def test_the_first_round_on_an_empty_book_places_the_buys(grid, connector):
    await grid.start()
    await grid.run_round()
    placed = _placed(connector)
    assert sorted(p[0] for p in placed) == [0, 1, 2, 3, 4, 5]
    assert all(side == BUY and is_buy and not ro for _, side, _, _, ro, is_buy in placed)
    assert (3, BUY, 130.0, 0.38, False, True) in placed


@pytest.mark.parametrize("unreadable", ["get_open_orders", "get_positions", "get_price", "get_fills"])
async def test_an_unreadable_exchange_does_nothing(grid, connector, unreadable):
    # An exchange that cannot be read is never an empty exchange.
    await grid.start()
    getattr(connector, unreadable).side_effect = ConnectionError("down")
    assert await grid.run_round() is None
    connector.place_limit.assert_not_called()
    connector.cancel_order.assert_not_called()


async def test_a_filled_buy_gets_a_reduce_only_sell(grid, connector):
    await grid.start()
    cloid3 = encode(grid.fp, 3, BUY)
    connector.get_order_status.return_value = {"oid": 9, "cloid": cloid3, "status": "filled"}
    connector.get_open_orders.return_value = _resting_buys(grid, 0, 1, 2, 4, 5)
    connector.get_positions.return_value = {"BTC": {"szi": 0.38, "entry_px": 130.0}}
    connector.get_fills.return_value = [_fill(9, BUY, 0.38, 130, START_MS + 1000)]
    await grid.run_round()
    assert _placed(connector) == [(3, SELL, 140.0, 0.38, True, False)]


async def test_fills_of_orders_the_bot_placed_need_no_lookup(grid, connector):
    await grid.start()
    await grid.run_round()                              # places buy on cell 3, among others
    oid3 = next(c for c in connector.place_limit.call_args_list
                if decode(c.kwargs["cloid"]).cell == 3)
    placed_oid = 500 + connector.place_limit.call_args_list.index(oid3)
    connector.get_order_status.reset_mock()
    connector.get_fills.return_value = [_fill(placed_oid, BUY, 0.38, 130, START_MS + 1000)]
    connector.get_positions.return_value = {"BTC": {"szi": 0.38, "entry_px": 130.0}}
    connector.get_open_orders.return_value = _resting_buys(grid, 0, 1, 2, 4, 5)
    connector.place_limit.reset_mock()
    await grid.run_round()
    connector.get_order_status.assert_not_called()
    assert _placed(connector) == [(3, SELL, 140.0, 0.38, True, False)]


async def test_a_fill_that_cannot_be_tied_puts_the_grid_on_hold(grid, connector, alerter):
    await grid.start()
    connector.get_order_status.side_effect = ConnectionError("down")
    connector.get_open_orders.return_value = _resting_buys(grid, 0, 1, 2, 4, 5)
    connector.get_positions.return_value = {"BTC": {"szi": 0.38, "entry_px": 130.0}}
    connector.get_fills.return_value = [_fill(9, BUY, 0.38, 130, START_MS + 1000)]
    plan = await grid.run_round()
    assert plan.hold is not None
    connector.place_limit.assert_not_called()
    assert "grid_hold" in _alert_types(alerter)


async def test_an_unknown_oid_is_asked_again_next_round(grid, connector):
    # "unknownOid" can be the exchange not having caught up. Remembering it as
    # "not ours" would keep the grid on hold for good.
    await grid.start()
    connector.get_fills.return_value = [_fill(9, BUY, 0.38, 130, START_MS + 1000)]
    await grid.run_round()
    await grid.run_round()
    assert [c.args[0] for c in connector.get_order_status.call_args_list].count(9) == 2


async def test_cancels_go_first_and_a_failed_cancel_skips_its_replacement(grid, connector):
    # Placing the bigger sell while the small one survives would leave the cell
    # with two sells.
    await grid.start()
    small_sell = _book_order(grid, 3, SELL, 0.20, oid=300, ts=START_MS - 2000)
    connector.get_open_orders.return_value = _resting_buys(grid, 0, 1, 2, 4, 5) + [small_sell]
    connector.get_positions.return_value = {"BTC": {"szi": 0.38, "entry_px": 130.0}}
    cloid3 = encode(grid.fp, 3, BUY)
    connector.get_order_status.return_value = {"oid": 9, "cloid": cloid3, "status": "filled"}
    connector.get_fills.return_value = [_fill(9, BUY, 0.20, 130, START_MS - 3000),
                                        _fill(9, BUY, 0.18, 130, START_MS - 1000)]
    connector.cancel_order.side_effect = RuntimeError("already gone")
    await grid.run_round()
    connector.cancel_order.assert_awaited_once_with("BTC", 300)
    connector.place_limit.assert_not_called()


async def test_one_failed_order_does_not_stop_the_others(grid, connector):
    await grid.start()
    place = connector.place_limit.side_effect

    async def flaky(coin, is_buy, price, sz, cloid, reduce_only):
        if decode(cloid).cell == 2:
            raise RuntimeError("rejected")
        return await place(coin, is_buy, price, sz, cloid, reduce_only)

    connector.place_limit.side_effect = flaky
    await grid.run_round()
    assert connector.place_limit.await_count == 6


# ── reporting ──────────────────────────────────────────────────────────────────

async def test_a_hold_is_reported_once_and_so_is_its_end(grid, connector, alerter):
    await grid.start()
    connector.get_open_orders.return_value = _resting_buys(grid, 0, 1, 2, 3, 4, 5)
    connector.get_positions.return_value = {"BTC": {"szi": 0.5, "entry_px": 130.0}}
    await grid.run_round()
    await grid.run_round()
    connector.get_positions.return_value = {}
    await grid.run_round()
    types = _alert_types(alerter)
    assert types.count("grid_hold") == 1
    assert types.count("hold_cleared") == 1


async def test_fills_from_before_the_start_are_not_announced(grid, connector, alerter, clock):
    cloid3 = encode(grid.fp, 3, BUY)
    connector.get_order_status.return_value = {"oid": 9, "cloid": cloid3, "status": "filled"}
    connector.get_fills.return_value = [_fill(9, BUY, 0.38, 130, START_MS - 1000)]
    connector.get_positions.return_value = {"BTC": {"szi": 0.38, "entry_px": 130.0}}
    connector.get_open_orders.return_value = _resting_buys(grid, 0, 1, 2, 4, 5)
    await grid.start()
    await grid.run_round()
    assert "buy_filled" not in _alert_types(alerter)

    connector.get_fills.return_value.append(_fill(9, BUY, 0.0, 130, START_MS + 1000))
    clock.ms += 30_000
    await grid.run_round()
    assert "buy_filled" in _alert_types(alerter)


async def test_a_closed_cycle_reports_its_profit_after_fees(grid, connector, alerter):
    cloid_b = encode(grid.fp, 3, BUY)
    cloid_s = encode(grid.fp, 3, SELL)
    statuses = {9: cloid_b, 10: cloid_s}
    connector.get_order_status.side_effect = lambda oid: {"oid": oid, "cloid": statuses[oid],
                                                          "status": "filled"}
    connector.get_open_orders.return_value = _resting_buys(grid, 0, 1, 2, 4, 5)
    await grid.start()
    connector.get_fills.return_value = [
        _fill(9, BUY, 0.38, 130, START_MS + 1000, fee="0.01"),
        _fill(10, SELL, 0.38, 140, START_MS + 2000, fee="0.02"),
    ]
    await grid.run_round()
    closed = [c.args[1] for c in alerter.send_alert.call_args_list if c.args[0] == "trade_closed"]
    assert len(closed) == 1
    # (140 - 130) * 0.38 - 0.01 - 0.02
    assert closed[0]["profit_usd"] == pytest.approx(3.77)
    assert closed[0]["buy_price"] == 130.0 and closed[0]["sell_price"] == 140.0


async def test_price_outside_the_grid_is_alerted(grid, connector, alerter):
    await grid.start()
    connector.get_price.return_value = 95.0
    await grid.run_round()
    assert "outside_grid" in _alert_types(alerter)


async def test_status_shows_the_last_round(grid, connector):
    await grid.start()
    await grid.run_round()
    s = grid.status()
    assert s["coin"] == "BTC"
    assert s["price"] == 155.0
    assert s["hold"] is None
    assert len(s["cells"]) == 10
    assert s["cells"][3]["buy_price"] == 130.0


async def test_status_before_the_first_round(grid):
    s = grid.status()
    assert s["cells"] == [] and s["last_round_ms"] is None


async def test_status_includes_what_the_round_just_placed(grid, connector):
    # The round reads the book before it acts. Without this, /status right after
    # startup shows every cell empty while six buys are already resting.
    await grid.start()
    await grid.run_round()
    states = {c["cell"]: c["state"] for c in grid.status()["cells"]}
    assert [states[i] for i in range(6)] == ["buy"] * 6
    assert states[6] == "empty"
    assert grid.status()["cells"][3]["buy_sz"] == 0.38


async def test_status_shows_a_new_sell_as_covering_the_cell(grid, connector):
    await grid.start()
    cloid3 = encode(grid.fp, 3, BUY)
    connector.get_order_status.return_value = {"oid": 9, "cloid": cloid3, "status": "filled"}
    connector.get_open_orders.return_value = _resting_buys(grid, 0, 1, 2, 4, 5)
    connector.get_positions.return_value = {"BTC": {"szi": 0.38, "entry_px": 130.0}}
    connector.get_fills.return_value = [_fill(9, BUY, 0.38, 130, START_MS + 1000)]
    await grid.run_round()
    cell3 = grid.status()["cells"][3]
    assert cell3["state"] == "sell" and cell3["sell_sz"] == 0.38 and cell3["unsold"] == 0.0


async def test_status_leaves_out_an_order_that_failed(grid, connector):
    await grid.start()
    connector.place_limit.side_effect = RuntimeError("rejected")
    await grid.run_round()
    assert all(c["state"] == "empty" for c in grid.status()["cells"])


# ── reading the account it trades on ───────────────────────────────────────────

async def test_orders_that_vanish_put_the_grid_on_hold(grid, connector, alerter):
    # The connector reads one account while orders land on another -- an API
    # wallet without its account address does exactly that. Every round would
    # see an empty book and buy a full layer again. None of last round's orders
    # being on the book or filled gives it away.
    await grid.start()
    await grid.run_round()                       # six buys go out
    assert connector.place_limit.await_count == 6
    connector.place_limit.reset_mock()
    plan = await grid.run_round()                # the book still reads empty
    assert plan.hold is not None and "account" in plan.hold
    connector.place_limit.assert_not_called()
    assert "grid_hold" in _alert_types(alerter)


async def test_one_cancelled_order_is_not_a_wrong_account(grid, connector):
    # Someone cancelling one order by hand is not the same thing.
    await grid.start()
    await grid.run_round()
    placed = connector.place_limit.call_args_list
    book = []
    for i, call in enumerate(placed[1:], start=1):          # the first one was cancelled
        kw = call.kwargs
        book.append({"coin": "BTC", "oid": 500 + i, "cloid": kw["cloid"],
                     "side": "B", "limitPx": str(kw["price"]), "sz": str(kw["sz"]),
                     "origSz": str(kw["sz"]), "timestamp": START_MS})
    connector.get_open_orders.return_value = book
    plan = await grid.run_round()
    assert plan.hold is None


async def test_orders_that_filled_did_not_vanish(grid, connector):
    await grid.start()
    await grid.run_round()
    connector.get_fills.return_value = [_fill(500 + i, BUY, 0.0, 100, START_MS + 1000)
                                        for i in range(6)]
    plan = await grid.run_round()
    assert plan.hold is None or "account" not in plan.hold


async def test_an_exchange_that_stays_unreadable_is_alerted_once(grid, connector, alerter):
    # One failed read is noise; a node that lost its network for minutes is not.
    await grid.start()
    connector.get_open_orders.side_effect = ConnectionError("down")
    for _ in range(2):
        await grid.run_round()
    assert "error" not in _alert_types(alerter)
    for _ in range(5):
        await grid.run_round()
    errors = [c.args[1]["message"] for c in alerter.send_alert.call_args_list if c.args[0] == "error"]
    assert len(errors) == 1 and "unreadable" in errors[0]


# ── HIP-3 markets ──────────────────────────────────────────────────────────────

async def test_the_grid_decides_from_the_mark_price_not_the_mid(grid, connector):
    # On a thin book the mid is the middle of an empty spread, and the grid's own
    # buys would move it. The mid here says 300 -- every line would get a buy.
    connector.get_mids.return_value = {"BTC": 300.0}
    connector.get_price.return_value = 155.0
    await grid.start()
    await grid.run_round()
    assert sorted(decode(c.kwargs["cloid"]).cell
                  for c in connector.place_limit.call_args_list) == [0, 1, 2, 3, 4, 5]
    connector.get_price.assert_awaited_with("BTC")


async def test_the_margin_check_reads_the_balance_of_the_coin_s_dex(config, connector, alerter, clock):
    from src.strategy import StaticGrid
    config["coin"] = "xyz:XYZ100"
    g = StaticGrid("NDX", config, strategy_allocation_pct=100, start_balance=550,
                   connector=connector, alerter=alerter, clock=clock)
    await g.start()
    connector.get_account_value.assert_awaited_once_with(dex="xyz")


async def test_the_margin_check_on_the_default_dex(grid, connector):
    await grid.start()
    connector.get_account_value.assert_awaited_once_with(dex="")


# ── stop-loss ──────────────────────────────────────────────────────────────────

def _stop_grid(config, connector, alerter, clock, stop_loss=96.0):
    from src.strategy import StaticGrid
    config["stop_loss"] = stop_loss
    return StaticGrid("BTC-11", config, strategy_allocation_pct=100, start_balance=550,
                      connector=connector, alerter=alerter, clock=clock)


async def test_the_stop_goes_out_through_the_stop_route(config, connector, alerter, clock):
    g = _stop_grid(config, connector, alerter, clock)
    await g.start()
    cloid3 = encode(g.fp, 3, BUY)
    connector.get_order_status.return_value = {"oid": 9, "cloid": cloid3, "status": "filled"}
    connector.get_open_orders.return_value = _resting_buys(g, 0, 1, 2, 4, 5)
    connector.get_positions.return_value = {"BTC": {"szi": 0.38, "entry_px": 130.0}}
    connector.get_fills.return_value = [_fill(9, BUY, 0.38, 130, START_MS + 1000)]
    await g.run_round()
    kw = connector.place_stop.call_args.kwargs
    assert (kw["sz"], kw["trigger_px"]) == (0.38, 96.0)
    assert decode(kw["cloid"]).side == STOP
    assert all(decode(c.kwargs["cloid"]).side != STOP for c in connector.place_limit.call_args_list)
    # the stop is not a cell: cell 0 still shows its buy
    assert g.status()["cells"][0]["state"] == "buy"
    assert g.status()["stop_loss"] == 96.0


async def test_being_stopped_out_and_resuming_are_announced(config, connector, alerter, clock):
    g = _stop_grid(config, connector, alerter, clock)
    await g.start()
    connector.get_open_orders.return_value = _resting_buys(g, 0, 1, 2, 3, 4)
    connector.get_positions.return_value = {"BTC": {"szi": 0.42, "entry_px": 120.0}}
    connector.get_price.return_value = 115.0
    await g.run_round()                                  # holding coin (on hold, no fills)
    connector.get_open_orders.return_value = []
    connector.get_positions.return_value = {}
    connector.get_price.return_value = 94.0              # the stop went off
    await g.run_round()
    assert "stopped_out" in _alert_types(alerter)
    connector.place_limit.reset_mock()
    await g.run_round()                                  # still below the grid: nothing
    connector.place_limit.assert_not_called()
    connector.get_price.return_value = 125.0             # back inside
    await g.run_round()
    assert connector.place_limit.await_count > 0
    assert _alert_types(alerter).count("grid_resumed") == 1


async def test_a_manual_close_inside_the_grid_is_not_a_stop(config, connector, alerter, clock):
    g = _stop_grid(config, connector, alerter, clock)
    await g.start()
    connector.get_positions.return_value = {"BTC": {"szi": 0.42, "entry_px": 120.0}}
    await g.run_round()
    connector.get_positions.return_value = {}
    await g.run_round()                                  # flat at 155: not below the grid
    assert "stopped_out" not in _alert_types(alerter)


async def test_a_refused_start_is_alerted_and_retried(grid, connector, alerter, monkeypatch):
    import src.strategy as s
    connector.set_leverage.side_effect = [RuntimeError("Invalid leverage value"), None]
    sleeps = []

    async def fake_sleep(d):
        sleeps.append(d)
        if len(sleeps) > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(s.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await grid.run_loop()
    msgs = [c.args[1]["message"] for c in alerter.send_alert.call_args_list if c.args[0] == "error"]
    assert any("Invalid leverage" in m for m in msgs)
    assert connector.set_leverage.await_count == 2      # it tried again


# ── one alert per order, not per piece ─────────────────────────────────────────

async def test_a_buy_filled_in_pieces_is_announced_once_when_complete(grid, connector, alerter, clock):
    # Seen live: one buy filled in three pieces across two rounds and sent three
    # identical "BUY filled" messages.
    await grid.start()
    cloid3 = encode(grid.fp, 3, BUY)
    connector.get_order_status.return_value = {"oid": 9, "cloid": cloid3, "status": "open"}
    resting = _book_order(grid, 3, BUY, 0.14, oid=9, orig=0.38)
    connector.get_open_orders.return_value = _resting_buys(grid, 0, 1, 2, 4, 5) + [resting]
    connector.get_positions.return_value = {"BTC": {"szi": 0.24, "entry_px": 130.0}}
    connector.get_fills.return_value = [_fill(9, BUY, 0.10, 130, START_MS + 1000),
                                        _fill(9, BUY, 0.14, 130, START_MS + 2000)]
    await grid.run_round()
    assert "buy_filled" not in _alert_types(alerter)          # still resting

    connector.get_open_orders.return_value = _resting_buys(grid, 0, 1, 2, 4, 5)
    connector.get_positions.return_value = {"BTC": {"szi": 0.38, "entry_px": 130.0}}
    connector.get_fills.return_value.append(_fill(9, BUY, 0.14, 130, START_MS + 3000))
    clock.ms += 30_000
    await grid.run_round()
    await grid.run_round()
    filled = [c.args[1] for c in alerter.send_alert.call_args_list if c.args[0] == "buy_filled"]
    assert len(filled) == 1
    assert filled[0]["size"] == pytest.approx(0.38)


async def test_a_sell_filled_in_pieces_closes_one_cycle(grid, connector, alerter):
    statuses = {9: encode(grid.fp, 3, BUY), 10: encode(grid.fp, 3, SELL)}
    connector.get_order_status.side_effect = lambda oid: {"oid": oid, "cloid": statuses[oid],
                                                          "status": "filled"}
    connector.get_open_orders.return_value = _resting_buys(grid, 0, 1, 2, 4, 5)
    await grid.start()
    connector.get_fills.return_value = [
        _fill(9, BUY, 0.38, 130, START_MS + 1000, fee="0.01"),
        _fill(10, SELL, 0.20, 140, START_MS + 2000, fee="0.01"),
        _fill(10, SELL, 0.18, 140, START_MS + 2500, fee="0.01"),
    ]
    await grid.run_round()
    closed = [c.args[1] for c in alerter.send_alert.call_args_list if c.args[0] == "trade_closed"]
    assert len(closed) == 1
    # (140 - 130) * 0.38 - 0.01 buy fee - 0.02 sell fees
    assert closed[0]["profit_usd"] == pytest.approx(3.77)
    assert closed[0]["size"] == pytest.approx(0.38)


async def test_a_sell_still_resting_is_not_announced_yet(grid, connector, alerter):
    statuses = {9: encode(grid.fp, 3, BUY), 10: encode(grid.fp, 3, SELL)}
    connector.get_order_status.side_effect = lambda oid: {"oid": oid, "cloid": statuses[oid],
                                                          "status": "open"}
    sell = _book_order(grid, 3, SELL, 0.18, oid=10, orig=0.38)
    connector.get_open_orders.return_value = _resting_buys(grid, 0, 1, 2, 4, 5) + [sell]
    connector.get_positions.return_value = {"BTC": {"szi": 0.18, "entry_px": 130.0}}
    await grid.start()
    connector.get_fills.return_value = [_fill(9, BUY, 0.38, 130, START_MS + 1000),
                                        _fill(10, SELL, 0.20, 140, START_MS + 2000)]
    await grid.run_round()
    assert "trade_closed" not in _alert_types(alerter)
