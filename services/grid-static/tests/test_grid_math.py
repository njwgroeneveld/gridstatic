import pytest
from src.grid_math import calculate_levels, calculate_size_usd, get_buy_levels, find_level_index


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


def test_calculate_size_usd():
    # balance=10000, strategy=80%, coin=100%, 10 lines → 800
    size = calculate_size_usd(balance=10000, strategy_pct=80, coin_pct=100, num_lines=10)
    assert size == pytest.approx(800.0, rel=1e-6)


def test_calculate_size_usd_split():
    # balance=10000, strategy=80%, coin=50%, 10 lines → 400
    size = calculate_size_usd(balance=10000, strategy_pct=80, coin_pct=50, num_lines=10)
    assert size == pytest.approx(400.0, rel=1e-6)


def test_get_buy_levels_excludes_above_price():
    levels = [57000, 58222, 59444, 60666, 61888, 63111, 64333, 65555, 66777, 68000]
    buys = get_buy_levels(levels, current_price=63000)
    assert all(l < 63000 for l in buys)
    assert 63111 not in buys


def test_get_buy_levels_excludes_level_at_price():
    levels = [60000, 61000, 62000, 63000, 64000]
    buys = get_buy_levels(levels, current_price=63000)
    assert 63000 not in buys


def test_find_level_index_exact():
    levels = [57000.0, 58222.2, 59444.4, 60666.6]
    assert find_level_index(levels, 57000.0) == 0
    assert find_level_index(levels, 58222.2) == 1


def test_find_level_index_not_found():
    levels = [57000.0, 58222.2, 59444.4]
    assert find_level_index(levels, 99999.0) is None
