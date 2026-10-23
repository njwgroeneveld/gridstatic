"""One static grid: read the exchange, let reconcile decide, act, report.

There is no record of its own. Every round starts from what the exchange says
-- open orders, the position, recent fills -- so a restart is just another
round. The only memory is a cache of which cloid each order id carried; losing
it costs a few lookups, never a wrong decision.
"""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import asdict

from src import metrics
from src.alert_throttle import OutsideGridThrottle
from src.cloid import BUY, SELL, STOP, decode, encode, fingerprint
from src.grid_math import calculate_levels, capped_equity, realised_equity, size_per_line
from src.reconcile import Place, Plan, TaggedFill, plan

log = logging.getLogger(__name__)

MIN_NOTIONAL = 10.0                 # Hyperliquid refuses orders below $10
_LOOKUPS_PER_ROUND = 50             # order-status calls to tie fills to cells
_REPEAT_MS = 60 * 60 * 1000         # a standing hold or notice is repeated hourly
_DAY_MS = 24 * 60 * 60 * 1000
_UNREADABLE_ALERT_AFTER = 3          # failed rounds in a row before an alert

def describe(e: Exception) -> str:
    """The kind of error and its message. A timeout has no message of its own,
    and "exchange unreadable: " with nothing after it hides what went wrong. An
    error answer from the connector carries the exchange's reason as `detail`."""
    msg = str(e)
    text = f"{type(e).__name__}: {msg}" if msg else type(e).__name__
    response = getattr(e, "response", None)
    try:
        detail = response.json().get("detail") if response is not None else None
    except Exception:
        detail = None
    return f"{text} ({detail})" if detail else text


def dex_of(coin: str) -> str:
    """The perp dex a coin trades on: "xyz" for "xyz:XYZ100", "" by default."""
    return coin.split(":", 1)[0] if ":" in coin else ""


