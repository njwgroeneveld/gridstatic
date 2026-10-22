import pytest
from src.grid_math import calculate_levels, realised_equity, size_per_line


def test_calculate_levels_count():
    levels = calculate_levels(lower=57000, upper=68000, num_lines=10)
    assert len(levels) == 10


def test_calculate_levels_bounds():
    levels = calculate_levels(lower=57000, upper=68000, num_lines=10)
    assert levels[0] == pytest.approx(57000, rel=1e-6)
    assert levels[-1] == pytest.approx(68000, rel=1e-6)


def test_calculate_levels_spacing():
    levels = calculate_levels(lower=57000, upper=68000, num_lines=10)
    spacings = [levels[i+1] - levels[i] for i in range(len(levels)-1)]
    for s in spacings:
        assert s == pytest.approx(spacings[0], rel=1e-4)


def test_realised_equity_takes_off_every_unrealised_pnl_on_the_dex():
    positions = {"BTC": {"szi": 0.002, "entry_px": 83000, "unrealized_pnl": -0.74},
                 "ETH": {"szi": 0.1, "entry_px": 2500, "unrealized_pnl": 3.0}}
    assert realised_equity(413.66, positions) == pytest.approx(411.40)


def test_realised_equity_without_positions_is_the_account_value():
    assert realised_equity(105.0, {}) == 105.0


def test_a_position_without_the_field_is_unreadable():
    # An older connector does not send it. Sizing from the plain account value would
    # shrink the lines in a drawdown without anyone noticing; the round skips instead.
    with pytest.raises(KeyError):
        realised_equity(100.0, {"BTC": {"szi": 0.1}})


def test_size_per_line_is_the_grid_s_share_over_its_lines():
    assert size_per_line(414.39, allocation_pct=90, num_lines=10) == pytest.approx(37.30)
    assert size_per_line(105.38, allocation_pct=90, num_lines=10) == pytest.approx(9.48)


def test_size_per_line_never_goes_below_zero():
    assert size_per_line(-50.0, allocation_pct=90, num_lines=10) == 0.0
