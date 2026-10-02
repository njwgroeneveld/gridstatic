"""Decide what a grid needs, from what the exchange says right now.

No I/O and no memory: the caller reads the open orders, the position and the
recent fills, and this module works out which orders to cancel and which to
place. Everything the bot decides is decided here, which is what makes it
testable without an exchange.

The grid is made of cells. Cell i buys at levels[i] and sells at levels[i + 1];
the cloid of every order says which cell it belongs to (see cloid.py).

Two rules carry the safety of it:

- A cell's unsold coin is the coin it bought *after* its current sell was
  placed -- or, without a resting sell, after its last sell fill. The bot sizes
  every sell to cover everything the cell holds at that moment, so anything
  bought later has no exit yet. That one rule covers a partial fill, a buy that
  filled completely and left the book, a restart, and a crash between pulling a
  small sell and placing the bigger one.

- Every coin of the position has to be accounted for -- by a resting sell or by
  a cell's unsold coin -- before a single new buy goes out. A buy too few costs
  one missed cycle; a buy too many is what stacked eight buys on one line.
"""

from collections import defaultdict
from dataclasses import dataclass, field

from src.cloid import BUY, SELL, decode

# Within this fraction of a spacing below the price, a buy would sit on top of
# the market. The old grid used the same margin.
_NEAR_PRICE = 0.1


@dataclass(frozen=True)
class TaggedFill:
    """A fill of this grid, already tied to its cell through the order's cloid."""
    cell: int
    side: str
    sz: float
    time: int


@dataclass(frozen=True)
class Cancel:
    oid: int
    cell: int
    side: str
    reason: str


@dataclass(frozen=True)
class Place:
    cell: int
    side: str
    price: float
    sz: float
    reduce_only: bool
    # The sell this one grows. Place it only if cancelling that one succeeded,
    # or the cell ends up with two sells.
    replaces: int | None = None


@dataclass(frozen=True)
class CellView:
    cell: int
    buy_price: float
    sell_price: float
    buy_sz: float
    sell_sz: float
    unsold: float
    state: str  # "unsold", "sell", "buy" or "empty"


@dataclass
class Plan:
    cancels: list[Cancel] = field(default_factory=list)
    places: list[Place] = field(default_factory=list)
    hold: str | None = None
    alerts: list[str] = field(default_factory=list)
    cells: list[CellView] = field(default_factory=list)
    residual: float = 0.0


def _ts(order: dict) -> int:
    return int(order.get("timestamp") or 0)


def _sz(order: dict) -> float:
    return float(order["sz"])


def plan(*, levels: list[float], fp: bytes, orders: list[dict],
         fills: list[TaggedFill], position: float, price: float,
         size_usd: float, leverage: float, sz_decimals: int,
         min_notional: float = 10.0) -> Plan:
    out = Plan()
    n_cells = len(levels) - 1
    tol = 10 ** -sz_decimals / 2

    # ── Sort our orders into cells ────────────────────────────────────────────
    buys_in: dict[int, list[dict]] = defaultdict(list)
    sells_in: dict[int, list[dict]] = defaultdict(list)
    earlier_grid: list[dict] = []
    for o in orders:
        tag = decode(o.get("cloid"))
        if tag is None:
            continue  # not ours: a manual order or another bot
        if tag.fingerprint != fp or not 0 <= tag.cell < n_cells:
            earlier_grid.append(o)
            continue
        (buys_in if tag.side == BUY else sells_in)[tag.cell].append(o)

    # ── One buy and one sell per cell ─────────────────────────────────────────
    # The oldest buy keeps its place in the queue. The newest sell is the one
    # sized to cover the whole cell; an older one is a cancel that failed.
    buy: dict[int, dict] = {}
    sell: dict[int, dict] = {}
    for cell, found in buys_in.items():
        found.sort(key=_ts)
        buy[cell] = found[0]
        out.cancels += [Cancel(o["oid"], cell, BUY, "second buy on one cell")
                        for o in found[1:]]
    for cell, found in sells_in.items():
        found.sort(key=_ts)
        sell[cell] = found[-1]
        out.cancels += [Cancel(o["oid"], cell, SELL, "superseded by a newer sell")
                        for o in found[:-1]]

    # ── Unsold coin per cell ──────────────────────────────────────────────────
    last_sell_fill: dict[int, int] = {}
    for f in fills:
        if f.side == SELL and f.time > last_sell_fill.get(f.cell, -1):
            last_sell_fill[f.cell] = f.time
    unsold: dict[int, float] = defaultdict(float)
    for f in fills:
        if f.side != BUY:
            continue
        since = _ts(sell[f.cell]) if f.cell in sell else last_sell_fill.get(f.cell, -1)
        if f.time > since:
            unsold[f.cell] += f.sz

    covered = sum(_sz(o) for o in sell.values())
    out.residual = position - covered - sum(unsold.values())

    # ── Hold: no new buys while anything is unexplained ───────────────────────
    if position < -tol:
        out.hold = f"short position of {position} -- a grid only ever holds long"
    elif earlier_grid:
        out.hold = (f"{len(earlier_grid)} order(s) from an earlier grid on this coin -- "
                    f"cancel them, or restore the old settings")
    elif out.residual > tol:
        out.hold = (f"position {position} has {out.residual:.6g} that no cell accounts for "
                    f"(resting sells {covered:.6g}, unsold {sum(unsold.values()):.6g})")
    elif out.residual < -tol:
        out.hold = (f"resting sells of {covered:.6g} exceed what the position "
                    f"{position} can cover")
    if out.hold:
        out.alerts.append(f"grid on hold: {out.hold}")

    # ── Sells: give every unsold coin an exit ─────────────────────────────────
    # Allowed on hold too, unless the position is already short of the sells.
    if position >= -tol and out.residual >= -tol:
        for cell in sorted(unsold):
            if unsold[cell] <= tol:
                continue
            current = sell.get(cell)
            total = round((_sz(current) if current else 0.0) + unsold[cell], sz_decimals)
            px = levels[cell + 1]
            if total * px < min_notional:
                continue  # the exchange would refuse it; wait for more fills
            replaces = None
            if current is not None:
                out.cancels.append(Cancel(current["oid"], cell, SELL, "grown by a later fill"))
                replaces = current["oid"]
            out.places.append(Place(cell, SELL, px, total, True, replaces))

    # ── Buys: every free cell below the price ─────────────────────────────────
    if out.hold is None:
        threshold = price - (levels[1] - levels[0]) * _NEAR_PRICE
        too_small = []
        for cell in range(n_cells):
            if cell in buy or cell in sell or unsold.get(cell, 0.0) > tol:
                continue
            px = levels[cell]
            if px >= threshold:
                continue
            sz = round(size_usd * leverage / px, sz_decimals)
            if sz * px < min_notional:
                too_small.append(cell)
                continue
            out.places.append(Place(cell, BUY, px, sz, False))
        if too_small:
            out.alerts.append(f"buys on cells {too_small} are under the ${min_notional:g} "
                              f"minimum -- raise startBalance or lower the line count")

    # ── What /status shows ────────────────────────────────────────────────────
    for cell in range(n_cells):
        b, s, u = buy.get(cell), sell.get(cell), unsold.get(cell, 0.0)
        state = ("unsold" if u > tol else "sell" if s else "buy" if b else "empty")
        out.cells.append(CellView(cell, levels[cell], levels[cell + 1],
                                  _sz(b) if b else 0.0, _sz(s) if s else 0.0,
                                  round(u, sz_decimals), state))
    return out
