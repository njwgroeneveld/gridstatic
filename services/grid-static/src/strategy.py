"""One static grid: read the exchange, let reconcile decide, act, report.

There is no record of its own. Every round starts from what the exchange says
-- open orders, the position, recent fills -- so a restart is just another
round. The only memory is a cache of which cloid each order id carried; losing
it costs a few lookups, never a wrong decision.
"""

import asyncio
import logging
import time
from dataclasses import asdict

from src import metrics
from src.alert_throttle import OutsideGridThrottle
from src.cloid import BUY, SELL, decode, encode, fingerprint
from src.grid_math import calculate_levels, calculate_size_usd
from src.reconcile import Place, Plan, TaggedFill, plan

log = logging.getLogger(__name__)

MIN_NOTIONAL = 10.0                 # Hyperliquid refuses orders below $10
_LOOKUPS_PER_ROUND = 50             # order-status calls to tie fills to cells
_REPEAT_MS = 60 * 60 * 1000         # a standing hold or notice is repeated hourly
_DAY_MS = 24 * 60 * 60 * 1000
_UNREADABLE_ALERT_AFTER = 3          # failed rounds in a row before an alert


def dex_of(coin: str) -> str:
    """The perp dex a coin trades on: "xyz" for "xyz:XYZ100", "" by default."""
    return coin.split(":", 1)[0] if ":" in coin else ""


