# Postmortems

What went wrong, why, and what is different now. The first three happened with versions that kept
a database; the last two on the exchange-as-truth design, found once its metrics were collected.
The design they led to is in [design.md](design.md).

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

### 4. Two grids on hold for days, and nobody saw it (2026-10-17)

**What happened.** Both testnet grids turned out to have placed no new buys for days. The bot did
what it is built to do — hold, and say so in its log and in an hourly notice — but nothing made it
visible: Prometheus had never collected a single metric of it. The Service carried
`prometheus.io/scrape` annotations, which the Prometheus Operator ignores; it only follows
ServiceMonitors. The process's own counters, read straight from the pod, showed both grids on
hold and 14,398 failed order placements over its 15 days. When and why those failed is lost: the
counters died with the next restart.

**Cause of the BTC hold — a bug.** On 2026-10-15 the stop closed the whole position (0.00473 BTC at
79,969, −$14.50). Flat, every cell was free, and cell 0 bought again. But only a *sell* fill ended
what a cell held; a *stop* fill did not. The buy from before the stop came back as unsold coin next
to the new one: 0.00196 counted against a position of 0.00098, and the grid held. The hold message
named only the resting sells, so it read as nonsense: "resting sells of 0 exceed the position".

**Cause of the NASDAQ hold — correct, by design.** 0.0004 of the position belonged to no cell:
0.0003 from an untagged order of an older version, and 0.0001 from a partial buy on the thin
testnet book that sat unsold until it fell out of the 72-hour look-back. Holding was right. It was
closed by hand with a reduce-only limit sell; a market sell was refused, as the only bid sat 18%
below the mark and made it a $9.64 order, under the $10 minimum.

**Now.** A stop fill ends what every cell held, and the hold message names the unsold coin; a
regression test replays the BTC case. [`monitoring/`](../monitoring/README.md) registers the scrape
targets and adds two alerts evaluated outside the bot: `GridstaticHold` after 30 minutes on hold,
and `GridstaticDown`, which uses `absent(up == 1)` because a pod that is gone leaves no `up` series
to be 0. Both link to a runbook; `GridstaticDown` was tested end to end by scaling the bot to zero.
The lesson: a safe default — hold — is only safe while someone sees it.

### 5. Rounds took seconds, and the obvious suspect was half of it (2026-10-17)

**What happened.** A round — four reads, one decision — took 3.4 seconds on average, up to 9 at
p95. Harmless at a 30-second interval, but the grid acted on a market up to seconds old.

**Measured, not guessed.** The connector now times every request where it goes over the network,
per request type. The suspected one, `userFills`, sent the account's whole history — 444 fills,
145 KB — to keep the 16 of the last 72 hours. But `clearinghouseState` cost as much, by count
alone: 11 requests a minute, because every grid read the positions of every dex each round and
used one coin of the answer. That also coupled the grids: one dex that could not be read stopped
the grids on all the others.

**Now.** Each grid reads the position of its own dex. The parameter is optional — without it the
connector reads every dex as before — so either service could be rolled out first. Fills come from
`userFillsByTime`, paged so the newest are never the ones cut off; checked against testnet, it
returned the same 16 fills in 5.9 KB and 0.62 s instead of 1.47. A round reads in two parallel
phases. Rounds take 1.6 seconds on average.

**Not all four at once.** Read together, the fills can be answered before the book: a buy that
fills in between is gone from the book and not yet in the fills — an empty cell, and a second buy
on it, incident 1 again. The book is read first; position and fills follow together, and when
those two miss each other the position does not add up and the grid holds one round. A test fixes
the order; run against all four reads at once, it fails.