class StaticGrid:
    def __init__(self, coin_key: str, config: dict, connector, alerter, *,
                 interval: float = 30, lookback_hours: float = 72, clock=time.time) -> None:
        self.coin_key = coin_key
        self.config = config
        self.coin = config["coin"]
        self.leverage = float(config.get("leverage", 1))
        self.levels = calculate_levels(config["lower"], config["upper"], config["num_lines"])
        self.fp = fingerprint(self.coin, config["lower"], config["upper"],
                              config["num_lines"], self.leverage)
        # Margin per line. Set every round from the realised equity of the grid's
        # dex (see _set_size), so profit is reinvested without a record of it.
        self.size_usd: float | None = None
        self.equity_usd: float | None = None   # the realised equity the size came from
        # Optional ceiling on the equity it sizes from; None means no limit.
        cap = config.get("max_equity_usd")
        self.max_equity_usd = float(cap) if cap is not None else None
        self.capped = False
        self._cap_reached = False                # announce once, on reaching the cap
        # Optional. A reduce-only stop on the exchange at this price; see reconcile.
        self.stop_loss = float(config["stop_loss"]) if config.get("stop_loss") else None
        self.connector = connector
        self.alerter = alerter
        self.interval = interval
        self.lookback_ms = int(lookback_hours * 3600 * 1000)
        self._clock = clock
        self.sz_decimals: int | None = None

        self._cloid_of: dict[int, str | None] = {}
        self._started_ms = self._now_ms()
        self._announced: dict[int, int] = {}    # oid -> fill time, see _announce_fills
        self._counted: dict[str, int] = {}      # fill id -> fill time, see _count_realised
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
        self._stopped_out = False     # announced once, cleared by the first buy after it
        # A labelled counter exists only once touched. Without these, gain - loss
        # in PromQL is empty for as long as the grid has made no loss.
        for counter in (metrics.grid_realized_gain_usd, metrics.grid_realized_loss_usd,
                        metrics.grid_fees_usd, metrics.grid_trades_closed_total):
            counter.labels(coin=self.coin)
        for side in (BUY, SELL):
            metrics.grid_fills_total.labels(coin=self.coin, side=side)
            metrics.grid_orders_placed_total.labels(coin=self.coin, side=side)
        metrics.grid_lower_bound.labels(coin=self.coin).set(self.levels[0])
        metrics.grid_upper_bound.labels(coin=self.coin).set(self.levels[-1])
        if self.stop_loss is not None:
            metrics.grid_stop_loss_price.labels(coin=self.coin).set(self.stop_loss)

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def _set_size(self, equity: float) -> None:
        self.equity_usd = round(equity, 2)
        metrics.grid_realised_equity_usd.labels(
            coin=self.coin, dex=dex_of(self.coin) or "default").set(self.equity_usd)
        was_capped = self.capped
        self.capped = self.max_equity_usd is not None and equity > self.max_equity_usd
        if self.capped and not was_capped:
            self._cap_reached = True
        self.size_usd = size_per_line(capped_equity(equity, self.max_equity_usd),
                                      self.config["allocation_pct"], self.config["num_lines"])
        metrics.grid_budget_usd.labels(coin=self.coin).set(
            self.size_usd * self.leverage * (len(self.levels) - 1))

    async def _announce_cap(self) -> None:
        """Once, when the equity passes max_equity_usd: from here the grid stops growing."""
        if not self._cap_reached:
            return
        self._cap_reached = False
        msg = (f"{self.coin_key} reached its max_equity_usd of ${self.max_equity_usd:,.0f}: it "
               f"sizes from that, while ${self.equity_usd:,.0f} is on its dex. Profit above it "
               f"stays on the dex; raise max_equity_usd to let the grid grow.")
        log.info(msg)
        await self.alerter.send_alert("error", {"coin": self.coin, "message": msg})

    def _label(self) -> dict:
        lev = int(self.leverage) if self.leverage.is_integer() else self.leverage
        return {"coin": self.coin, "num_lines": self.config["num_lines"], "leverage": lev}

    # ── Starting up ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        self.sz_decimals = await self.connector.get_sz_decimals(self.coin)
        await self.connector.set_leverage(self.coin, int(self.leverage))
        self._started_ms = self._now_ms()
        # Tie every recent fill to its cell before the first round, so that round
        # does not go on hold over fills it simply had not looked up yet.
        try:
            fills = await self.connector.get_fills(self.coin, self._now_ms() - self.lookback_ms)
            await self._tie(fills, limit=None)
        except Exception as e:
            log.warning(f"[{self.coin_key}] warming the fill cache failed: {describe(e)}")
        await self._check_size()
        await self._announce_cap()
        size = f"${self.size_usd} per line now" if self.size_usd is not None else "size unread"
        log.info(f"[{self.coin_key}] started: {len(self.levels)} lines "
                 f"{self.levels[0]}-{self.levels[-1]}, {size}, {self.leverage:g}x, "
                 f"{self.config['allocation_pct']:g}% of its dex")

    async def _check_size(self) -> None:
        """Warn, not refuse: orders under the exchange's minimum leave cells empty
        until the dex holds more -- not a reason to stop the grid."""
        dex = dex_of(self.coin)
        try:
            # A HIP-3 dex keeps a balance of its own; that is the one that pays.
            value, positions = await asyncio.gather(
                self.connector.get_account_value(dex=dex),
                self.connector.get_positions(dex=dex))
        except Exception as e:
            log.warning(f"[{self.coin_key}] size check skipped: {describe(e)}")
            return
        equity = realised_equity(value, positions)
        self._set_size(equity)
        order = self.size_usd * self.leverage
        if order < MIN_NOTIONAL:
            msg = (f"{self.coin_key}: ${order:.2f} per order -- {self.config['allocation_pct']:g}% "
                   f"of ${equity:,.0f} realised equity over {self.config['num_lines']} lines -- "
                   f"is under Hyperliquid's ${MIN_NOTIONAL:g} minimum. Cells stay empty until "
                   f"the dex holds more: add funds, raise allocation_pct or use fewer lines.")
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
                log.warning(f"[{self.coin_key}] order status of {oid} unreadable: {describe(e)}")
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
            # Two phases, not four reads at once. The book has to be read before
            # the position and the fills: a buy that fills in between is then
            # either still on the book or in the fills. Read the other way round,
            # it can be gone from the book and not yet in the fills -- an empty
            # cell, and a second buy on it. Position and fills may run together:
            # if they miss each other the position does not add up, and that
            # holds one round instead of buying twice.
            orders, price = await asyncio.gather(
                self.connector.get_open_orders(self.coin),
                self.connector.get_price(self.coin))
            positions, fills, value = await asyncio.gather(
                self.connector.get_positions(dex=dex_of(self.coin)),
                self.connector.get_fills(self.coin, self._now_ms() - self.lookback_ms),
                self.connector.get_account_value(dex=dex_of(self.coin)))
            # Sized every round: realised equity moves with closed profit, not with
            # the price of the open position. Orders already resting keep their size.
            self._set_size(realised_equity(value, positions))
        except Exception as e:
            # An exchange that cannot be read is never an empty exchange: act on
            # nothing until it answers again.
            log.error(f"[{self.coin_key}] round skipped, exchange unreadable: {describe(e)}")
            metrics.grid_errors_total.labels(type="read").inc()
            self._failed_reads += 1
            if self._failed_reads >= _UNREADABLE_ALERT_AFTER:
                await self._notice(f"{self.coin_key}: exchange unreadable for "
                                   f"{self._failed_reads} rounds in a row -- the grid does "
                                   f"nothing until it answers again. Last error: {describe(e)}",
                                   key="unreadable")
            return None
        self._failed_reads = 0
        await self._announce_cap()

        position = float((positions.get(self.coin) or {}).get("szi", 0.0))
        for o in orders:
            if o.get("oid") is not None:
                self._cloid_of[int(o["oid"])] = o.get("cloid")
        tied = await self._tie(fills, limit=_LOOKUPS_PER_ROUND)

        p = plan(levels=self.levels, fp=self.fp, orders=orders,
                 fills=[t for t, _ in tied], position=position, price=price,
                 size_usd=self.size_usd, leverage=self.leverage,
                 sz_decimals=self.sz_decimals, min_notional=MIN_NOTIONAL,
                 forced_hold=self._vanished(orders, fills), stop_loss=self.stop_loss)
        placed, self._placed_oids = await self._execute(p)
        await self._report(p, tied, price, {int(o["oid"]) for o in orders if o.get("oid") is not None})
        await self._report_stop(position, price, placed)
        self._count_realised(tied)

        self.last_plan, self.last_price, self.last_position = p, price, position
        self._last_placed = placed
        self.last_round_ms = self._now_ms()
        metrics.grid_round_duration.labels(coin=self.coin).observe(time.time() - t0)
        metrics.grid_last_round_timestamp.labels(coin=self.coin).set(self.last_round_ms / 1000)
        metrics.grid_position_value_usd.labels(coin=self.coin).set(position * price)
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
                log.error(f"[{self.coin_key}] cancel of {c.oid} failed: {describe(e)}")
                metrics.grid_errors_total.labels(type="order_cancel").inc()
                await self._notice(f"{self.coin_key}: a cancel was refused: {describe(e)}",
                                   key="cancel-refused")

        for pl in p.places:
            if pl.replaces is not None and pl.replaces in failed_cancels:
                # Placing it anyway would leave the cell with two sells.
                log.warning(f"[{self.coin_key}] cell {pl.cell}: bigger sell held back, "
                            f"the old one could not be cancelled")
                continue
            cloid = encode(self.fp, pl.cell, pl.side)
            t0 = time.time()
            try:
                if pl.side == STOP:
                    res = await self.connector.place_stop(
                        self.coin, sz=pl.sz, trigger_px=pl.price, cloid=cloid)
                else:
                    res = await self.connector.place_limit(
                        self.coin, is_buy=pl.side == BUY, price=pl.price, sz=pl.sz,
                        cloid=cloid, reduce_only=pl.reduce_only)
            except Exception as e:
                log.error(f"[{self.coin_key}] {pl.side} {pl.sz} at {pl.price} "
                          f"(cell {pl.cell}) failed: {describe(e)}")
                metrics.grid_errors_total.labels(type="order_placement").inc()
                # At most hourly. If every order is refused, the likely cause is the
                # key: an expired API wallet keeps reading fine and cannot trade.
                await self._notice(f"{self.coin_key}: an order was refused: {describe(e)}. "
                                   f"If every order is refused, check the key: an API wallet "
                                   f"may have expired.", key="order-refused")
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

    async def _report(self, p: Plan, tied: list[tuple[TaggedFill, dict]], price: float,
                      open_oids: set[int]) -> None:
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

        await self._announce_fills(tied, open_oids)
        self.realized_24h = round(sum(
            self._cycle(t, f, tied)[1] or 0.0 for t, f in tied
            if t.side == SELL and t.time >= now - _DAY_MS), 2)
        await self._report_price(price)

        metrics.grid_on_hold.labels(coin=self.coin).set(1 if p.hold else 0)
        metrics.grid_active_levels.labels(coin=self.coin).set(
            sum(1 for c in p.cells if c.state == "buy"))
        metrics.grid_open_trades.labels(coin=self.coin).set(
            sum(1 for c in p.cells if c.state in ("sell", "unsold")))

    async def _report_stop(self, position: float, price: float, placed: list[Place]) -> None:
        """Being stopped out, and resuming. Flat below the grid after holding coin
        means the stop went off; a close by hand inside the grid is not a stop.
        The grid resumes on its own -- this only says so."""
        held = (self.last_position or 0.0) > 0
        flat = abs(position) <= 10 ** -(self.sz_decimals or 0) / 2
        if (self.stop_loss is not None and held and flat and price < self.levels[0]
                and not self._stopped_out):
            self._stopped_out = True
            log.warning(f"[{self.coin_key}] stopped out near {self.stop_loss}")
            await self.alerter.send_alert("stopped_out", {
                **self._label(), "stop_loss": self.stop_loss, "price": price,
                "lower": self.levels[0]})
        elif self._stopped_out and any(pl.side == BUY for pl in placed):
            self._stopped_out = False
            log.info(f"[{self.coin_key}] resumed: price {price} is back inside the grid")
            await self.alerter.send_alert("grid_resumed", {**self._label(), "price": price})

    def _count_realised(self, tied: list[tuple[TaggedFill, dict]]) -> None:
        """Realised P&L as the exchange has it: the closedPnl of this grid's fills,
        stop-outs included, each fill counted once and only fills since the
        start. After a restart the counters begin again, and increase() in
        Prometheus handles that."""
        for t, f in tied:
            key = str(f.get("tid") or f"{f['oid']}-{t.time}-{f['sz']}")
            if t.time <= self._started_ms or key in self._counted:
                continue
            self._counted[key] = t.time
            pnl = float(f.get("closedPnl") or 0)
            if pnl > 0:
                metrics.grid_realized_gain_usd.labels(coin=self.coin).inc(pnl)
            elif pnl < 0:
                metrics.grid_realized_loss_usd.labels(coin=self.coin).inc(-pnl)
            metrics.grid_fees_usd.labels(coin=self.coin).inc(max(float(f.get("fee") or 0), 0.0))
        horizon = self._now_ms() - self.lookback_ms
        for key in [k for k, t in self._counted.items() if t < horizon]:
            del self._counted[key]

    async def _notice(self, msg: str, key: str | None = None) -> None:
        """An error alert, at most once an hour per kind."""
        now = self._now_ms()
        key = key or msg
        if now - self._notice_alerted_ms.get(key, -_REPEAT_MS) >= _REPEAT_MS:
            await self.alerter.send_alert("error", {"coin": self.coin, "message": msg})
            self._notice_alerted_ms[key] = now

    async def _announce_fills(self, tied: list[tuple[TaggedFill, dict]],
                              open_oids: set[int]) -> None:
        """One alert per order, once it is complete. The exchange fills an order
        in as many pieces as it likes; announcing each piece sent the same
        message three times. Only fills since the start: after a restart the
        history is still on the exchange, and announcing it again is noise."""
        pieces_of: dict[int, list[tuple[TaggedFill, dict]]] = defaultdict(list)
        for t, f in tied:
            if t.time > self._started_ms and t.side in (BUY, SELL):
                pieces_of[int(f["oid"])].append((t, f))
        done = [(oid, sorted(ps, key=lambda x: x[0].time)) for oid, ps in pieces_of.items()
                if oid not in self._announced and oid not in open_oids]
        for oid, pieces in sorted(done, key=lambda d: d[1][-1][0].time):
            self._announced[oid] = pieces[-1][0].time
            first = pieces[0][0]
            size = sum(t.sz for t, _ in pieces)
            px = (sum(t.sz * float(f["px"]) for t, f in pieces) / size if size
                  else float(pieces[-1][1]["px"]))
            metrics.grid_fills_total.labels(coin=self.coin, side=first.side).inc()
            if first.side == BUY:
                await self.alerter.send_alert("buy_filled", {
                    **self._label(), "filled_price": round(px, 8), "size": round(size, 8),
                    "sell_price": self.levels[first.cell + 1]})
                continue
            profit, buy_px = 0.0, None
            for t, f in pieces:
                b_px, piece_profit = self._cycle(t, f, tied)
                if piece_profit is None:   # its buys are older than the look-back
                    piece_profit = float(f.get("closedPnl") or 0) - float(f.get("fee") or 0)
                profit += piece_profit
                buy_px = buy_px or b_px
            profit = round(profit, 2)
            await self.alerter.send_alert("trade_closed", {
                **self._label(), "buy_price": buy_px or 0.0, "sell_price": round(px, 8),
                "size": round(size, 8), "profit_usd": profit})
            metrics.grid_trades_closed_total.labels(coin=self.coin).inc()
            metrics.grid_profit_usd.labels(coin=self.coin).inc(profit)
        # Forget orders older than the look-back: their fills are gone from it too.
        horizon = self._now_ms() - self.lookback_ms
        for oid in [o for o, t in self._announced.items() if t < horizon]:
            del self._announced[oid]

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
        return round(buy_px, 8), profit

    async def _report_price(self, price: float) -> None:
        lower, upper = self.levels[0], self.levels[-1]
        spacing = self.levels[1] - self.levels[0]
        metrics.grid_mark_price.labels(coin=self.coin).set(price)
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
                log.error(f"[{self.coin_key}] start failed, retrying: {describe(e)}")
                metrics.grid_errors_total.labels(type="start").inc()
                await self._notice(f"{self.coin_key} cannot start and keeps retrying: {describe(e)}",
                                   key="start")
                await asyncio.sleep(self.interval)
        while True:
            try:
                await self.run_round()
            except Exception as e:
                log.exception(f"[{self.coin_key}] round failed: {describe(e)}")
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
            "equity_usd": self.equity_usd,
            "max_equity_usd": self.max_equity_usd,
            "capped": self.capped,
            "stop_loss": self.stop_loss,
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
            if pl.side == STOP:
                continue    # the stop belongs to the grid, not to a cell
            c = cells[pl.cell]
            if pl.side == BUY:
                c.update(state="buy" if c["state"] == "empty" else c["state"], buy_sz=pl.sz)
            else:
                c.update(state="sell", sell_sz=pl.sz, unsold=0.0)
        return [cells[i] for i in sorted(cells)]
