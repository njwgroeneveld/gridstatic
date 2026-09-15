# Design decisions

This document explains why gridstatic is built the way it is. Several of these choices were not
made up front: they were forced by incidents, and those are described here too, because the
reasoning is more useful than the rule.

- [Principles](#principles)
- [The strategy](#the-strategy)
- [Data and the database](#data-and-the-database)
- [Deployment and secrets](#deployment-and-secrets)
- [Incidents](#incidents)
- [Known limitations](#known-limitations)

---

## Principles

**A check that only runs in simulation tests the simulation, not the bot.** Shadow mode uses
exact prices and ideal fills. Any safeguard that depends on those properties will pass in shadow
and fail live. Tests must model live behaviour — real fill prices, partial fills, API errors —
or they only confirm what shadow already does.

**Fail loudly rather than assume.** A failed API call must never read as an empty result.
"The exchange is unreachable" and "there are no orders" lead to opposite actions.

**One place per responsibility.** Only the dal touches the database; only the connector touches
the exchange; only grid-static decides what to trade. That makes each failure point obvious and
each service testable on its own.

**Reaching real money takes deliberate steps.** Everything defaults to shadow. Going live needs
both an exchange key and `shadow: false` on a specific grid, and is done by hand.

---

## The strategy

### A grid line is identified by its number, not its price

Every order and every open position is tied to a line number (`level`). Prices are kept for
display and P&L, but never compared as keys.

The price that comes back from the exchange is not the price the bot asked for. The connector
rounds every limit price to five significant digits, the exchange fills at a price of its own,
and a partial fill averages the entry across pieces. A grid line at 79,210.53 comes back as a
trade at 79,211. Matching on price therefore never matches. See
[incident 1](#1-eight-buys-on-one-line-2026-09-09).

A line counts as **occupied** in two cases: it has a resting order, **or** it holds an open
position waiting for its sell. The second case is easy to miss — a filled buy leaves no resting
order behind — and missing it is what caused the incident.

### Order size comes from fixed capital, not account value

The size per line is `startBalance × allocation ÷ lines`, plus realised profit. It is
deliberately not derived from the exchange account value. That value moves with unrealised
P&L, so lines would shrink exactly while the grid is buying its way down, and grow again as it
sells — the opposite of what a grid needs. Using the same rule live and in shadow also keeps
the two comparable.

The cost is that nothing checks `startBalance` against the money actually available. Set it too
high and the exchange rejects orders for insufficient margin. See
[known limitations](#known-limitations).

### Two sells may stack on one line; two buys may not

Two different buy orders can legitimately both exit one line higher, so duplicate sells on a
line are allowed. Skipping the second sell once left a position with no exit. Duplicate *buys*
on a line are never legitimate, and a partial unique index enforces that
([below](#the-unique-index-is-detection-not-protection)).

### A grid is recognised by its settings on restart

On startup the bot looks for an active grid with the same coin, number of lines, bounds and
leverage. If it finds one, it recovers that grid's state; if not, it builds a new one.

Consequences worth knowing:

- Changing any of those five settings creates a **new** grid, while the old one's orders stay on
  the exchange unmanaged.
- Changing `shadow` does not create a new grid, so a shadow grid switched to live continues in
  the same record.
- Two stacks sharing one database would recognise each other's grids and manage them twice.

### Startup waits for its dependencies

grid-static waits for both the connector and the dal before it touches either, and every
deployment has a readiness probe. On a fresh install the dal is ready later than grid-static,
because its initContainer applies the schema first. See
[incident 3](#3-a-crash-on-every-fresh-install-2026-09-10).

---

## Data and the database

### Money is stored as numeric; the dal returns floats

Prices, sizes and profit are `numeric`, because floating-point rounding shows up in sums.
`leverage` stays `double precision`: it is a multiplier, not money.

psycopg2 returns `numeric` as Python `Decimal`, and FastAPI serialises a `Decimal` on a route
annotated with `dict` as a JSON **string**. The dal therefore converts every `Decimal` to `float`
before a row leaves the service, in one place (`Database._execute`). See
[incident 2](#2-prices-arrived-as-strings-2026-09-10).

### The unique index is detection, not protection

```sql
CREATE UNIQUE INDEX grid_orders_one_open_buy_per_level
    ON gridtrading.grid_orders (grid_config_id, level)
    WHERE status = 'OPEN' AND side = 'BUY';
```

It makes two resting buys on one line impossible to record. It would **not** have prevented
incident 1: those buys had already filled and were no longer `OPEN`. And when it fires, it leaves
an orphan: the bot places the order on the exchange first and writes to the database second, so
a rejected row means an order that nothing tracks. Treat it as an alarm.

### The schema is applied by an initContainer, not a Helm hook

A `pre-install` hook runs before the chart's regular resources — including the Secret that holds
the connection string — and Helm waits for the hook to finish. That deadlocks. The dal is the
only service that touches the database, so it applies the schema itself in an initContainer that
waits for the database and runs an idempotent script on every start.

### Use your own database, and the Supabase session pooler

A second stack on the same database finds the first stack's active grids and treats them as its
own. A Kubernetes namespace does not isolate database rows; only the connection string does.

Supabase's direct connection (`db.<ref>.supabase.co`) has had no IPv4 address since 2024, so it
fails from most clusters. The session pooler on port 5432 is reachable over IPv4 and suits a
long-running service. (psycopg2 does not use server-side prepared statements, so the transaction
pooler would also work; the session pooler is simply the better fit.)

---

## Deployment and secrets

### Secrets never pass through Helm

Helm stores the values you install with in a Secret in the cluster
(`sh.helm.release.v1.<name>.v1`), and `helm get values` prints them back. A password passed as a
chart value is stored a second time and shown to anyone allowed to run Helm.

So the chart creates no Secrets. It takes the *name* of a Secret you create yourself
(`existingSecret`), and CI fails if the chart ever renders one.

Secret values are read with hidden input, base64-encoded over a pipe and sent to
`kubectl apply -f -` on stdin — never with `--from-literal=key=value`, which puts the value in
shell history and in the process list. Encoding also keeps a password containing quotes or
backslashes from breaking the YAML. `install.sh` does the same.

### No key is needed for shadow mode

The connector builds its exchange client the first time a route needs the account — orders,
positions, fills — not at startup. Without a key it starts normally, serves prices from
Hyperliquid's public API, and answers 503 on the account routes. Shadow mode only needs prices.
That makes a complete shadow installation possible without a single exchange credential — the
right way to let someone try the project.

### Going live is manual

There was briefly a script for it. It made going live faster, and speed is not a virtue there: it
is a step you want to take slowly, with your own eyes on what changes. The installer covers the
harmless half (shadow); the README covers the other half by hand.

### One replica, Recreate strategy

Two grid-static pods managing the same grid would place duplicate orders, so there is exactly one
replica and deployments use `Recreate`, not `RollingUpdate`. The price is a few seconds without a
running bot during every upgrade. Resting orders stay on the exchange meanwhile.

---

## Incidents

### 1. Eight buys on one line (2026-09-09)

**What happened.** A live BTC grid bought the same line eight times in eight minutes, about 70
seconds apart — the health-check interval plus jitter. The position grew to 0.01522 BTC, more than
a fully filled 20-line grid could ever intend to hold. The stacked sells visible on the exchange
were the symptom; the duplicate buys were the cause.

**Cause.** The health loop refills empty lines below the price. It decided a line was free with
two guards: no resting order on the line, and no open trade at the line's price. The first guard
lapses as soon as a buy fills. The second compared the *line* price with the *fill* price, and
those never matched live (5-significant-digit rounding, slippage, averaged partial fills). So
every filled line looked free again within a minute and was bought again.

**Why shadow never showed it.** Shadow fills at the exact line price, so the price comparison
matched there. The guard worked only in the mode where it did not matter.

A second, related flaw: the exchange client turned any API error into an empty list. Recovery read
an empty order book as "everything filled", and the fill loop advanced its watermark before the
fetch succeeded, silently dropping fills on failure.

**Fix.** Occupied lines are now resting orders **plus** open positions, matched by line number
(the dal joins each trade to its buy order's level). API read errors raise instead of returning
empty lists; recovery skips reconciliation when the exchange is unreadable; the watermark moves
only after a successful fetch. Every new test was verified to fail on the old code.

**Cleanup.** The grid was flattened — orders cancelled, position closed, records closed — at a
cost of $0.57 in fees and spread.

### 2. Prices arrived as strings (2026-09-10)

**What happened.** On the first live run of this stack, the fill loop failed every 30 seconds with
`'>' not supported between instances of 'float' and 'str'`.

**Cause.** Money columns had just been changed to `numeric`. psycopg2 returns those as `Decimal`,
and FastAPI serialised them as JSON strings. The change had been "verified" with FastAPI's
`jsonable_encoder`, which does return floats — but the dal's routes are annotated with `dict`,
which sends serialisation through Pydantic v2 instead. The right conclusion was drawn from the
wrong measurement. The unit tests could not catch it: their mocks returned floats.

**Worse than the visible error.** The grid-recognition check compares `upper`, `lower` and
`leverage` for equality. With strings, `"83000" == 83000` is `False`: on the next restart the bot
would not have recognised its own grid and would have placed a second layer of orders.

**Fix.** The dal converts `Decimal` to `float` in `Database._execute`, with a test that fails
without the conversion.

**Lesson.** Verify an assumption on the path the code actually takes. When a change crosses a type
boundary — database, driver, serialiser, client — reproduce that boundary, and distrust mocks that
already return the corrected type.

### 3. A crash on every fresh install (2026-09-10)

**What happened.** On a fresh install grid-static restarted once with
`httpx.ConnectError: All connection attempts failed`, then came up fine.

**Cause.** Startup waited for the connector but not for the dal, whose initContainer was still
applying the schema. The first database call failed, uvicorn exited, and Kubernetes restarted the
pod. Self-healing — but it looks like a broken installation, and after a node outage every pod
starts at once, which is exactly when the race is most likely.

**Fix.** A shared wait loop for both dependencies, and readiness probes on all four deployments,
so a pod is only "ready" once its application has actually started.

---

## Known limitations

- **Orders are written to the exchange before the database.** A database failure between the two
  leaves an order that nothing tracks. The fix is a reserved row before the exchange call, or a
  reconciliation pass that cancels unknown orders.
- **No check of `startBalance` against the real balance.** A live grid sized beyond the available
  margin only finds out when the exchange rejects orders. A startup check that compares the two
  and warns is planned.
- **Logs do not survive a restart.** Only the current container's log is available (plus one
  previous one). Without Telegram, errors from before a restart are lost; the database and the
  exchange remain the reliable record, which is why `tools/gridcheck.py` relies on them.
- **Leftovers from a larger system.** The connector still exposes take-profit, stop-loss and
  close-position routes that grid-static never calls, and the alerter's `/status` matches orders
  by price — a choice made for a trailing grid that does not exist in this repository.
- **Kubernetes Secrets are not encrypted at rest** unless your cluster enables it.
