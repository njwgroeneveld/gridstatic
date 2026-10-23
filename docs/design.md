# Design decisions

This document explains why gridstatic is built the way it is. Several of these choices were not
made up front: they were forced by incidents, described in [postmortems.md](postmortems.md),
because the reasoning is more useful than the rule.

- [Principles](#principles)
- [The exchange is the only source of truth](#the-exchange-is-the-only-source-of-truth)
- [The strategy](#the-strategy)
- [Deployment and secrets](#deployment-and-secrets)
- [Postmortems](postmortems.md)
- [Known limitations](#known-limitations)

---

## Principles

**One source of truth.** The bot keeps no record of its own. Every decision is made from what
the exchange says right now: open orders, the position, recent fills. Up to version 0.3 the bot
kept a database next to the exchange, and every incident below came from the two disagreeing.

**Fail loudly rather than assume.** A failed API call must never read as an empty result.
"The exchange is unreachable" and "there are no orders" lead to opposite actions. A round that
cannot read everything does nothing.

**When in doubt, do not buy.** A buy too few costs one missed cycle. A buy too many is how a
grid once bought the same line eight times. Any position the bot cannot account for stops new
buys until it can.

**A check that only runs in a simulation tests the simulation, not the bot.** Shadow mode used
exact prices and ideal fills, and a safeguard that depended on those properties passed there for
months while failing live ([incident 1](postmortems.md#1-eight-buys-on-one-line-2026-09-09)). Shadow mode is
gone; the bot is tested on Hyperliquid's testnet, against a real order book.

**Reaching real money takes deliberate steps.** Everything defaults to testnet. Mainnet needs a
mainnet key and `hyperliquid.testnet: false`, and is done by hand.

---

## The exchange is the only source of truth

### Why there is no database

The database held the bot's view of its own orders and trades. The bot decided from that view,
and updated it after the exchange had acted. Two writes that are not atomic drift apart, and the
database itself was one more thing that could stop the bot:

| | what went wrong |
|---|---|
| [Incident 1](postmortems.md#1-eight-buys-on-one-line-2026-09-09) | drift: the database said "line free", the exchange held the position |
| [Incident 2](postmortems.md#2-prices-arrived-as-strings-2026-09-10) | drift: `Decimal` → JSON on the way out of the database service |
| [Incident 3](postmortems.md#3-a-shared-free-database-dropped-the-bots-requests-september-2026) | the database itself: a free service, shared with other projects, dropped requests |

On top of that the database cost a service, a hosted project, a choice of connection pooler, a
Secret, an initContainer, a schema copy with its own CI check, and half of the installer.
Everything the bot needs is already on the exchange.

### A cell, and the client order id

Lines `p_0 < p_1 < … < p_n-1` divide the range. A **cell** `i` is a pair: buy at `p_i`, sell at
`p_i+1`. Every order belongs to exactly one cell, and its client order id says which. A cloid is
an optional 128-bit label the exchange stores with an order
([Hyperliquid docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint),
field `c`); gridstatic fills its 16 bytes like this:

| bytes | |
|---|---|
| 0–1 | magic `0x6773`, a gridstatic order |
| 2 | version |
| 3–8 | grid fingerprint: coin, bounds, line count, leverage |
| 9–10 | cell |
| 11 | side |
| 12–15 | nonce, so no two orders share a cloid |

Hyperliquid hands the cloid back with every resting order. After a restart the bot reads the book
and knows which cell each order is on, without a record, and without comparing prices, which
the exchange rounds and fills at prices of its own ([incident 1](postmortems.md#1-eight-buys-on-one-line-2026-09-09)).

Fills carry only the order id, not the cloid. The bot keeps an in-memory map from order id to
cloid, filled when it places an order and whenever it reads the book; anything missing is looked
up with `orderStatus`. Losing that map costs a few lookups after a restart, never a wrong
decision: a fill that cannot be tied to a cell leaves the position unexplained, and the grid
holds until it can.

A grid's fingerprint changes with any of its five settings. Orders with another fingerprint
belong to an earlier grid: the bot does not touch them, and holds until they are gone.

### What a cell holds

The rule that carries the design: **a cell's unsold coin is what it bought after its current
sell was placed** (or, without a resting sell, after its last sell fill). A stop fill ends what
every cell held, since the stop sells the whole position.

It works because the bot sizes every sell to cover everything the cell holds at that moment.
Anything bought later has no exit yet. That one rule covers a partial fill, a buy that filled
completely and left the book, a restart, and a crash between cancelling a small sell and placing
the bigger one that replaces it.

For example, cell 3 buys 0.0010 BTC at 80,000 and sells at 81,000:

| when | the exchange | the cell's unsold coin | the bot |
|---|---|---|---|
| 10:00 | the buy fills half: 0.0004 | 0.0004 (no sell yet) | places a sell of 0.0004 |
| 10:05 | the rest fills: 0.0006 | 0.0006 (bought after that sell) | cancels the 0.0004 sell, places one of 0.0010 |

A crash between that cancel and that place leaves no sell at all. After the restart the same rule
reads "bought since the last sell fill": 0.0010, and a sell of 0.0010 goes out. No special case.

### The position has to add up

`residual = position − resting sells − unsold coin`. In a healthy grid it is zero within half a
lot. A position of 0.0030 BTC, with sells of 0.0010 on cells 2 and 5 and 0.0010 unsold on cell 7,
adds up. A position of 0.0034 leaves 0.0004 that no cell accounts for (bought by hand, say) and
the grid holds. Any of these makes the grid hold, and no new buy goes out:

| | grid holds: no new buys |
|---|---|
| residual > 0 | coin no cell accounts for: a manual position, another bot, fills older than the look-back |
| residual < 0 | resting sells and unsold coin exceed the position |
| orders with another fingerprint | an earlier grid on this coin |
| short position | a grid only ever holds long |
| none of last round's orders on the book or filled | the bot reads another account than it trades on |

On hold, sells still go out for coin the bot can account for, and the grid resumes by itself once
the cause is gone. The last row matters more than it looks: with the exchange as the only truth,
a bot that reads the wrong account sees an empty book every round, finds nothing unexplained, and
would buy a full layer again every 30 seconds. An API wallet configured without the account it
trades for does exactly that.

### One round

```
read open orders and price, together      ── any read fails → do nothing this round
then position and fills, together         ── never before the book: see below
tie fills to cells through their cloid
reconcile → cancels, sells, buys, hold
cancel; then sells; then buys
report fills, closed cycles, hold changes
```

The order of the reads carries a rule. A buy that fills during the round has to be seen
somewhere: still on the book, or in the fills. Reading the book first guarantees that. Reading the
fills or the position before it can catch the buy gone from the book and not yet in the fills:
an empty cell, and a second buy on it. Position and fills may be read together: when they miss
each other the position does not add up, and that costs one round on hold, not a double buy.

`reconcile` has no I/O and no memory. It is where every decision is made, and it is tested on
its own, case by case. Each safety rule was mutated away once to confirm a test fails without it.

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

Each perp dex keeps its own balance, so each grid grows with the profit on its own dex. Grids on
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

Markets on builder-deployed perp dexes carry the dex in their name (`xyz:XYZ100`). Everything
the bot reads about such a coin (lot size, mark price, open orders, the position, the balance)
comes from that dex, and the SDK has to be told the dex exists before it can turn the name into
an asset id. The chart derives the dexes from the configured coins. Each dex has its own
balance, so the margin check reads the coin's own.

Not yet seen on real data: whether `userFills` returns fills from HIP-3 markets. If it does not,
the bot cannot tie a filled buy to its cell, the position stops adding up, and the grid holds:
safe, but it does not trade. The first testnet run checks this.

### No buy on the top line, none near the price

The top line has no line above it for the sell. And a buy within a tenth of a spacing below the
price would sit on top of the market.

### The stop-loss lives on the exchange, and the grid resumes on its own

The optional stop-loss is a reduce-only stop-market order on the exchange, not a price check in
the bot: it has to fire when the bot or the cluster is down, which on a home cluster with wifi
nodes is when it matters. `reconcile` keeps exactly one, the size of the whole position, and
replaces it when the position grows or the setting moves. It keeps it while the grid holds.

Whether a triggered order keeps its order id and cloid is not documented, so recovery does not
depend on recognising the stop's fill. It rests on a simpler rule: **a flat position means no cell
holds coin.** Stale sells are cancelled and the fill history is ignored. That covers a stop, a
close by hand, and a stop that fills in pieces (the rest gets a new stop, which fires at once).

Resuming needs no state either. After a stop the price is below `stopLoss`, which is below
`lower`, so every line is above the price and no buy goes out. When the price comes back inside,
the lines below it get their buys again.

### Leverage fails loudly

The SDK answers a refused leverage with `status: err` instead of raising. The connector used to
log it and answer ok, so the bot sized orders for a leverage that was never set. Now a refusal
stops that grid from starting, with an alert, and it keeps retrying.

### One grid per coin

The exchange keeps one net position per coin per account. Two grids on one coin share it and can
no longer tell what is theirs; the position check would fail for both. The bot refuses to start
with such a configuration.

### Refusing to start

The bot refuses a configuration it must not trade with, and names the problem: two grids on one
coin, a removed `startBalance`, grids on one dex using more than 100% of it, or a grid that still
says `shadow: true`, which would otherwise place real orders for someone who believes they are
simulating. Refusing costs nothing; trading on a wrong assumption costs money. An order under the
exchange's $10 minimum does not block starting: the size depends on the money on the dex, so the
bot warns instead (see `operations.md`).

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

### Prices from the network the orders go to

The connector reads prices from the same Hyperliquid network it trades on. Up to 0.3 the price
feed always came from mainnet, which suited shadow mode; a testnet grid priced off mainnet would
place its orders at prices that do not exist on testnet.

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
  date set when it was created, and the grid then holds until a new one is in the Secret.
- **A long outage needs a look.** Coin bought more than `fillLookbackHours` before the bot saw it
  cannot be tied to its cell. The grid holds, and a person decides. On a thin book a partial fill
  too small to sell can age out the same way; see incident 4.
- **One grid per coin per account.** Several grids on one coin need several accounts, and several
  installations.
- **Logs do not survive a restart.** Only the current container's log is available (plus one
  previous one). The exchange is the reliable record, which is why `tools/gridcheck.py` reads it
  directly.
- **Kubernetes Secrets are not encrypted at rest** unless your cluster enables it.
