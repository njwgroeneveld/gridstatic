def calculate_levels(lower: float, upper: float, num_lines: int) -> list[float]:
    """Calculate evenly spaced price levels between lower and upper bounds."""
    spacing = (upper - lower) / (num_lines - 1)
    levels = [lower + i * spacing for i in range(num_lines)]
    # Ensure exact bounds
    levels[0] = lower
    levels[-1] = upper
    return [round(level, 2) for level in levels]


def calculate_size_usd(balance: float, strategy_pct: float,
                       coin_pct: float, num_lines: int) -> float:
    """Calculate order size in USD per level.

    Args:
        balance: Total account balance in USD
        strategy_pct: Percentage of balance allocated to strategy (0-100)
        coin_pct: Percentage of strategy allocation for this coin (0-100)
        num_lines: Number of grid levels

    Returns:
        Order size in USD per level
    """
    return round((balance * strategy_pct / 100.0 * coin_pct / 100.0) / num_lines, 2)


def get_buy_levels(levels: list[float], current_price: float) -> list[float]:
    """Return grid levels below current price (valid buy levels).

    Args:
        levels: List of grid levels
        current_price: Current market price

    Returns:
        List of levels suitable for buying (strictly below current price)
    """
    if not levels:
        return []
    spacing = levels[1] - levels[0] if len(levels) > 1 else 1000
    threshold = current_price - spacing * 0.1
    return [l for l in levels if l < threshold]


def find_level_index(levels: list[float], price: float) -> int | None:
    """Find the index of the grid level closest to a given price.

    Args:
        levels: List of grid levels
        price: Price to match to a level

    Returns:
        Index of the closest level, or None if price is too far from any level
    """
    if not levels:
        return None
    spacing = (levels[-1] - levels[0]) / (len(levels) - 1) if len(levels) > 1 else 1
    diffs = [abs(l - price) for l in levels]
    idx = diffs.index(min(diffs))
    if diffs[idx] > spacing * 0.6:
        return None
    return idx
