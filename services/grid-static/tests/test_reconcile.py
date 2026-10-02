"""The decision logic, on a small grid: lines 100, 110, ..., 200 (11 lines,
10 cells). Cell i buys at LEVELS[i] and sells at LEVELS[i + 1]. $50 per line at
1x, so a full buy on cell 3 (130) is round(50 / 130, 2) = 0.38 coin."""

import itertools

import pytest

from src.cloid import BUY, SELL, STOP, encode, fingerprint
from src.grid_math import calculate_levels
from src.reconcile import TaggedFill, plan

LEVELS = calculate_levels(100, 200, 11)
FP = fingerprint("TEST", 100, 200, 11, 1)
OLD_FP = fingerprint("TEST", 90, 200, 11, 1)
_oids = itertools.count(1000)


def order(cell, side, sz, orig=None, ts=0, oid=None, fp=FP):
    return {
        "coin": "TEST",
        "oid": oid if oid is not None else next(_oids),
        "cloid": encode(fp, cell, side),
        "side": "B" if side == BUY else "A",
        "limitPx": str(LEVELS[cell] if side == BUY else LEVELS[cell + 1]),
        "sz": str(sz),
        "origSz": str(orig if orig is not None else sz),
        "timestamp": ts,
    }


def stop(sz, trigger, ts=0, oid=None):
    return {"coin": "TEST", "oid": oid if oid is not None else next(_oids),
            "cloid": encode(FP, 0, STOP), "side": "A", "limitPx": str(trigger * 0.9),
            "sz": str(sz), "origSz": str(sz), "timestamp": ts,
            "isTrigger": True, "triggerPx": str(trigger), "reduceOnly": True}


def fill(cell, side, sz, t):
    return TaggedFill(cell=cell, side=side, sz=sz, time=t)


def run(orders=(), fills=(), position=0.0, price=155.0, size_usd=50.0, stop_loss=None):
    return plan(levels=LEVELS, fp=FP, orders=list(orders), fills=list(fills),
                position=position, price=price, size_usd=size_usd, leverage=1,
                sz_decimals=2, min_notional=10.0, stop_loss=stop_loss)


def buys(p):
    return {x.cell: x for x in p.places if x.side == BUY}


def sells(p):
    return {x.cell: x for x in p.places if x.side == SELL}


def stops(p):
    return [x for x in p.places if x.side == STOP]


def resting_buys(*cells):
    return [order(c, BUY, round(50 / LEVELS[c], 2)) for c in cells]


# ── incident 1 first: a filled buy must never be bought again ──────────────────

def test_incident_1_unexplained_position_blocks_every_buy():
    # Cell 3's buy filled and left the book; its sell was never placed, and the
    # fill is out of reach. The cell looks empty -- that is exactly how the line
    # got bought eight times. The position gives it away.
    p = run(orders=resting_buys(0, 1, 2, 4, 5), position=0.38)
    assert p.hold is not None
    assert buys(p) == {}
    assert not sells(p)              # nothing tells which cell the coin is in


def test_a_filled_buy_found_in_the_fills_gets_its_sell_not_a_new_buy():
    p = run(orders=resting_buys(0, 1, 2, 4, 5), position=0.38,
            fills=[fill(3, BUY, 0.38, t=1000)])
    assert p.hold is None
    assert 3 not in buys(p)
    s = sells(p)[3]
    assert (s.price, s.sz, s.reduce_only) == (140.0, 0.38, True)


# ── the ordinary cycle ─────────────────────────────────────────────────────────

def test_empty_book_places_buys_below_the_price_only():
    p = run()
    assert sorted(buys(p)) == [0, 1, 2, 3, 4, 5]
    assert buys(p)[3].price == 130.0
    assert buys(p)[3].sz == 0.38
    assert not buys(p)[3].reduce_only
    assert p.hold is None and not p.cancels and not sells(p)


def test_a_complete_grid_needs_nothing():
    p = run(orders=resting_buys(0, 1, 2, 3, 4, 5))
    assert p.places == [] and p.cancels == [] and p.hold is None


def test_a_sold_cell_gets_its_buy_back():
    p = run(orders=resting_buys(0, 1, 2, 4, 5),
            fills=[fill(3, BUY, 0.38, t=1000), fill(3, SELL, 0.38, t=2000)])
    assert p.hold is None
    assert buys(p)[3].price == 130.0
    assert not sells(p)


