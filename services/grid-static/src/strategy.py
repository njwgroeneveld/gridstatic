import asyncio
import logging
import random
import time
from datetime import datetime, timezone

from shared.base_strategy import BaseStrategy
from shared.dal_client import DALClient
from shared.connector_client import ConnectorClient
from shared.alerter_client import AlerterClient
from src.grid_math import calculate_levels, calculate_size_usd, get_buy_levels, find_level_index
from src.alert_throttle import OutsideGridThrottle
from src import metrics

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> int:
    return int(time.time() * 1000)


MAKER_FEE_RATE = 0.00015  # Hyperliquid maker: 0.0150%


def _sz_coin(size_usd: float, leverage: float, price: float) -> float:
    """Order size in coin. Leverage multiplies the position -- the leverage
    setting on the exchange only lowers the margin requirement, it does not
    size anything -- so notional is size_usd * leverage, matching how
    _calc_profit accounts for a leveraged line."""
    return round(size_usd * leverage / price, 6)


def _modelled_fee(size_usd: float, leverage: float) -> float:
    """Shadow has no fill to read a fee off, so model it at the maker rate:
    grid orders rest away from the market and are practically always maker."""
    return round(size_usd * leverage * MAKER_FEE_RATE, 6)


async def _occupied_levels(dal: DALClient, coin: str, grid_config_id: int) -> set[int]:
    """Grid lines that must not be bought again -- two kinds, and the second is
    the one that used to slip through.

    A line with a resting order is obviously taken. A line whose buy already
    filled is just as taken: it holds a position waiting on its sell one level
    up. It leaves no resting order behind, though, so looking only at open
    orders makes it read as free, and the health loop re-buys it every cycle.

    Matched on the level, never on the price. The connector rounds every limit
    price to five significant digits, the exchange fills at a price of its own,
    and a partial fill averages the entry across pieces -- so a trade's
    buy_price never equals its grid line on a live grid. In shadow it does,
    since no exchange is involved, which is why a price match held up there for
    months while stacking positions on the live grid.
    """
    orders = await dal.get_open_orders(coin)
    levels = {o["level"] for o in orders if o.get("grid_config_id") == grid_config_id}
    for trade in await dal.get_open_trades(coin, grid_config_id):
        if trade.get("buy_level") is not None:
            levels.add(trade["buy_level"])
    return levels


