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
    "grid_round_duration_seconds", "One read-decide-act round of one grid",
    ["coin"], buckets=[0.1, 0.5, 1, 2, 5, 10]
)
# The grid itself, so a dashboard can draw the price between its bounds.
grid_mark_price = Gauge(
    "grid_mark_price", "The mark price the grid decides from", ["coin"]
)
grid_lower_bound = Gauge("grid_lower_bound", "Lowest line of the grid", ["coin"])
grid_upper_bound = Gauge("grid_upper_bound", "Highest line of the grid", ["coin"])
grid_stop_loss_price = Gauge(
    "grid_stop_loss_price", "Trigger price of the grid's stop; absent without one", ["coin"]
)
# Capital: both as position value, leverage included, so they compare directly.
grid_realised_equity_usd = Gauge(
    "grid_realised_equity_usd",
    "Account value of the grid's dex minus the unrealised P&L on it: what the size is "
    "computed from. One value per dex; take max by (dex) before adding dexes up",
    ["coin", "dex"]
)
grid_budget_usd = Gauge(
    "grid_budget_usd", "Position value with every cell bought: size per line x leverage x cells",
    ["coin"]
)
grid_position_value_usd = Gauge(
    "grid_position_value_usd", "Position value now: position x mark price", ["coin"]
)
grid_on_hold = Gauge(
    "grid_on_hold", "1 while new buys are held back over an unexplained position", ["coin"]
)
grid_order_placement_latency = Histogram(
    "grid_order_placement_latency_seconds", "Time to place one order",
    ["coin"], buckets=[0.1, 0.25, 0.5, 1, 2.5, 5]
)

grid_last_round_timestamp = Gauge(
    "grid_last_round_timestamp_seconds",
    "When the last complete round ended. A bot can be up and still be stuck.", ["coin"]
)

# ── Realised P&L, as the exchange has it ─────────────────────────────────────
# grid_profit_usd above is the bot's own view of completed cycles: it starts at
# zero on every restart and leaves out stop-outs. These are counters of the
# exchange's closedPnl, so increase() over any range survives a restart -- only
# fills that happen while the pod is down are missed.
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