def test_a_cell_with_its_sell_resting_is_taken():
    p = run(orders=resting_buys(0, 1, 2, 4, 5) + [order(3, SELL, 0.38, ts=2000)],
            fills=[fill(3, BUY, 0.38, t=1000)], position=0.38)
    assert p.places == [] and p.hold is None


# ── partial fills ──────────────────────────────────────────────────────────────

def test_a_partial_buy_gets_a_sell_for_the_filled_part():
    p = run(orders=resting_buys(0, 1, 2, 4, 5) + [order(3, BUY, 0.18, orig=0.38)],
            fills=[fill(3, BUY, 0.20, t=1000)], position=0.20)
    assert p.hold is None
    assert sells(p)[3].sz == 0.20
    assert 3 not in buys(p)          # the rest of the buy is still resting
    assert p.cancels == []


def test_the_second_piece_replaces_the_sell_with_a_bigger_one():
    # The first 0.20 got its sell at t=2000; the remaining 0.18 filled at 3000
    # and took the buy off the book. The cell has a sell, so it is not "empty"
    # -- but 0.18 of it has no exit yet.
    old_sell = order(3, SELL, 0.20, ts=2000)
    p = run(orders=resting_buys(0, 1, 2, 4, 5) + [old_sell],
            fills=[fill(3, BUY, 0.20, t=1000), fill(3, BUY, 0.18, t=3000)],
            position=0.38)
    assert p.hold is None
    assert [c.oid for c in p.cancels] == [old_sell["oid"]]
    s = sells(p)[3]
    assert s.sz == 0.38 and s.replaces == old_sell["oid"]


def test_a_sell_cancelled_before_its_replacement_is_recovered():
    # Crash between cancelling the small sell and placing the bigger one: no
    # sell rests, and every buy since the last sell fill is unsold.
    p = run(orders=resting_buys(0, 1, 2, 4, 5),
            fills=[fill(3, BUY, 0.20, t=1000), fill(3, BUY, 0.18, t=3000)],
            position=0.38)
    assert p.hold is None
    assert sells(p)[3].sz == 0.38
    assert 3 not in buys(p)


def test_a_partly_sold_cell_counts_what_is_still_resting():
    p = run(orders=resting_buys(0, 1, 2, 4, 5) + [order(3, SELL, 0.18, orig=0.38, ts=2000)],
            fills=[fill(3, BUY, 0.38, t=1000), fill(3, SELL, 0.20, t=2500)],
            position=0.18)
    assert p.places == [] and p.hold is None


# ── minimum order size ─────────────────────────────────────────────────────────

def test_a_sell_below_the_minimum_waits_and_keeps_the_cell_taken():
    # 0.05 * 140 = $7: the exchange would reject it. Wait for more fills.
    p = run(orders=resting_buys(0, 1, 2, 4, 5),
            fills=[fill(3, BUY, 0.05, t=1000)], position=0.05)
    assert p.hold is None
    assert not sells(p)
    assert 3 not in buys(p)


def test_a_buy_below_the_minimum_is_skipped_with_an_alert():
    p = run(size_usd=5.0)
    assert buys(p) == {}
    assert p.alerts


# ── what the grid does not touch ───────────────────────────────────────────────

def test_lines_close_to_the_price_get_no_buy():
    # Within a tenth of a spacing below the price a buy would sit on top of the
    # market; the old grid had the same rule.
    p = run(price=150.5)
    assert sorted(buys(p)) == [0, 1, 2, 3, 4]


def test_the_top_line_never_gets_a_buy():
    # Its sell would have no line to go to.
    p = run(price=250.0)
    assert sorted(buys(p)) == list(range(10))


def test_orders_without_our_magic_are_ignored():
    stranger = {"coin": "TEST", "oid": 1, "cloid": None, "side": "B",
                "limitPx": "120", "sz": "1", "origSz": "1", "timestamp": 0}
    p = run(orders=[stranger])
    assert p.cancels == [] and p.hold is None
    assert sorted(buys(p)) == [0, 1, 2, 3, 4, 5]


# ── things that are wrong ──────────────────────────────────────────────────────

