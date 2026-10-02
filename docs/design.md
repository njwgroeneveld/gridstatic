# Design decisions

This document explains why gridstatic is built the way it is. Several of these choices were not
made up front: they were forced by incidents, and those are described here too, because the
reasoning is more useful than the rule.

- [Principles](#principles)
- [The exchange is the only source of truth](#the-exchange-is-the-only-source-of-truth)
- [The strategy](#the-strategy)
- [Deployment and secrets](#deployment-and-secrets)
- [Incidents](#incidents)
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
months while failing live ([incident 1](#1-eight-buys-on-one-line-2026-09-09)). Shadow mode is
gone; the bot is tested on Hyperliquid's testnet, against a real order book.

**Reaching real money takes deliberate steps.** Everything defaults to testnet. Mainnet needs a
mainnet key and `hyperliquid.testnet: false`, and is done by hand.

---

## The exchange is the only source of truth

### Why there is no database

The database held the bot's view of its own orders and trades. The bot decided from that view,
and updated it after the exchange had acted. Two writes that are not atomic drift apart, and
every incident in this document is such a drift:

| | what drifted |
|---|---|
| [Incident 1](#1-eight-buys-on-one-line-2026-09-09) | the database said "line free", the exchange held the position |
| [Incident 2](#2-prices-arrived-as-strings-2026-09-10) | `Decimal` → JSON on the way out of the database service |
| [Incident 3](#3-a-crash-on-every-fresh-install-2026-09-10) | waiting for the database schema at startup |

On top of that the database cost a service, a hosted project, a choice of connection pooler, a
Secret, an initContainer, a schema copy with its own CI check, and half of the installer.
Everything the bot needs is already on the exchange.

### A cell, and the client order id

Lines `p_0 < p_1 < … < p_n-1` divide the range. A **cell** `i` is a pair: buy at `p_i`, sell at
`p_i+1`. Every order belongs to exactly one cell, and its client order id (cloid, 16 bytes) says
which:

| bytes | |
|---|---|
| 0–1 | magic `0x6773` — a gridstatic order |
| 2 | version |
| 3–8 | grid fingerprint: coin, bounds, line count, leverage |
| 9–10 | cell |
| 11 | side |
| 12–15 | nonce, so no two orders share a cloid |

Hyperliquid hands the cloid back with every resting order. After a restart the bot reads the book
and knows which cell each order is on — without a record, and without comparing prices, which
the exchange rounds and fills at prices of its own ([incident 1](#1-eight-buys-on-one-line-2026-09-09)).

Fills carry only the order id, not the cloid. The bot keeps an in-memory map from order id to
cloid, filled when it places an order and whenever it reads the book; anything missing is looked
up with `orderStatus`. Losing that map costs a few lookups after a restart, never a wrong
decision: a fill that cannot be tied to a cell leaves the position unexplained, and the grid
holds until it can.

A grid's fingerprint changes with any of its five settings. Orders with another fingerprint
belong to an earlier grid: the bot does not touch them, and holds until they are gone.

### What a cell holds

The rule that carries the design: **a cell's unsold coin is what it bought after its current
sell was placed** — or, without a resting sell, after its last sell fill.

It works because the bot sizes every sell to cover everything the cell holds at that moment.
Anything bought later has no exit yet. That one rule covers a partial fill, a buy that filled
completely and left the book, a restart, and a crash between cancelling a small sell and placing
the bigger one that replaces it.

### The position has to add up

`residual = position − resting sells − unsold coin`. In a healthy grid it is zero within half a
lot. Before the bot places any new buy, it has to be:

| | grid holds — no new buys |
|---|---|
| residual > 0 | coin no cell accounts for: a manual position, another bot, fills older than the look-back |
| residual < 0 | resting sells exceed the position |
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
read open orders, position, price, fills  ── any read fails → do nothing this round
tie fills to cells through their cloid
reconcile → cancels, sells, buys, hold
cancel; then sells; then buys
report fills, closed cycles, hold changes
```

`reconcile` has no I/O and no memory. It is where every decision is made, and it is tested on
its own, case by case. Each safety rule was mutated away once to confirm a test fails without it.

A filled sell leaves its cell empty, and the next round puts the buy back. A cycle is not a
separate code path; it is the next round.

---

## The strategy

### Order size is fixed

The size per line is `startBalance × strategyAllocationPct × allocationPct ÷ lines`, and it does
not grow with profit. Growing it needs the realised profit since the start, which the exchange
cannot report once its fill history rolls over — it keeps the last 10,000 fills — and the bot
keeps no record to add it up.

It is deliberately not derived from the account value either. That value moves with unrealised
P&L, so lines would shrink exactly while the grid is buying its way down, and grow again as it
sells — the opposite of what a grid needs. At startup the bot compares what a full grid needs
with the account value and warns if it cannot be carried.

### One buy and one sell per cell

Each cell has at most one resting buy and one resting sell. A sell on line `j` always belongs to
cell `j−1`, so two sells on one line cannot occur. When a later fill grows what a cell holds, the
bot cancels the smaller sell and places one that covers it all — and only places the new one if
the cancel succeeded, so a cell never ends up with two. Should two still appear, the bot keeps
the oldest buy (its place in the queue) and the newest sell (the one sized to the whole cell).

Sells are placed **reduce-only**. Whatever the bot believes, a sell can never open a short.

### The price is the mark price

Which lines sit below the price is decided from the mark price, not the mid. On a liquid market
the two are the same to the tick. On a thin one they are not: on testnet, `xyz:XYZ100` had one
ask and four bids, a 9% spread and $21 of daily volume, and its mid sat 3.5% above the mark.
Worse, a grid's own buys become the best bid, which moves the mid up, which lets the next round
place a buy one line higher — the grid would chase its own orders toward the ask. The mark price
is anchored to the oracle and does not move with the bot.

### HIP-3 markets

Markets on builder-deployed perp dexes carry the dex in their name (`xyz:XYZ100`). Everything
the bot reads about such a coin — lot size, mark price, open orders, the position, the balance —
comes from that dex, and the SDK has to be told the dex exists before it can turn the name into
an asset id. The chart derives the dexes from the configured coins. Each dex has its own
balance, so the margin check reads the coin's own.

Not yet seen on real data: whether `userFills` returns fills from HIP-3 markets. If it does not,
the bot cannot tie a filled buy to its cell, the position stops adding up, and the grid holds —
safe, but it does not trade. The first testnet run checks this.

### No buy on the top line, none near the price

The top line has no line above it for the sell. And a buy within a tenth of a spacing below the
price would sit on top of the market.

### One grid per coin

The exchange keeps one net position per coin per account. Two grids on one coin share it and can
no longer tell what is theirs; the position check would fail for both. The bot refuses to start
with such a configuration.

### Refusing to start

The bot refuses a configuration it must not trade with, and names the problem: an order under
the exchange's $10 minimum, two grids on one coin, a missing `startBalance`, or a grid that still
says `shadow: true` — which would otherwise place real orders for someone who believes they are
simulating. Refusing costs nothing; trading on a wrong assumption costs money.

---

## Deployment and secrets

### Secrets never pass through Helm

Helm stores the values you install with in a Secret in the cluster
(`sh.helm.release.v1.<name>.v1`), and `helm get values` prints them back. A key passed as a chart
value is stored a second time and shown to anyone allowed to run Helm.

So the chart creates no Secrets. It takes the *name* of a Secret you create yourself
(`existingSecret`), and CI fails if the chart ever renders one.

Secret values are read with hidden input, base64-encoded over a pipe and sent to
`kubectl apply -f -` on stdin — never with `--from-literal=key=value`, which puts the value in
shell history and in the process list. `install.sh` does the same.

### Prices from the network the orders go to

The connector reads prices from the same Hyperliquid network it trades on. Up to 0.3 the price
feed always came from mainnet, which suited shadow mode; a testnet grid priced off mainnet would
place its orders at prices that do not exist on testnet.

### Testnet by default; mainnet by hand

A fresh installation trades on testnet. Moving to mainnet is a step you want to take slowly,
with your own eyes on what changes, so it is described in the README rather than scripted.

### One replica, Recreate strategy

Two grid-static pods managing the same grid would place duplicate orders, so there is exactly one
replica and deployments use `Recreate`, not `RollingUpdate`. The price is a few seconds without a
running bot during every upgrade. Resting orders stay on the exchange meanwhile, and the new pod
picks them up through their cloids.

### Startup waits for the connector

grid-static waits for the connector before it touches it, and every deployment has a readiness
probe, so a pod is only "ready" once its application has actually started.

---

## Incidents

These happened with versions that kept a database. Each one ends with what is different now.

### 1. Eight buys on one line (2026-09-09)

**What happened.** A live BTC grid bought the same line eight times in eight minutes, about 70
seconds apart — the health-check interval plus jitter. The position grew to 0.01522 BTC, more than
a fully filled 20-line grid could ever intend to hold. The stacked sells visible on the exchange
were the symptom; the duplicate buys were the cause.

**Cause.** The health loop refilled empty lines below the price. It decided a line was free with
two guards: no resting order on the line, and no open trade at the line's price. The first guard
lapses as soon as a buy fills. The second compared the *line* price with the *fill* price, and
those never matched live (5-significant-digit rounding, slippage, averaged partial fills). So
every filled line looked free again within a minute and was bought again.

**Why shadow never showed it.** Shadow filled at the exact line price, so the price comparison
matched there. The guard worked only in the mode where it did not matter.

A second, related flaw: the exchange client turned any API error into an empty list. Recovery read
an empty order book as "everything filled", and the fill loop advanced its watermark before the
fetch succeeded, silently dropping fills on failure.

**Fix at the time.** Occupied lines became resting orders plus open positions, matched by line
number. API read errors raise instead of returning empty lists. The grid was flattened at a cost
of $0.57.

**Now.** A cell with a filled buy is never free: either its fill is tied to it (unsold coin, the
cell is taken and gets a sell) or it is not (the position does not add up, and the grid holds).
The guard is the position on the exchange, not bookkeeping that can drift from it. This is the
first regression test of `reconcile`, and it fails when the hold rule is removed.

### 2. Prices arrived as strings (2026-09-10)

**What happened.** On the first live run of the standalone stack, the fill loop failed every 30
seconds with `'>' not supported between instances of 'float' and 'str'`.

**Cause.** Money columns had just been changed to `numeric`. psycopg2 returns those as `Decimal`,
and FastAPI serialised them as JSON strings. The change had been "verified" with FastAPI's
`jsonable_encoder`, which does return floats — but the routes were annotated with `dict`, which
sends serialisation through Pydantic v2 instead. The right conclusion was drawn from the wrong
measurement. The unit tests could not catch it: their mocks returned floats.

**Worse than the visible error.** Grid recognition compared bounds for equality. With strings,
`"83000" == 83000` is `False`: on the next restart the bot would not have recognised its own grid
and would have placed a second layer of orders.

**Lesson, still in force.** Verify an assumption on the path the code actually takes. When a
change crosses a type boundary — database, driver, serialiser, client — reproduce that boundary,
and distrust mocks that already return the corrected type.

**Now.** There is no database to cross. Grid recognition is a fingerprint in the cloid, computed
from the settings after converting every number to `float`. The lesson was applied while building
0.4: the real status output was piped through the real alerter formatter (which found a bug no
test had), the real SDK signatures were checked against the calls, and `gridcheck` was run
against the real testnet API.

### 3. A crash on every fresh install (2026-09-10)

**What happened.** On a fresh install grid-static restarted once with
`httpx.ConnectError: All connection attempts failed`, then came up fine.

**Cause.** Startup waited for the connector but not for the database service, whose
initContainer was still applying the schema. The first database call failed, uvicorn exited, and
Kubernetes restarted the pod.

**Now.** There is no database service and no schema to wait for. The wait loop and the readiness
probes stay.

---

## Known limitations

- **History is short.** Realised profit is reported for the last 24 hours from the exchange's
  fills; there is no total since the start, and the exchange keeps only the last 10,000 fills.
  The Prometheus counters add up while the pod runs.
- **API wallets are not supported yet.** The connector passes `wallet_address` as the vault
  (subaccount) to trade for, and reads that address. An API wallet needs the account it trades
  for passed differently; until then the bot detects the mismatch and holds.
- **A long outage needs a look.** Coin bought more than `fillLookbackHours` before the bot saw it
  cannot be tied to its cell. The grid holds, and a person decides.
- **One grid per coin per account.** Several grids on one coin need several accounts, and several
  installations.
- **Logs do not survive a restart.** Only the current container's log is available (plus one
  previous one). The exchange is the reliable record, which is why `tools/gridcheck.py` reads it
  directly.
- **Kubernetes Secrets are not encrypted at rest** unless your cluster enables it.
