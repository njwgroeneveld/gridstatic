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