def test_a_second_buy_on_a_cell_is_cancelled_keeping_the_older():
    older = order(2, BUY, 0.42, ts=1000)
    newer = order(2, BUY, 0.42, ts=2000)
    p = run(orders=resting_buys(0, 1, 3, 4, 5) + [older, newer])
    assert [c.oid for c in p.cancels] == [newer["oid"]]
    assert 2 not in buys(p)


def test_a_second_sell_on_a_cell_is_cancelled_keeping_the_newest():
    # The newest sell is sized to cover everything; the old one is what a
    # failed cancel left behind.
    stale = order(3, SELL, 0.20, ts=2000)
    current = order(3, SELL, 0.38, ts=3000)
    p = run(orders=resting_buys(0, 1, 2, 4, 5) + [stale, current],
            fills=[fill(3, BUY, 0.20, t=1000), fill(3, BUY, 0.18, t=2500)],
            position=0.38)
    assert [c.oid for c in p.cancels] == [stale["oid"]]
    assert p.hold is None
    assert not sells(p)


def test_orders_of_an_earlier_grid_put_the_grid_on_hold_untouched():
    old = order(3, SELL, 0.38, ts=2000, fp=OLD_FP)
    p = run(orders=resting_buys(0, 1, 2) + [old], position=0.38)
    assert p.hold is not None
    assert p.cancels == []
    assert buys(p) == {}


def test_sells_exceeding_the_position_hold_without_new_orders():
    p = run(orders=resting_buys(0, 1, 2, 4, 5) + [order(3, SELL, 0.38, ts=2000)],
            fills=[fill(3, BUY, 0.38, t=1000)], position=0.10)
    assert p.hold is not None
    assert p.places == []


def test_a_short_position_holds_everything():
    p = run(position=-0.5)
    assert p.hold is not None
    assert p.places == []


def test_a_position_within_half_a_lot_is_not_a_mismatch():
    p = run(orders=resting_buys(0, 1, 2, 3, 4, 5), position=0.004)
    assert p.hold is None


# ── what /status shows ─────────────────────────────────────────────────────────

def test_every_cell_is_reported():
    p = run(orders=resting_buys(0, 1, 2, 4, 5) + [order(3, SELL, 0.38, ts=2000)],
            fills=[fill(3, BUY, 0.38, t=1000)], position=0.38)
    assert len(p.cells) == 10
    states = {c.cell: c.state for c in p.cells}
    assert states[3] == "sell"
    assert states[0] == "buy"
    assert states[9] == "empty"


@pytest.mark.parametrize("position", [0.38, 0.0])
def test_residual_is_reported(position):
    p = run(orders=resting_buys(0, 1, 2, 4, 5), position=position)
    assert p.residual == pytest.approx(position)


def test_a_forced_hold_blocks_buys_but_not_sells():
    p = plan(levels=LEVELS, fp=FP, orders=resting_buys(0, 1, 2, 4, 5),
             fills=[fill(3, BUY, 0.38, t=1000)], position=0.38, price=155.0,
             size_usd=50.0, leverage=1, sz_decimals=2, min_notional=10.0,
             forced_hold="reading the wrong account")
    assert p.hold == "reading the wrong account"
    assert buys(p) == {}
    assert sells(p)[3].sz == 0.38



# ── a flat position: every cell is free ────────────────────────────────────────

def test_a_flat_position_cancels_stale_sells_and_frees_the_cells():
    # After a stop -- or a manual close -- the position is gone but the sells and
    # the fill history are still there. Nothing can be sold any more.
    old_sell = order(3, SELL, 0.38, ts=2000)
    p = run(orders=resting_buys(0, 1, 2, 4, 5) + [old_sell],
            fills=[fill(3, BUY, 0.38, t=1000)], position=0.0)
    assert [c.oid for c in p.cancels] == [old_sell["oid"]]
    assert p.hold is None
    assert buys(p)[3].price == 130.0          # the cell buys again


def test_a_flat_position_ignores_unsold_coin_from_the_fills():
    p = run(orders=resting_buys(0, 1, 2, 4, 5),
            fills=[fill(3, BUY, 0.38, t=1000)], position=0.0)
    assert not sells(p) and p.hold is None and 3 in buys(p)


def test_after_a_stop_no_buys_while_the_price_is_below_the_grid():
    p = run(position=0.0, price=95.0, stop_loss=96.0)
    assert buys(p) == {} and p.hold is None


