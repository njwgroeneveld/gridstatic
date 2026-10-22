def calculate_levels(lower: float, upper: float, num_lines: int) -> list[float]:
    """Calculate evenly spaced price levels between lower and upper bounds."""
    spacing = (upper - lower) / (num_lines - 1)
    levels = [lower + i * spacing for i in range(num_lines)]
    # Ensure exact bounds
    levels[0] = lower
    levels[-1] = upper
    return [round(level, 2) for level in levels]


def realised_equity(account_value: float, positions: dict[str, dict]) -> float:
    """The account value of a dex minus the unrealised P&L of every position on it.

    It moves with closed profit and loss, fees, funding, deposits and withdrawals --
    not with the price of an open position. Sizing from it reinvests profit without
    a record, and does not shrink the lines while the grid buys its way down."""
    return account_value - sum(float(p.get("unrealized_pnl", 0.0)) for p in positions.values())


def size_per_line(equity: float, allocation_pct: float, num_lines: int) -> float:
    """Margin per line in USD: the grid's share of its dex's realised equity, over its lines."""
    return max(0.0, round(equity * allocation_pct / 100.0 / num_lines, 2))
