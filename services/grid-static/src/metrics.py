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
    "grid_open_trades", "Currently open grid trades", ["coin"]
)
grid_active_levels = Gauge(
    "grid_active_levels", "BUY orders currently OPEN on exchange", ["coin"]
)
grid_price_vs_lower = Gauge(
    "grid_price_vs_lower_pct", "Distance from current price to lower bound in %", ["coin"]
)
grid_price_vs_upper = Gauge(
    "grid_price_vs_upper_pct", "Distance from current price to upper bound in %", ["coin"]
)
grid_fill_loop_duration = Histogram(
    "grid_fill_loop_duration_seconds", "Fill detection loop duration",
    buckets=[0.1, 0.5, 1, 2, 5, 10]
)
grid_health_loop_duration = Histogram(
    "grid_health_loop_duration_seconds", "Health check loop duration",
    buckets=[0.1, 0.5, 1, 2, 5, 10]
)
grid_order_placement_latency = Histogram(
    "grid_order_placement_latency_seconds", "Time to place one order",
    ["coin"], buckets=[0.1, 0.25, 0.5, 1, 2.5, 5]
)