def test_the_grid_resumes_once_the_price_is_back_inside():
    p = run(position=0.0, price=125.0, stop_loss=96.0)
    assert sorted(buys(p)) == [0, 1, 2]          # 100, 110, 120 are below 125 - 1


# ── the stop-loss order ────────────────────────────────────────────────────────

def test_no_stop_without_a_stop_loss():
    p = run(orders=resting_buys(0, 1, 2, 4, 5) + [order(3, SELL, 0.38, ts=2000)],
            fills=[fill(3, BUY, 0.38, t=1000)], position=0.38)
    assert stops(p) == []


def test_a_position_gets_a_stop_of_its_size():
    p = run(orders=resting_buys(0, 1, 2, 4, 5) + [order(3, SELL, 0.38, ts=2000)],
            fills=[fill(3, BUY, 0.38, t=1000)], position=0.38, stop_loss=96.0)
    [s] = stops(p)
    assert (s.sz, s.price, s.reduce_only) == (0.38, 96.0, True)


def test_no_stop_while_flat():
    assert stops(run(position=0.0, stop_loss=96.0)) == []


def test_a_stop_that_fits_is_left_alone():
    p = run(orders=resting_buys(0, 1, 2, 4, 5) + [order(3, SELL, 0.38, ts=2000), stop(0.38, 96.0)],
            fills=[fill(3, BUY, 0.38, t=1000)], position=0.38, stop_loss=96.0)
    assert stops(p) == [] and p.cancels == []


def test_a_stop_grows_with_the_position():
    old = stop(0.38, 96.0)
    p = run(orders=resting_buys(0, 1, 4, 5) + [order(3, SELL, 0.38, ts=2000),
                                               order(2, SELL, 0.42, ts=2100), old],
            fills=[fill(3, BUY, 0.38, t=1000), fill(2, BUY, 0.42, t=1100)],
            position=0.80, stop_loss=96.0)
    [s] = stops(p)
    assert s.sz == 0.80 and s.replaces == old["oid"]
    assert old["oid"] in [c.oid for c in p.cancels]


def test_a_moved_stop_loss_moves_the_stop():
    old = stop(0.38, 96.0)
    p = run(orders=resting_buys(0, 1, 2, 4, 5) + [order(3, SELL, 0.38, ts=2000), old],
            fills=[fill(3, BUY, 0.38, t=1000)], position=0.38, stop_loss=90.0)
    [s] = stops(p)
    assert s.price == 90.0 and s.replaces == old["oid"]


def test_a_stop_is_cancelled_when_flat_or_unconfigured():
    old = stop(0.38, 96.0)
    assert old["oid"] in [c.oid for c in run(orders=[old], position=0.0, stop_loss=96.0).cancels]
    p = run(orders=resting_buys(0, 1, 2, 4, 5) + [order(3, SELL, 0.38, ts=2000), old],
            fills=[fill(3, BUY, 0.38, t=1000)], position=0.38, stop_loss=None)
    assert [c.oid for c in p.cancels] == [old["oid"]] and stops(p) == []


def test_a_stop_is_kept_on_hold():
    # A hold stops new buys; it must not take the protection away.
    p = run(orders=resting_buys(0, 1, 2, 3, 4, 5), position=0.5, stop_loss=96.0)
    assert p.hold is not None
    assert stops(p)[0].sz == 0.5


def test_a_second_stop_is_cancelled():
    a, b = stop(0.38, 96.0, ts=1), stop(0.38, 96.0, ts=2)
    p = run(orders=resting_buys(0, 1, 2, 4, 5) + [order(3, SELL, 0.38, ts=2000), a, b],
            fills=[fill(3, BUY, 0.38, t=1000)], position=0.38, stop_loss=96.0)
    assert [c.oid for c in p.cancels] == [a["oid"]] and stops(p) == []


def test_stops_do_not_count_as_sells():
    # A resting stop covers the position too -- counting it would make the
    # position look over-covered and put the grid on hold.
    p = run(orders=resting_buys(0, 1, 2, 4, 5) + [order(3, SELL, 0.38, ts=2000), stop(0.38, 96.0)],
            fills=[fill(3, BUY, 0.38, t=1000)], position=0.38, stop_loss=96.0)
    assert p.hold is None and p.residual == pytest.approx(0.0)
