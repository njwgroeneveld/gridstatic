# Design decisions

This document explains why gridstatic is built the way it is. Several of these choices were not
made up front: they were forced by incidents, described in [postmortems.md](postmortems.md).

- [Principles](#principles)
- [The exchange is the only source of truth](#the-exchange-is-the-only-source-of-truth)
- [One round](#one-round)
- [The strategy](#the-strategy)
- [Deployment and secrets](#deployment-and-secrets)
- [Postmortems](postmortems.md)
- [Known limitations](#known-limitations)

---

## Principles

**One source of truth.** The bot keeps no record of its own. Every decision is made from what
the exchange says right now: open orders, the position, recent fills. That works because every
order carries its own label, stored by the exchange ([below](#what-every-order-carries)).

**Fail loudly rather than assume.** A failed API call must never read as an empty result.
"The exchange is unreachable" and "there are no orders" lead to opposite actions. A round that
cannot read everything does nothing.

**When in doubt, do not buy.** A buy too few costs one missed cycle. A buy too many is how a
grid once bought the same line eight times. Any position the bot cannot account for stops new
buys until it can.

**Test against a real exchange, not a simulation.** A simulation fills an order at exactly its
price. A real exchange rounds prices, and fills orders in pieces and at prices of their own. A
safeguard that relied on exact prices once passed a simulation for months and failed live
([incident 1](postmortems.md#1-eight-buys-on-one-line-2026-09-09)). So the bot is tested on
Hyperliquid's testnet, with real orders on a real order book.

**Reaching real money takes deliberate steps.** Everything defaults to testnet. Mainnet needs a
mainnet key and `hyperliquid.testnet: false`, and is done by hand.

---

## The exchange is the only source of truth

### Why the orders carry their own data

For every order on its market, a grid has to know four things: is it one of ours, which grid
placed it (and with which settings), which cell it belongs to, and is it a buy, a sell or the
stop. Hyperliquid lets every order carry a client order id (cloid): 16 bytes of your own that the
exchange stores with the order and hands back with it
([Hyperliquid docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint),
field `c`). Those 16 bytes hold all four answers. So the exchange itself is the record, and the
bot needs no database.

For one bot trading one account that is the simplest design that works: the state lives in one
place, there is nothing to keep in step, and nothing extra to run. An earlier version did keep a
database next to the exchange. That can be made to work, but it means two places that must agree
after every order, and keeping them in step is where its incidents came from
([postmortems 1 to 3](postmortems.md)). A database would earn its place again for what the
exchange does not keep: a long history, or reporting across many accounts
([known limitations](#known-limitations)).

### What every order carries

Lines `p_0 < p_1 < … < p_n-1` divide the range. A **cell** `i` buys at `p_i` and sells at
`p_i+1`. Every order belongs to exactly one cell, and its cloid says which:

| what the bot needs to know | where it is in the cloid |
|---|---|
| is this a gridstatic order? | bytes 0–1: the marker `0x6773` |
| which layout of these bytes | byte 2: a version number |
| which grid, with which settings | bytes 3–8: the grid's **fingerprint**, a hash of its coin, bounds, line count and leverage |
| which cell | bytes 9–10 |
| buy, sell or stop | byte 11 |
| (nothing: it keeps every cloid unique) | bytes 12–15: a random nonce |

The fingerprint belongs to the grid, not to a cell: all orders of one grid share it, and the
cell number tells them apart. Every round the bot sorts the orders on its market by their cloid:

| the cloid | whose it is | what the bot does |
|---|---|---|
| no gridstatic marker | someone else's: a manual order, another bot | leaves it alone. If it fills, the position no longer adds up and the grid holds ([below](#the-position-has-to-add-up)) |
| the marker, another fingerprint | an earlier grid on this market, whose settings have since changed | cancels its buys and sells; what it bought makes the grid hold until it is closed |
| the marker, its own fingerprint | one of its own cells | manages it |

So after a restart the bot reads the book and knows which cell each order is on, without a
record, and without comparing prices, which the exchange rounds and fills at prices of its own
([incident 1](postmortems.md#1-eight-buys-on-one-line-2026-09-09)).

Fills carry only the order id, not the cloid. The bot keeps an in-memory map from order id to
cloid, filled when it places an order and whenever it reads the book; anything missing is looked
up with `orderStatus`. Losing that map after a restart costs a few lookups, never a wrong
decision: a fill that cannot be tied to a cell leaves the position unexplained, and the grid
holds until it can.

### What a cell holds

Each cell has to know how much it has bought and not yet put up for sale. One rule answers that.
When the bot places a cell's sell, it sizes the sell to everything the cell holds at that moment.
So **whatever a cell holds without a sell is what it bought after its sell was placed.** A cell
with no sell on the book counts from its last sell fill instead. After a stop fill no cell holds
anything, since the stop sells the whole position.

That one rule covers a partial fill, a buy that filled
completely and left the book, a restart, and a crash between cancelling a small sell and placing
the bigger one that replaces it.

For example, cell 3 buys 0.0010 BTC at 80,000 and sells at 81,000:

| when | the exchange | bought, not yet for sale | the bot |
|---|---|---|---|
| 10:00 | the buy fills half: 0.0004 | 0.0004 (no sell yet) | places a sell of 0.0004 |
| 10:05 | the rest fills: 0.0006 | 0.0006 (bought after that sell) | cancels the 0.0004 sell, places one of 0.0010 |

A crash between that cancel and that place leaves no sell at all. After the restart the same rule
reads "bought since the last sell fill": 0.0010, and a sell of 0.0010 goes out. No special case.

### The position has to add up

`residual = position − resting sells − bought, not yet for sale`. In a healthy grid it is zero within half a
lot. A position of 0.0030 BTC, with sells of 0.0010 on cells 2 and 5 and 0.0010 unsold on cell 7,
adds up. A position of 0.0034 leaves 0.0004 BTC that no cell accounts for (bought by hand, say),
and the grid holds. A residual below zero holds too: the bot's sells and what its cells hold add
up to more than the position. So do a short position, and last round's
orders being nowhere on the book or in the fills. Every reason, with what to do about it, is in
[operations.md](operations.md#when-the-grid-holds).

On hold, sells still go out for what the cells hold, and the grid resumes by itself once
the cause is gone. The last reason matters more than it looks: with the exchange as the only truth,
a bot that reads the wrong account sees an empty book every round, finds nothing unexplained, and
would buy a full layer again every 30 seconds. An API wallet configured without the account it
trades for does exactly that.

---

## One round

Every 30 seconds each grid runs one round. These are its steps, in the order they run:

```
1. read open orders and price, together      ── any read fails → do nothing this round
2. then position and fills, together         ── never before the book: see below
3. tie fills to cells through their cloid
4. decide (reconcile): cancels, sells, buys, hold
5. cancel; then sells; then buys
6. report fills, closed cycles, hold changes
```

What each step asks the exchange, and why:

| step | request (Hyperliquid info API) | what it is for |
|---|---|---|
| 1 | `frontendOpenOrders` | the bot's resting orders, with their cloids: which cells have a buy or a sell |
| 1 | `metaAndAssetCtxs` | the mark price, which decides which lines are below the price |
| 2 | `clearinghouseState` for the grid's dex | the position, and the account value of the dex |
| 2 | `spotClearinghouseState` (main dex only) | spot USDC, which counts towards the main dex's balance |
| 2 | `userFillsByTime` | what filled since the look-back: a buy that filled and has no sell yet |
| 3 | `orderStatus`, only when needed | the cloid of a filled order that has left the book and is not in the bot's memory |
| 5 | `order`, `cancel` (exchange API, signed) | the changes `reconcile` decided on |

The account value, minus unrealised P&L, sizes the next buys
([below](#order-size-follows-realised-equity)).

On testnet a round takes 2 to 3 seconds, almost always under 5: nearly all of it is waiting for
the exchange. The grids run their rounds side by side.

The order of the reads carries a rule. A buy that fills during the round has to be seen
somewhere: still on the book, or in the fills. Reading the book first guarantees that. Reading the
fills or the position before it can catch the buy gone from the book and not yet in the fills:
an empty cell, and a second buy on it. Position and fills may be read together: when they miss
each other the position does not add up, and that costs one round on hold, not a double buy.

`reconcile` makes no network calls and remembers nothing between rounds: it gets what steps 1 to 3
read and returns what to do. It is where every decision is made, so it can be tested on its own,
case by case, without an exchange. Each safety rule was mutated away once to confirm a test fails without it.

A filled sell leaves its cell empty, and the next round puts the buy back. A cycle is not a
separate code path; it is the next round.

---

## The strategy

### Order size follows realised equity

The margin per line is set every round:

    realised equity = account value of the grid's dex − unrealised P&L of every position on it
    size per line   = realised equity × allocationPct ÷ lines

Realised equity moves with closed profit and loss, fees, funding, deposits and withdrawals, not
with the price of an open position. So profit is reinvested without the bot keeping a record of
it, and the lines do not shrink while the grid buys its way down, which the plain account value
would do. Orders already resting keep their size; only new buys use the new one.

Hyperliquid runs several perp dexes side by side under one account: the main one, with BTC, ETH
and the rest, and HIP-3 dexes that others deploy, such as `xyz` with the Nasdaq-100 (`XYZ100`).
Each dex keeps its own margin balance. Here the BTC grid trades on the main dex and the XYZ100
grid on `xyz`, so each has a balance of its own and grows with the profit made there. Grids on
one dex share it through `allocationPct`, which may add up to at most 100 per dex. The bot never
moves money between dexes; a person does, and the next round sizes from it. An optional
`maxEquityUsd` per grid caps the equity it sizes from, so a deposit meant for something else, or
one wrong reading, cannot blow up the buys of every empty cell at once.

### One buy and one sell per cell

Each cell has at most one resting buy and one resting sell. A sell on line `j` always belongs to
cell `j−1`, so two sells on one line cannot occur. When a later fill grows what a cell holds, the
bot cancels the smaller sell and places one that covers it all, and only places the new one if
the cancel succeeded, so a cell never ends up with two. Should two still appear, the bot keeps
the oldest buy (its place in the queue) and the newest sell (the one sized to the whole cell).

Sells are placed **reduce-only**. Whatever the bot believes, a sell can never open a short.

### The price is the mark price

Which lines sit below the price is decided from the mark price, not the mid. On a liquid market
the two are the same to the tick. On a thin one they are not: on testnet, `xyz:XYZ100` had one
ask and four bids, a 9% spread and $21 of daily volume, and its mid sat 3.5% above the mark.
Worse, a grid's own buys become the best bid, which moves the mid up, which lets the next round
place a buy one line higher. The grid would chase its own orders toward the ask. The mark price
is anchored to the oracle and does not move with the bot.

### HIP-3 markets

A market on a HIP-3 dex carries the dex in its name (`xyz:XYZ100`). The bot reads everything
about it from that dex, its balance included. The chart works out from the configured coins which
dexes there are, because the SDK has to be told before it can trade on one.

### No buy on the top line, none near the price

The top line has no line above it for the sell. And a buy within a tenth of a spacing below the
price would sit on top of the market.

### The stop-loss lives on the exchange, and the grid resumes on its own

The optional stop-loss is a reduce-only stop-market order on the exchange, not a price check in
the bot: it has to fire when the bot or the cluster is down, which on a home cluster with wifi
nodes is when it matters. `reconcile` keeps exactly one, the size of the whole position, and
replaces it when the position grows or the setting moves. It keeps it while the grid holds.
When it fires, it sells the whole position at market: everything every cell held.

Whether a triggered order keeps its order id and cloid is not documented, so recovery does not
depend on recognising the stop's fill. It rests on a simpler rule: **a flat position means no cell
holds coin.** Stale sells are cancelled and the fill history is ignored. That covers a stop, a
close by hand, and a stop that fills in pieces (the rest gets a new stop, which fires at once).

Resuming needs no state either. After a stop the price is below `stopLoss`, which is below
`lower`, so every line is above the price and no buy goes out. The grid simply waits. When the
price comes back inside the range, the lines below it get their buys again, sized from the
realised equity that is left after the loss.

### Leverage fails loudly

The SDK answers a refused leverage with `status: err` instead of raising. The connector used to
log it and answer ok, so the bot sized orders for a leverage that was never set. Now a refusal
stops that grid from starting, with an alert, and it keeps retrying.

### One grid per coin

The exchange keeps one net position per coin per account. Two grids on one coin share it and can
no longer tell what is theirs; the position check would fail for both. The bot refuses to start
with such a configuration.

### Refusing to start

The bot refuses a configuration it must not trade with, and names the problem; the checks are
listed in [operations.md](operations.md#grids). Refusing costs nothing; trading on a wrong
assumption costs money. An order under the exchange's $10 minimum only warns: the size depends on
the money on the dex, which can change.

---

## Deployment and secrets

### Secrets never pass through Helm

Helm stores the values you install with in a Secret in the cluster
(`sh.helm.release.v1.<name>.v1`), and `helm get values` prints them back. A key passed as a chart
value is stored a second time and shown to anyone allowed to run Helm.

So the chart creates no Secrets. It takes the *name* of a Secret you create yourself
(`existingSecret`), and CI fails if the chart ever renders one.

Secret values are read with hidden input, base64-encoded over a pipe and sent to
`kubectl apply -f -` on stdin, never with `--from-literal=key=value`, which puts the value in
shell history and in the process list. `install.sh` does the same.

### Testnet by default; mainnet by hand

A fresh installation trades on testnet. Moving to mainnet is a step you want to take slowly,
with your own eyes on what changes, so it is described in [operations.md](operations.md#going-to-mainnet) rather than scripted.

### One replica, Recreate strategy

Two grid-static pods managing the same grid would place duplicate orders, so there is exactly one
replica and deployments use `Recreate`, not `RollingUpdate`. The price is a few seconds without a
running bot during every upgrade. Resting orders stay on the exchange meanwhile, and the new pod
picks them up through their cloids.

### Startup waits for the connector

grid-static waits for the connector before it touches it, and every deployment has a readiness
probe, so a pod is only "ready" once its application has actually started.

---

## Known limitations

- **History is short.** Realised profit is reported for the last 24 hours from the exchange's
  fills; there is no total since the start, and the exchange keeps only the last 10,000 fills.
  The Prometheus counters add up while the pod runs.
- **An API wallet expires.** It is the safer key (it cannot withdraw), but it stops working on the
  date set when it was created. Reading still works, so the grid keeps running but every order
  is refused; the bot alerts that once an hour per grid until a new key is in the Secret.
- **A long outage needs a look.** Coin bought more than `fillLookbackHours` before the bot saw it
  cannot be tied to its cell. The grid holds, and a person decides. On a thin book a partial fill
  too small to sell can age out the same way; see incident 4.
- **One grid per coin per account.** Several grids on one coin need several accounts, and several
  installations.
- **Logs do not survive a restart.** Only the current container's log is available (plus one
  previous one). The exchange is the reliable record, which is why `tools/gridcheck.py` reads it
  directly.
- **Kubernetes Secrets are not encrypted at rest** unless your cluster enables it.
