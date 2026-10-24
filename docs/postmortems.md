# Postmortems

What went wrong, why, and what is different now. The first three happened with versions that kept
a database; the last two on the exchange-as-truth design, found once its metrics were collected.
The design they led to is in [design.md](design.md).

### 1. Eight buys on one line (2026-09-09)

**What happened.** A live BTC grid bought the same line eight times in eight minutes, about 70
seconds apart: the health-check interval plus jitter. The position grew to 0.01522 BTC, more than
a fully filled 20-line grid could ever intend to hold. The stacked sells visible on the exchange
were the symptom; the duplicate buys were the cause.

**Cause.** The health loop refilled empty lines below the price. It decided a line was free with
two guards: no resting order on the line, and no open trade at the line's price. The first guard
lapses as soon as a buy fills. The second compared the *line* price with the *fill* price, and
those never matched live (5-significant-digit rounding, slippage, averaged partial fills). So
every filled line looked free again within a minute and was bought again.

**Why testing never showed it.** The bot had been tested in a simulation that ran without any
exchange: it followed the mainnet price and counted an order as filled when the price crossed its
line, at exactly the line price. So the price comparison matched there. The guard worked only in
the mode where it did not matter.

**A second flaw, found on the way.** When a request to the exchange failed, for example on a
timeout, the exchange client did not report an error: it returned an empty list. That did harm in
two places:

- After a restart the bot read the open orders to see where it stood. A failed read gave "no open
  orders", which it took to mean that every order had filled.
- The bot kept a watermark: the time up to which it had processed fills. It moved the watermark
  forward *before* fetching the fills. When the fetch failed, those fills were skipped for good.

**Fix at the time.** A line counted as taken when it had a resting order or an open trade *with
the same line number*, no longer one at the same price. A failed read now raised an error instead
of returning an empty list. The grid was closed (every position sold), at a cost of $0.57.