class StaticGrid:
    def __init__(self, coin_key: str, config: dict, strategy_allocation_pct: float,
                 start_balance: float, connector, alerter, *, interval: float = 30,
                 lookback_hours: float = 72, clock=time.time) -> None:
        self.coin_key = coin_key
        self.config = config
        self.coin = config["coin"]
        self.leverage = float(config.get("leverage", 1))
        self.levels = calculate_levels(config["lower"], config["upper"], config["num_lines"])
        self.fp = fingerprint(self.coin, config["lower"], config["upper"],
                              config["num_lines"], self.leverage)
        # Fixed per line, no compounding: the profit since the start is not
        # something the exchange can tell once its fill history has rolled over.
        self.size_usd = calculate_size_usd(start_balance, strategy_allocation_pct,
                                           config["allocation_pct"], config["num_lines"])
        self.connector = connector
        self.alerter = alerter
        self.interval = interval
        self.lookback_ms = int(lookback_hours * 3600 * 1000)
        self._clock = clock
        self.sz_decimals: int | None = None

        self._cloid_of: dict[int, str | None] = {}
        self._announced_until_ms = self._now_ms()
        self._hold: str | None = None
        self._hold_alerted_ms = 0
        self._notice_alerted_ms: dict[str, int] = {}
        self._failed_reads = 0
        self._outside = OutsideGridThrottle()
        self._edge = OutsideGridThrottle()

        self.last_plan: Plan | None = None
        self._last_placed: list[Place] = []
        self._placed_oids: set[int] = set()   # placed last round, see run_round
        self.last_round_ms: int | None = None
        self.last_price: float | None = None
        self.last_position: float | None = None
        self.realized_24h = 0.0

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def _label(self) -> dict:
        lev = int(self.leverage) if self.leverage.is_integer() else self.leverage
        return {"coin": self.coin, "num_lines": self.config["num_lines"], "leverage": lev}

    # ── Starting up ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        self.sz_decimals = await self.connector.get_sz_decimals(self.coin)
        await self.connector.set_leverage(self.coin, int(self.leverage))
        self._announced_until_ms = self._now_ms()
        # Tie every recent fill to its cell before the first round, so that round
        # does not go on hold over fills it simply had not looked up yet.
        try:
            fills = await self.connector.get_fills(self.coin, self._now_ms() - self.lookback_ms)
            await self._tie(fills, limit=None)
        except Exception as e:
            log.warning(f"[{self.coin_key}] warming the fill cache failed: {e}")
        await self._check_margin()
        log.info(f"[{self.coin_key}] started: {len(self.levels)} lines "
                 f"{self.levels[0]}-{self.levels[-1]}, ${self.size_usd} per line, "
                 f"{self.leverage:g}x")

    async def _check_margin(self) -> None:
        """Warn, not refuse: a grid may be set larger than what is free right now."""
        try:
            price = await self.connector.get_price(self.coin)
            # A HIP-3 dex keeps a balance of its own; that is the one that pays.
            value = await self.connector.get_account_value(dex=dex_of(self.coin))
        except Exception as e:
            log.warning(f"[{self.coin_key}] margin check skipped: {e}")
            return
        cells_below = sum(1 for p in self.levels[:-1] if p < price)
        needed = self.size_usd * cells_below
        if value < needed:
            msg = (f"{self.coin_key}: buying every line below {price:g} needs ${needed:,.0f} "
                   f"margin; the account holds ${value:,.0f}. Orders will be refused once it "
                   f"runs out -- lower startBalance or add funds.")
            log.warning(msg)
            await self.alerter.send_alert("error", {"coin": self.coin, "message": msg})

    # ── Tying fills to cells ──────────────────────────────────────────────────

    async def _tie(self, fills: list[dict], limit: int | None) -> list[tuple[TaggedFill, dict]]:
        """Fills only name the order id. Look its cloid up -- from the cache, or
        from the exchange -- and keep the fills that belong to this grid. A fill
        that could not be looked up is left out; the position check then puts
        the round on hold, which is the safe side to err on."""
        lookups = 0
        for fill in sorted(fills, key=lambda f: -int(f.get("time", 0))):
            oid = int(fill["oid"])
            if oid in self._cloid_of:
                continue
            if limit is not None and lookups >= limit:
                break
            lookups += 1
            try:
                status = await self.connector.get_order_status(oid)
            except Exception as e:
                log.warning(f"[{self.coin_key}] order status of {oid} unreadable: {e}")
                break
            # "unknownOid" may be the exchange not having caught up yet; remembering
            # it as "not ours" would keep the grid on hold for good.
            if status.get("status") != "unknownOid":
                self._cloid_of[oid] = status.get("cloid")

        tied = []
        for fill in fills:
            tag = decode(self._cloid_of.get(int(fill["oid"])))
            if tag is None or tag.fingerprint != self.fp:
                continue
            tied.append((TaggedFill(cell=tag.cell, side=tag.side, sz=float(fill["sz"]),
                                    time=int(fill["time"])), fill))
        return tied

    # ── One round ─────────────────────────────────────────────────────────────

    async def run_round(self) -> Plan | None:
        t0 = time.time()
        try:
            orders = await self.connector.get_open_orders(self.coin)
            positions = await self.connector.get_positions()
            price = await self.connector.get_price(self.coin)
            fills = await self.connector.get_fills(self.coin, self._now_ms() - self.lookback_ms)
        except Exception as e:
            # An exchange that cannot be read is never an empty exchange: act on
            # nothing until it answers again.
            log.error(f"[{self.coin_key}] round skipped, exchange unreadable: {e}")
            metrics.grid_errors_total.labels(type="read").inc()
            self._failed_reads += 1
            if self._failed_reads >= _UNREADABLE_ALERT_AFTER:
                await self._notice(f"{self.coin_key}: exchange unreadable for "
                                   f"{self._failed_reads} rounds in a row -- the grid does "
                                   f"nothing until it answers again. Last error: {e}",
                                   key="unreadable")
            return None
        self._failed_reads = 0

        position = float((positions.get(self.coin) or {}).get("szi", 0.0))
        for o in orders:
            if o.get("oid") is not None:
                self._cloid_of[int(o["oid"])] = o.get("cloid")
        tied = await self._tie(fills, limit=_LOOKUPS_PER_ROUND)

        p = plan(levels=self.levels, fp=self.fp, orders=orders,
                 fills=[t for t, _ in tied], position=position, price=price,
                 size_usd=self.size_usd, leverage=self.leverage,
                 sz_decimals=self.sz_decimals, min_notional=MIN_NOTIONAL,
                 forced_hold=self._vanished(orders, fills))
        placed, self._placed_oids = await self._execute(p)
        await self._report(p, tied, price)

        self.last_plan, self.last_price, self.last_position = p, price, position
        self._last_placed = placed
        self.last_round_ms = self._now_ms()
        metrics.grid_round_duration.observe(time.time() - t0)
        return p

    def _vanished(self, orders: list[dict], fills: list[dict]) -> str | None:
        """Every order placed last round should be on the book or filled now. If
        not one of them is, the bot is almost certainly reading another account
        than it trades on -- an API wallet without the account it trades for does
        exactly that. Every round would then see an empty book and buy a whole
        layer again. One order cancelled by hand does not trigger this."""
        if not self._placed_oids:
            return None
        seen = {int(o["oid"]) for o in orders if o.get("oid") is not None}
        seen |= {int(f["oid"]) for f in fills}
        if self._placed_oids & seen:
            return None
        return (f"none of the {len(self._placed_oids)} order(s) placed last round is on the "
                f"book or filled -- the bot may be reading another account than the one it "
                f"trades on (check wallet_address)")

    async def _execute(self, p: Plan) -> tuple[list[Place], set[int]]:
        """Carry the plan out; return the orders that went out, and their ids."""
        failed_cancels: set[int] = set()
        placed: list[Place] = []
        oids: set[int] = set()
        for c in p.cancels:
            try:
                await self.connector.cancel_order(self.coin, c.oid)
                log.info(f"[{self.coin_key}] cancelled {c.side} on cell {c.cell}: {c.reason}")
            except Exception as e:
                failed_cancels.add(c.oid)
                log.error(f"[{self.coin_key}] cancel of {c.oid} failed: {e}")
                metrics.grid_errors_total.labels(type="order_cancel").inc()

        for pl in p.places:
            if pl.replaces is not None and pl.replaces in failed_cancels:
                # Placing it anyway would leave the cell with two sells.
                log.warning(f"[{self.coin_key}] cell {pl.cell}: bigger sell held back, "
                            f"the old one could not be cancelled")
                continue
            cloid = encode(self.fp, pl.cell, pl.side)
            t0 = time.time()
            try:
                res = await self.connector.place_limit(
                    self.coin, is_buy=pl.side == BUY, price=pl.price, sz=pl.sz,
                    cloid=cloid, reduce_only=pl.reduce_only)
            except Exception as e:
                log.error(f"[{self.coin_key}] {pl.side} {pl.sz} at {pl.price} "
                          f"(cell {pl.cell}) failed: {e}")
                metrics.grid_errors_total.labels(type="order_placement").inc()
                continue
            if res.get("oid") is not None:
                self._cloid_of[int(res["oid"])] = cloid
                oids.add(int(res["oid"]))
            placed.append(pl)
            log.info(f"[{self.coin_key}] {pl.side} {pl.sz} at {pl.price} (cell {pl.cell})")
            metrics.grid_orders_placed_total.labels(coin=self.coin, side=pl.side).inc()
            metrics.grid_order_placement_latency.labels(coin=self.coin).observe(time.time() - t0)
        return placed, oids

    # ── Reporting ─────────────────────────────────────────────────────────────

    async def _report(self, p: Plan, tied: list[tuple[TaggedFill, dict]], price: float) -> None:
        now = self._now_ms()

        if p.hold and (self._hold is None or now - self._hold_alerted_ms >= _REPEAT_MS):
            log.warning(f"[{self.coin_key}] on hold: {p.hold}")
            await self.alerter.send_alert("grid_hold", {**self._label(), "reason": p.hold})
            self._hold_alerted_ms = now
        elif p.hold is None and self._hold is not None:
            log.info(f"[{self.coin_key}] hold cleared")
            await self.alerter.send_alert("hold_cleared", self._label())
        self._hold = p.hold

        for msg in p.alerts:
            await self._notice(msg)

        await self._announce_fills(tied)
        self.realized_24h = round(sum(
            self._cycle(t, f, tied)[1] or 0.0 for t, f in tied
            if t.side == SELL and t.time >= now - _DAY_MS), 2)
        await self._report_price(price)

        metrics.grid_on_hold.labels(coin=self.coin).set(1 if p.hold else 0)
        metrics.grid_active_levels.labels(coin=self.coin).set(
            sum(1 for c in p.cells if c.state == "buy"))
        metrics.grid_open_trades.labels(coin=self.coin).set(
            sum(1 for c in p.cells if c.state in ("sell", "unsold")))

    async def _notice(self, msg: str, key: str | None = None) -> None:
        """An error alert, at most once an hour per kind."""
        now = self._now_ms()
        key = key or msg
        if now - self._notice_alerted_ms.get(key, -_REPEAT_MS) >= _REPEAT_MS:
            await self.alerter.send_alert("error", {"coin": self.coin, "message": msg})
            self._notice_alerted_ms[key] = now

    async def _announce_fills(self, tied: list[tuple[TaggedFill, dict]]) -> None:
        """Only fills since the start: after a restart the history is still on the
        exchange, and announcing it again would be noise."""
        new = sorted(((t, f) for t, f in tied if t.time > self._announced_until_ms),
                     key=lambda x: x[0].time)
        for t, f in new:
            px = float(f["px"])
            metrics.grid_fills_total.labels(coin=self.coin, side=t.side).inc()
            if t.side == BUY:
                await self.alerter.send_alert("buy_filled", {
                    **self._label(), "filled_price": px, "sell_price": self.levels[t.cell + 1]})
            else:
                buy_px, profit = self._cycle(t, f, tied)
                if profit is None:
                    profit = round(float(f.get("closedPnl") or 0) - float(f.get("fee") or 0), 2)
                await self.alerter.send_alert("trade_closed", {
                    **self._label(), "buy_price": buy_px or 0.0, "sell_price": px,
                    "profit_usd": profit})
                metrics.grid_trades_closed_total.labels(coin=self.coin).inc()
                metrics.grid_profit_usd.labels(coin=self.coin).inc(profit)
        if new:
            self._announced_until_ms = new[-1][0].time

    @staticmethod
    def _cycle(sell: TaggedFill, raw: dict,
               tied: list[tuple[TaggedFill, dict]]) -> tuple[float | None, float | None]:
        """Buy price and profit of one sell fill: the run of buys in the same cell
        that this sell closes, at their volume-weighted price, with both sides'
        fees. A sell that comes in pieces shares that run in proportion."""
        run_sz = run_cost = run_fee = 0.0
        prev_side = None
        own = sorted((x for x in tied if x[0].cell == sell.cell and x[0].time <= sell.time),
                     key=lambda x: x[0].time)
        for t, f in own:
            if t.side == BUY:
                if prev_side == SELL:
                    run_sz = run_cost = run_fee = 0.0
                run_sz += t.sz
                run_cost += t.sz * float(f["px"])
                run_fee += float(f.get("fee") or 0)
            prev_side = t.side
        if run_sz <= 0:
            return None, None
        buy_px = run_cost / run_sz
        share = min(1.0, sell.sz / run_sz)
        profit = ((float(raw["px"]) - buy_px) * sell.sz
                  - float(raw.get("fee") or 0) - run_fee * share)
        return round(buy_px, 8), round(profit, 2)

    async def _report_price(self, price: float) -> None:
        lower, upper = self.levels[0], self.levels[-1]
        spacing = self.levels[1] - self.levels[0]
        metrics.grid_price_vs_lower.labels(coin=self.coin).set((price - lower) / lower * 100)
        metrics.grid_price_vs_upper.labels(coin=self.coin).set((upper - price) / upper * 100)
        for side, outside, near, bound in (("bottom", price < lower, price - lower < spacing, lower),
                                           ("top", price > upper, upper - price < spacing, upper)):
            if outside:
                if self._outside.should_alert(side):
                    await self.alerter.send_alert("outside_grid", {
                        **self._label(), "price": price, "bound": bound, "side": side})
                self._edge.clear(side)
                continue
            self._outside.clear(side)
            if near:
                if self._edge.should_alert(side):
                    await self.alerter.send_alert("edge_warning", {
                        **self._label(), "side": side, "level_price": bound})
            else:
                self._edge.clear(side)

    # ── The loop and /status ──────────────────────────────────────────────────

    async def run_loop(self) -> None:
        while True:
            try:
                await self.start()
                break
            except Exception as e:
                log.error(f"[{self.coin_key}] start failed, retrying: {e}")
                metrics.grid_errors_total.labels(type="start").inc()
                await asyncio.sleep(self.interval)
        while True:
            try:
                await self.run_round()
            except Exception as e:
                log.exception(f"[{self.coin_key}] round failed: {e}")
                metrics.grid_errors_total.labels(type="round").inc()
            await asyncio.sleep(self.interval)

    def status(self) -> dict:
        p = self.last_plan
        return {
            "coin_key": self.coin_key,
            "coin": self.coin,
            "lower": self.levels[0],
            "upper": self.levels[-1],
            "num_lines": self.config["num_lines"],
            "leverage": self._label()["leverage"],
            "size_usd": self.size_usd,
            "price": self.last_price,
            "position": self.last_position,
            "hold": self._hold,
            "residual": p.residual if p else None,
            "realized_24h": self.realized_24h,
            "last_round_ms": self.last_round_ms,
            "cells": self._cells_after_round(),
        }

    def _cells_after_round(self) -> list[dict]:
        """The cells as the book stands after the last round. The round read the
        book before it acted; without the orders it placed, /status right after
        startup shows every cell empty while the buys are already resting."""
        if self.last_plan is None:
            return []
        cells = {c.cell: asdict(c) for c in self.last_plan.cells}
        for pl in self._last_placed:
            c = cells[pl.cell]
            if pl.side == BUY:
                c.update(state="buy" if c["state"] == "empty" else c["state"], buy_sz=pl.sz)
            else:
                c.update(state="sell", sell_sz=pl.sz, unsold=0.0)
        return [cells[i] for i in sorted(cells)]
