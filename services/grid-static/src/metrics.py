from prometheus_client import Counter, Gauge, Histogram

grid_fills_total = Counter(
    "grid_fills_total", "Fill events detected", ["coin", "side"]
)
grid_orders_placed_total = Counter(
    "grid_orders_placed_total", "Orders placed on exchange", ["coin", "side"]
)
grid_trades_closed_total = Counter(
    "grid_trades_closed_total", "Completed buy→sell round-trips", ["coin"]
)
grid_errors_total = Counter(
    "grid_errors_total", "Errors by type", ["type"]
)
grid_profit_usd = Gauge(
    "grid_profit_usd", "Cumulative realised P&L in USD", ["coin"]
)
grid_open_trades = Gauge(
    "grid_open_trades", "Cells holding coin: a resting sell or unsold coin", ["coin"]
)
grid_active_levels = Gauge(
    "grid_active_levels", "Cells with a resting buy", ["coin"]
)
grid_price_vs_lower = Gauge(
    "grid_price_vs_lower_pct", "Distance from current price to lower bound in %", ["coin"]
)
grid_price_vs_upper = Gauge(
    "grid_price_vs_upper_pct", "Distance from current price to upper bound in %", ["coin"]
)
grid_round_duration = Histogram(
    "grid_round_duration_seconds", "One read-decide-act round",
    buckets=[0.1, 0.5, 1, 2, 5, 10]
)
grid_on_hold = Gauge(
    "grid_on_hold", "1 while new buys are held back over an unexplained position", ["coin"]
)
grid_order_placement_latency = Histogram(
    "grid_order_placement_latency_seconds", "Time to place one order",
    ["coin"], buckets=[0.1, 0.25, 0.5, 1, 2.5, 5]
)

# ── What the exchange says, read every round ──────────────────────────────────
# The P&L above is the bot's own view of completed cycles and starts at zero on
# every restart. These come from the exchange: the realised ones are counters,
# so increase() over any range survives a restart -- only fills that happen
# while the pod is down are missed.
grid_position = Gauge(
    "grid_position", "Position in coin, as the exchange reports it", ["coin"]
)
grid_position_value_usd = Gauge(
    "grid_position_value_usd", "Position times the mark price", ["coin"]
)
grid_unrealized_pnl_usd = Gauge(
    "grid_unrealized_pnl_usd", "(mark - entry price) x position", ["coin"]
)
grid_realized_gain_usd = Counter(
    "grid_realized_gain_usd", "Sum of positive closedPnl of this grid's fills", ["coin"]
)
grid_realized_loss_usd = Counter(
    "grid_realized_loss_usd", "Sum of negative closedPnl of this grid's fills, "
    "stop-outs included, as a positive number", ["coin"]
)
grid_fees_usd = Counter(
    "grid_fees_usd", "Fees paid on this grid's fills", ["coin"]
)
grid_account_value_usd = Gauge(
    "grid_account_value_usd", "Account value on the perp dex the grid trades on", ["dex"]
)