**Now.** Every decision is made in one function, `reconcile` (see
[design.md](design.md#one-round)), and this incident is its first regression test. The rule it
tests: a cell with a filled buy is never free. Either the fill is tied to the cell, so the cell is
taken and gets a sell, or it is not, and then the position does not add up and the grid holds.
The guard is the position on the exchange, not bookkeeping that can drift from it. The test fails
when the hold rule is removed.

### 2. Prices arrived as strings (2026-09-10)

**What happened.** On the first live run of the standalone stack, the bot failed every 30 seconds
with `'>' not supported between instances of 'float' and 'str'`: prices from the database service
arrived as text.

**Cause.** A small change with a check that looked right. Money columns had become `numeric`, which
the database driver returns as `Decimal`. The change was checked with one FastAPI function, which
turns those into numbers, but the routes took another path, through Pydantic v2, which turns them
into strings. The unit tests could not catch it: their mocks returned numbers.

**Why it is here.** The visible error was the harmless part. The bot recognised its own grid by
comparing the grid's bounds, and `"83000" == 83000` is `False`: on the next restart it would have
failed to recognise its own orders and placed a second layer of them.

**Lesson, still in force.** Check an assumption on the path the code actually takes, and distrust
mocks that already return the type you hope for. That is why the version without a database was
tested against the real thing: the real status output through the real alerter, the real SDK
signatures, `gridcheck` against the real testnet API.

### 3. A shared free database dropped the bot's requests (September 2026)

**What happened.** The bot's state lived in a free cloud Postgres, shared with several other
projects. In September the provider's dashboard reported that the project was *dropping
requests*. The bot hung on its database calls, and a fresh install crashed at startup with
`httpx.ConnectError: All connection attempts failed` on the same unreachable service.

**Cause.** The projects on that one free account together used more egress (data sent out of the
database) than the free tier allows, and the provider began dropping requests. The bot could not
reach its own state, and had no say in what the other projects used.

**Now.** There is no database. The bot reads its state from the exchange, which it needs anyway;
its egress went to zero once the version without a database was deployed on October 2. The
startup wait for the connector and the readiness probes stay.

### 4. Two grids on hold for days, and nobody saw it (2026-10-17)

**What happened.** Both testnet grids turned out to have placed no new buys for days. The bot did
what it is built to do (hold, and say so in its log and in an hourly notice), but nothing made it
visible: Prometheus had never collected a single metric of it. The cluster runs Prometheus through
the Prometheus Operator, which finds what to scrape only through *ServiceMonitors*: small
Kubernetes objects that say "scrape this Service, on this port and path". The bot's Service carried
`prometheus.io/scrape` annotations instead, a convention of plain Prometheus setups that the
Operator ignores. The process's own counters, read straight from the pod, showed both grids on
hold and 14,398 failed order placements over its 15 days. When and why those failed is lost: the
counters died with the next restart.

**Why the BTC grid held: a bug.** On 2026-10-15 the stop-loss fired and sold the whole position
(0.00473 BTC at 79,969, −$14.50). The price came back, and cell 0 bought again. But the bot still
counted cell 0's buy from *before* the stop as held: it only reset a cell after a sell fill, and the
stop fill was not one. So it believed cell 0 held two buys, 0.00196 BTC, while the position was one
buy, 0.00098. The numbers did not add up, and the grid held. The hold message named only the
resting sells, so it read as nonsense: "resting sells of 0 exceed the position".

**Why the NASDAQ grid held: correctly.** 0.0004 of the position belonged to no cell: 0.0003 from
an order placed by an older version, without a cloid, and 0.0001 from a partial buy on the thin
testnet book that was too small to sell and aged out of the 72-hour look-back. The bot cannot know
whose that is, so holding was right. It was closed by hand.

Both holds did what holds are for. The incident is that nobody saw them.

**Now.** A stop fill ends what every cell held, and the hold message names what the cells hold; a
regression test replays the BTC case. [`monitoring/`](../monitoring/README.md) registers the scrape
targets and adds two alerts evaluated outside the bot: `GridstaticHold` after 30 minutes on hold,
and `GridstaticDown`, which uses `absent(up == 1)` because a pod that is gone leaves no `up` series
to be 0. Both link to a runbook; `GridstaticDown` was tested end to end by scaling the bot to zero.
The lesson: a safe default (hold) is only safe while someone sees it.

### 5. Rounds took seconds, and the obvious suspect was half of it (2026-10-17)

**What happened.** A round (four reads, one decision) took 3.4 seconds on average, up to 9 at
p95. Harmless at a 30-second interval, but the grid acted on a market up to seconds old.

**Measured, not guessed.** The connector now times every request where it goes over the network,
per request type. The suspected one, `userFills`, sent the account's whole history (444 fills,
145 KB) to keep the 16 of the last 72 hours. But `clearinghouseState` cost as much, by count
alone: 11 requests a minute, because every grid read the positions of every dex each round and
used one coin of the answer. That also coupled the grids: one dex that could not be read stopped
the grids on all the others.

**Now.** Each grid reads the position of its own dex. The parameter is optional: without it the
connector reads every dex as before, so either service could be rolled out first. Fills come from
`userFillsByTime`, paged so the newest are never the ones cut off; checked against testnet, it
returned the same 16 fills in 5.9 KB and 0.62 s instead of 1.47. A round reads in two parallel
phases. Rounds take 1.6 seconds on average.

**Why not all four reads at once.** That would be fastest, but the order matters. Say the fills
are read first and a buy fills a moment later, before the book is read. The fills do not have it
yet, and the book no longer has it. The bot sees an empty cell and buys it again: incident 1 once
more. Read the book first, and a buy that fills in between is in one of the two: still on the book,
or in the fills read after it. So the book and the price go first, and the position and the fills
follow together. If those two miss each other, the position does not add up and the grid holds for
one round, which is harmless. A test fixes this order, and fails when all four are read at once.