class StaticGrid(BaseStrategy):
    def __init__(self, coin_key: str, config: dict, strategy_allocation_pct: float,
                 dal: DALClient, connector: ConnectorClient, alerter: AlerterClient,
                 fill_interval: int = 5, health_interval: int = 30,
                 start_balance: float = 10000.0) -> None:
        self.coin_key = coin_key
        self.coin = config["coin"]
        self.config = config
        self.strategy_allocation_pct = strategy_allocation_pct
        self.shadow: bool = config.get("shadow", True)
        self.dal = dal
        self.connector = connector
        self.alerter = alerter
        self.fill_interval = fill_interval
        self.health_interval = health_interval
        self.start_balance = start_balance
        self.levels: list[float] = []
        self.grid_config_id: int | None = None
        self.last_fill_ms: int = _now_ms()
        self._last_shadow_price: float | None = None
        self._last_error_alert_ms: int = 0
        self._outside_throttle = OutsideGridThrottle()
        self._edge_throttle = OutsideGridThrottle()

    # ── Initialization ────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        log.info(f"[{self.coin_key}] Initialising grid (shadow={self.shadow})")
        mids = await self.connector.get_mids()
        current_price = mids[self.coin]

        balance = self.start_balance
        size_usd = calculate_size_usd(
            balance=balance,
            strategy_pct=self.strategy_allocation_pct,
            coin_pct=self.config["allocation_pct"],
            num_lines=self.config["num_lines"],
        )

        self.levels = calculate_levels(
            lower=self.config["lower"],
            upper=self.config["upper"],
            num_lines=self.config["num_lines"],
        )

        cfg = await self.dal.insert_grid_config({
            "coin": self.coin,
            "strategy": "STATIC",
            "upper": self.config["upper"],
            "lower": self.config["lower"],
            "num_lines": self.config["num_lines"],
            "leverage": self.config.get("leverage", 1),
            "shadow": self.shadow,
            "active": True,
        })
        self.grid_config_id = cfg["id"]

        if not self.shadow:
            await self.connector.set_leverage(self.coin, int(self.config.get("leverage", 1)))

        buy_levels = get_buy_levels(self.levels, current_price)
        for level_price in buy_levels:
            level_idx = find_level_index(self.levels, level_price)
            await self._place_buy(level_idx, level_price, size_usd)

        await self.alerter.send_alert("grid_initialized", {
            "coin": self.coin, "lower": self.config["lower"],
            "upper": self.config["upper"], "num_lines": self.config["num_lines"],
            "shadow": self.shadow,
        })
        log.info(f"[{self.coin_key}] Grid ready — {len(buy_levels)} BUY orders placed")

    async def _place_buy(self, level_idx: int, price: float, size_usd: float) -> dict | None:
        t0 = time.time()
        exchange_oid = None
        sz_coin = None

        if not self.shadow:
            result = await self.connector.place_buy_limit(
                coin=self.coin, price=price, size_usd=size_usd,
                leverage=int(self.config.get("leverage", 1))
            )
            if result.get("status") != "ok":
                log.error(f"[{self.coin_key}] BUY order failed at {price}: {result}")
                metrics.grid_errors_total.labels(type="order_placement").inc()
                return None
            exchange_oid = result.get("hl_order_id")
            sz_coin = result.get("sz_coin")
            metrics.grid_order_placement_latency.labels(coin=self.coin).observe(time.time() - t0)

        order = await self.dal.insert_grid_order({
            "grid_config_id": self.grid_config_id,
            "coin": self.coin,
            "strategy": "STATIC",
            "side": "BUY",
            "level": level_idx,
            "price": price,
            "size_usd": size_usd,
            "exchange_order_id": exchange_oid,
            "status": "OPEN",
            "shadow": self.shadow,
        })
        metrics.grid_orders_placed_total.labels(coin=self.coin, side="BUY").inc()
        metrics.grid_active_levels.labels(coin=self.coin).inc()

        if not self.shadow:
            await self.alerter.send_alert("buy_placed", {
                "coin": self.coin, "price": price, "level": level_idx, "shadow": False
            })
        return order

    async def _place_sell(self, level_idx: int, price: float,
                          sz_coin: float, size_usd: float) -> dict | None:
        t0 = time.time()
        exchange_oid = None

        if not self.shadow:
            result = await self.connector.place_sell_limit(self.coin, sz_coin, price)
            if result.get("status") != "ok":
                log.error(f"[{self.coin_key}] SELL order failed at {price}: {result}")
                metrics.grid_errors_total.labels(type="order_placement").inc()
                return None
            exchange_oid = result.get("hl_order_id")
            metrics.grid_order_placement_latency.labels(coin=self.coin).observe(time.time() - t0)

        order = await self.dal.insert_grid_order({
            "grid_config_id": self.grid_config_id,
            "coin": self.coin,
            "strategy": "STATIC",
            "side": "SELL",
            "level": level_idx,
            "price": price,
            "size_usd": size_usd,
            "exchange_order_id": exchange_oid,
            "status": "OPEN",
            "shadow": self.shadow,
        })
        metrics.grid_orders_placed_total.labels(coin=self.coin, side="SELL").inc()
        return order

    async def _current_size_usd(self) -> float:
        # Fixed starting capital plus what this grid actually realised -- the same
        # rule live and in shadow, so the two stay comparable. Deliberately not the
        # live account value: that moves with unrealised P&L, so lines would shrink
        # exactly while the grid is buying its way down.
        trades = await self.dal.get_closed_trades(self.coin, self.grid_config_id)
        total_profit = sum(float(t.get("profit_usd") or 0) for t in trades)
        balance = self.start_balance + total_profit
        return calculate_size_usd(
            balance=balance,
            strategy_pct=self.strategy_allocation_pct,
            coin_pct=self.config["allocation_pct"],
            num_lines=self.config["num_lines"],
        )

    # ── State recovery ────────────────────────────────────────────────────────

    async def recover_state(self) -> None:
        log.info(f"[{self.coin_key}] Recovering state from DAL + exchange")
        dal_open = await self.dal.get_open_orders(self.coin)
        if not dal_open:
            return

        if self.shadow:
            return  # shadow has no exchange orders to reconcile

        try:
            exchange_orders = await self.connector.get_open_orders(self.coin)
        except Exception as e:
            # An unreachable exchange must not read as "the book is empty": every
            # resting order would look filled, and each one would get a sell
            # placed against a position that does not exist. Skip reconciling and
            # let the fill loop report what really happened.
            log.error(f"[{self.coin_key}] Recovery overgeslagen -- open orders onleesbaar: {e}")
            metrics.grid_errors_total.labels(type="recover_state").inc()
            return
        exchange_oids = {str(o.get("oid", "")) for o in exchange_orders}

        for order in dal_open:
            oid = order.get("exchange_order_id")
            if oid and oid not in exchange_oids:
                log.info(f"[{self.coin_key}] Missed fill detected for order {order['id']} (oid={oid})")
                await self._handle_missed_fill(order)

    async def _handle_missed_fill(self, order: dict) -> None:
        filled_price = order["price"]
        await self.dal.patch_order(order["id"], {
            "status": "FILLED",
            "filled_price": filled_price,
            "filled_at": _now_iso(),
        })

        if order["side"] == "BUY":
            size_usd = order["size_usd"]
            leverage = float(self.config.get("leverage", 1))
            sz_coin = _sz_coin(size_usd, leverage, filled_price)
            # A missed fill was never observed, so its fee has to be estimated
            # the same way the fill price is -- better than a NULL that silently
            # counts as zero when the trade closes.
            fee_usd = _modelled_fee(size_usd, leverage)
            trade = await self.dal.insert_grid_trade({
                "coin": self.coin, "strategy": "STATIC",
                "buy_order_id": order["id"], "buy_price": filled_price,
                "size_usd": size_usd, "status": "OPEN", "shadow": False,
                "fee_usd": fee_usd,
            })
            sell_level = order["level"] + 1
            if sell_level < len(self.levels):
                sell_price = self.levels[sell_level]
                sell_order = await self._place_sell(sell_level, sell_price, sz_coin, size_usd)
                if sell_order:
                    await self.dal.patch_trade(trade["id"], {"sell_order_id": sell_order["id"]})

    # ── Fill detection loop ───────────────────────────────────────────────────

    async def run_fill_loop(self) -> None:
        fill_interval = self.fill_interval
        await asyncio.sleep(random.uniform(0, fill_interval))
        while True:
            t0 = time.time()
            try:
                if self.shadow:
                    await self._shadow_fill_check()
                else:
                    await self._live_fill_check()
            except Exception as e:
                log.error(f"[{self.coin_key}] Fill loop error: {e}")
                metrics.grid_errors_total.labels(type="fill_loop").inc()
                now = _now_ms()
                if now - self._last_error_alert_ms > 5 * 60 * 1000:
                    self._last_error_alert_ms = now
                    await self.alerter.send_alert("error", {"coin": self.coin, "message": str(e)})
            metrics.grid_fill_loop_duration.observe(time.time() - t0)
            await asyncio.sleep(fill_interval)

    async def _shadow_fill_check(self) -> None:
        mids = await self.connector.get_mids()
        current_price = mids[self.coin]
        if self._last_shadow_price is None:
            self._last_shadow_price = current_price
            return

        all_open = await self.dal.get_open_orders(self.coin)
        # Filter by this grid's config_id — multiple BTC grids share the same coin
        # but must only process their own orders
        my_orders = [o for o in all_open if o.get("grid_config_id") == self.grid_config_id]
        buy_orders = [o for o in my_orders if o["side"] == "BUY"]

        for order in buy_orders:
            if self._last_shadow_price > order["price"] >= current_price:
                sell_order = await self._process_buy_fill(order, order["price"], known_orders=my_orders)
                if sell_order:
                    my_orders.append(sell_order)

        sell_orders = [o for o in my_orders if o["side"] == "SELL"]
        for order in sell_orders:
            if self._last_shadow_price < order["price"] <= current_price:
                await self._process_sell_fill(order, order["price"])

        self._last_shadow_price = current_price

    async def _live_fill_check(self) -> None:
        since = self.last_fill_ms
        now = _now_ms()
        fills = await self.connector.get_fills(self.coin, since)
        # Watermark moves only once the fills are in hand. Advancing it first meant
        # a failed call silently dropped that whole window, and those fills were
        # never looked at again -- they resurfaced as "missed fills" on the next
        # restart, booked at the line price instead of what they really filled at.
        self.last_fill_ms = now

        open_orders = await self.dal.get_open_orders(self.coin)
        oid_to_order = {o["exchange_order_id"]: o for o in open_orders if o.get("exchange_order_id")}

        for fill in fills:
            oid = str(fill.get("oid", ""))
            order = oid_to_order.get(oid)
            if not order:
                continue
            filled_price = float(fill.get("px", order["price"]))
            metrics.grid_fills_total.labels(coin=self.coin, side=order["side"]).inc()

            if order["side"] == "BUY":
                sell_order = await self._process_buy_fill(order, filled_price,
                                                          known_orders=open_orders, fill=fill)
                if sell_order:
                    open_orders.append(sell_order)
            elif order["side"] == "SELL":
                await self._process_sell_fill(order, filled_price, fill=fill)

    async def _cancel_existing_sell(self, trade: dict) -> None:
        """A later piece of the same buy order grew the position, so the sell that
        covered the first piece is now too small. Pull it before the bigger one
        goes out, otherwise the surplus sits there with no exit."""
        sell_id = trade.get("sell_order_id")
        if not sell_id:
            return
        rows = await self.dal.get_open_orders(self.coin)
        sell = next((o for o in rows if o["id"] == sell_id), None)
        if sell is None:
            return
        if not self.shadow and sell.get("exchange_order_id"):
            try:
                await self.connector.cancel_order(self.coin, sell["exchange_order_id"])
            except Exception as e:
                log.error(f"[{self.coin_key}] kon verkooporder {sell_id} niet intrekken: {e}")
                metrics.grid_errors_total.labels(type="order_cancel").inc()
                return
        await self.dal.patch_order(sell_id, {"status": "CANCELLED"})

    async def _process_buy_fill(self, order: dict, filled_price: float,
                                known_orders: list[dict] | None = None,
                                fill: dict | None = None) -> dict | None:
        leverage = float(self.config.get("leverage", 1))
        if fill:
            sz = float(fill["sz"])
            px = float(fill.get("px", filled_price))
            fee = float(fill["fee"])
        else:
            sz = _sz_coin(order["size_usd"], leverage, filled_price)
            px = filled_price
            fee = _modelled_fee(order["size_usd"], leverage)

        await self.dal.patch_order(order["id"], {
            "status": "FILLED",
            "filled_price": px,
            "filled_at": _now_iso(),
        })

        # An exchange splits a limit order into as many fills as it likes, and each
        # piece arrives separately. Keep one trade per buy order and grow it, or every
        # extra piece becomes inventory that nothing ever sells.
        open_trades = await self.dal.get_open_trades(self.coin)
        trade = next((t for t in open_trades if t.get("buy_order_id") == order["id"]), None)

        notional = sz * px
        if trade:
            eerder = float(trade["size_usd"]) * leverage
            sz += eerder / float(trade["buy_price"])
            notional += eerder
            fee = round(float(trade.get("fee_usd") or 0) + fee, 6)
            px = notional / sz  # gewogen gemiddelde instap over alle stukken
            await self.dal.patch_trade(trade["id"], {
                "buy_price": px,
                "size_usd": round(notional / leverage, 2),
                "fee_usd": fee,
            })
            await self._cancel_existing_sell(trade)
        else:
            metrics.grid_active_levels.labels(coin=self.coin).dec()
            trade = await self.dal.insert_grid_trade({
                "coin": self.coin, "strategy": "STATIC",
                "buy_order_id": order["id"], "buy_price": px,
                "size_usd": round(notional / leverage, 2),
                "status": "OPEN", "shadow": self.shadow, "fee_usd": fee,
            })
            metrics.grid_open_trades.labels(coin=self.coin).inc()

        await self.dal.patch_order(order["id"], {"fee_usd": fee})

        sz = round(sz, 6)
        size_usd = round(notional / leverage, 2)
        sell_order = None
        sell_level = order["level"] + 1
        # No "is this level already covered" check any more: two different buy
        # orders can legitimately both exit one level up, and skipping the second
        # was exactly what left positions without a sell.
        if sell_level < len(self.levels):
            sell_price = self.levels[sell_level]
            sell_order = await self._place_sell(sell_level, sell_price, sz, size_usd)
            if sell_order:
                await self.dal.patch_trade(trade["id"], {"sell_order_id": sell_order["id"]})

        await self.alerter.send_alert("buy_filled", {
            "coin": self.coin,
            "num_lines": self.config["num_lines"],
            "leverage": self.config.get("leverage", 1),
            "filled_price": px,
            "sell_price": self.levels[sell_level] if sell_level < len(self.levels) else 0,
            "shadow": self.shadow,
        })
        return sell_order

    async def _process_sell_fill(self, order: dict, filled_price: float,
                                 fill: dict | None = None) -> None:
        await self.dal.patch_order(order["id"], {
            "status": "FILLED",
            "filled_price": filled_price,
            "filled_at": _now_iso(),
        })

        open_trades = await self.dal.get_open_trades(self.coin)
        trade = next((t for t in open_trades if t.get("sell_order_id") == order["id"]), None)

        profit_usd = 0.0
        buy_price = 0.0
        leverage = self.config.get("leverage", 1)
        if trade:
            buy_price = trade.get("buy_price", 0)
            size_usd = trade.get("size_usd", 0)
            sell_fee = float(fill["fee"]) if fill else _modelled_fee(size_usd, leverage)
            await self.dal.patch_order(order["id"], {"fee_usd": sell_fee})
            # trade.fee_usd already carries the buy-side fee.
            fee_usd = round(float(trade.get("fee_usd") or 0) + sell_fee, 6)
            if buy_price > 0:
                gross = (filled_price - buy_price) / buy_price * size_usd * leverage
                profit_usd = round(gross - fee_usd, 2)
            await self.dal.patch_trade(trade["id"], {
                "sell_price": filled_price,
                "profit_usd": profit_usd,
                "fee_usd": fee_usd,
                "status": "CLOSED",
                "closed_at": _now_iso(),
            })
            metrics.grid_trades_closed_total.labels(coin=self.coin).inc()
            metrics.grid_open_trades.labels(coin=self.coin).dec()
            metrics.grid_profit_usd.labels(coin=self.coin).inc(profit_usd)

        buy_level = order["level"] - 1
        if buy_level >= 0 and buy_level not in await _occupied_levels(
                self.dal, self.coin, self.grid_config_id):
            buy_price_new = self.levels[buy_level]
            size_usd = await self._current_size_usd()
            await self._place_buy(buy_level, buy_price_new, size_usd)

        await self.alerter.send_alert("trade_closed", {
            "coin": self.coin,
            "num_lines": self.config["num_lines"],
            "leverage": self.config.get("leverage", 1),
            "buy_price": buy_price,
            "sell_price": filled_price,
            "profit_usd": profit_usd,
            "shadow": self.shadow,
        })

    # ── Health check loop ─────────────────────────────────────────────────────

    async def run_health_loop(self) -> None:
        health_interval = self.health_interval
        await asyncio.sleep(random.uniform(0, health_interval))
        while True:
            t0 = time.time()
            try:
                await self._health_check()
            except Exception as e:
                log.error(f"[{self.coin_key}] Health loop error: {e}")
                metrics.grid_errors_total.labels(type="health_loop").inc()
            metrics.grid_health_loop_duration.observe(time.time() - t0)
            await asyncio.sleep(health_interval)

    async def _record_funding(self) -> None:
        """Funding is charged hourly on the net position, moves every hour and can
        even flip sign, so it is read from the exchange rather than modelled.
        Shadow holds no position to be charged on and stays out of this."""
        if self.shadow:
            return
        try:
            sinds = await self.dal.get_last_funding_ms(self.coin, False)
            if sinds is None:
                sinds = _now_ms() - 7 * 24 * 3600 * 1000
            for rec in await self.connector.get_funding(sinds + 1):
                if rec["coin"] != self.coin:
                    continue
                rec["grid_config_id"] = self.grid_config_id
                rec["shadow"] = False
                await self.dal.insert_grid_funding(rec)
        except Exception as e:
            log.warning(f"[{self.coin_key}] funding bijwerken mislukt: {e}")

    async def _health_check(self) -> None:
        await self._record_funding()
        mids = await self.connector.get_mids()
        price = mids.get(self.coin, 0)
        lower = self.config["lower"]
        upper = self.config["upper"]

        if self.levels:
            pct_from_lower = (price - lower) / lower * 100
            pct_from_upper = (upper - price) / upper * 100
            metrics.grid_price_vs_lower.labels(coin=self.coin).set(pct_from_lower)
            metrics.grid_price_vs_upper.labels(coin=self.coin).set(pct_from_upper)

        spacing = (upper - lower) / (self.config["num_lines"] - 1) if self.config["num_lines"] > 1 else 0

        num_lines = self.config["num_lines"]
        leverage = self.config.get("leverage", 1)

        if price < lower:
            if self._outside_throttle.should_alert("bottom"):
                await self.alerter.send_alert("outside_grid", {
                    "coin": self.coin, "price": price, "bound": lower, "side": "bottom",
                    "num_lines": num_lines, "leverage": leverage,
                })
            self._edge_throttle.clear("bottom")
        else:
            self._outside_throttle.clear("bottom")
            if price - lower < spacing:
                if self._edge_throttle.should_alert("bottom"):
                    await self.alerter.send_alert("edge_warning", {
                        "coin": self.coin, "side": "bottom", "level_price": lower,
                        "num_lines": num_lines, "leverage": leverage,
                    })
            else:
                self._edge_throttle.clear("bottom")

        if price > upper:
            if self._outside_throttle.should_alert("top"):
                await self.alerter.send_alert("outside_grid", {
                    "coin": self.coin, "price": price, "bound": upper, "side": "top",
                    "num_lines": num_lines, "leverage": leverage,
                })
            self._edge_throttle.clear("top")
        else:
            self._outside_throttle.clear("top")
            if upper - price < spacing:
                if self._edge_throttle.should_alert("top"):
                    await self.alerter.send_alert("edge_warning", {
                        "coin": self.coin, "side": "top", "level_price": upper,
                        "num_lines": num_lines, "leverage": leverage,
                    })
            else:
                self._edge_throttle.clear("top")

        await self._backfill_missing_buy_orders(price)

    async def _backfill_missing_buy_orders(self, price: float) -> None:
        if not self.levels or self.grid_config_id is None:
            return
        occupied = await _occupied_levels(self.dal, self.coin, self.grid_config_id)
        size_usd = await self._current_size_usd()
        for idx, level_price in enumerate(self.levels):
            if level_price >= price or idx in occupied:
                continue
            log.info(f"[{self.coin_key}] Backfilling missing BUY at level {idx} (${level_price})")
            await self._place_buy(idx, level_price, size_usd)
