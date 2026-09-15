# gridstatic

A static grid trading bot for [Hyperliquid](https://hyperliquid.xyz), built as four small
services on Kubernetes and installed with a single command.

[![grid-static](https://github.com/njwgroeneveld/gridstatic/actions/workflows/deploy-grid-static.yml/badge.svg)](https://github.com/njwgroeneveld/gridstatic/actions/workflows/deploy-grid-static.yml)
[![dal](https://github.com/njwgroeneveld/gridstatic/actions/workflows/deploy-dal.yml/badge.svg)](https://github.com/njwgroeneveld/gridstatic/actions/workflows/deploy-dal.yml)
[![connector](https://github.com/njwgroeneveld/gridstatic/actions/workflows/deploy-connector.yml/badge.svg)](https://github.com/njwgroeneveld/gridstatic/actions/workflows/deploy-connector.yml)
[![telegram-alerter](https://github.com/njwgroeneveld/gridstatic/actions/workflows/deploy-telegram-alerter.yml/badge.svg)](https://github.com/njwgroeneveld/gridstatic/actions/workflows/deploy-telegram-alerter.yml)
[![chart](https://github.com/njwgroeneveld/gridstatic/actions/workflows/publish-chart.yml/badge.svg)](https://github.com/njwgroeneveld/gridstatic/actions/workflows/publish-chart.yml)

> **Disclaimer.** This is a personal project, not financial advice. Trading with leverage can
> lose more than you put in. Everything runs in shadow mode by default; start there, then use
> Hyperliquid's testnet before you ever point it at real funds.

---

## Contents

- [How it works](#how-it-works)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Manual installation](#manual-installation)
- [Configuration](#configuration)
- [Shadow mode: what it simulates](#shadow-mode-what-it-simulates)
- [Going live](#going-live)
- [Checking a running grid](#checking-a-running-grid)
- [Telegram alerts (optional)](#telegram-alerts-optional)
- [Security](#security)
- [Uninstalling](#uninstalling)
- [Development](#development)
- [Design decisions](#design-decisions)

---

## How it works

You choose a price range and a number of lines. The bot divides the range into evenly spaced
grid lines and places a **buy** limit order on every line below the current price.

```
 83,000 ─────────────────────────  line 19
    ...
 77,526 ─────────────────────────  line 6    ← sell, one line above a filled buy
 77,105 ─────────────────────────  line 5    ← filled buy
 77,075 ════════ price ══════════
 76,684 ─────────────────────────  line 4    ← resting buy
    ...
 75,000 ─────────────────────────  line 0    ← resting buy
```

- When a buy fills, a **sell** goes up one line higher.
- When that sell fills, the round trip is closed and the buy returns on its line.
- Every closed round trip earns the distance between two lines, minus fees.

A grid does well when the price moves back and forth inside the range. It does badly when the
price leaves the range and stays out: below the range you hold every position bought on the
way down; above it, nothing is left to sell.

**Everything runs in shadow mode by default** — a simulation with its own bookkeeping that
never sends an order to the exchange. Going live takes two separate, deliberate steps.

---

## Architecture

```mermaid
flowchart LR
    subgraph cluster[Kubernetes namespace]
        GS[grid-static<br/><i>the strategy</i>]
        DAL[dal<br/><i>database access</i>]
        CON[connector<br/><i>exchange access</i>]
        TG[telegram-alerter<br/><i>optional</i>]
    end
    DB[(PostgreSQL<br/>e.g. Supabase)]
    HL[Hyperliquid API]
    TGAPI[Telegram]

    GS -- orders, trades --> DAL
    GS -- prices, orders, fills --> CON
    GS -- alerts --> TG
    DAL --> DB
    CON --> HL
    TG -- /status --> DAL
    TG --> TGAPI
```

| service | responsibility |
|---|---|
| **grid-static** | The strategy. Builds the grid, detects fills, places the follow-up orders, and runs a health loop that refills empty lines below the price. |
| **dal** | The only service that talks to the database. Also applies the schema on startup. |
| **connector** | The only service that talks to Hyperliquid. Holds the private key; without one it still serves prices. |
| **telegram-alerter** | Optional. Sends alerts and answers a `/status` command. |

Every service is a small FastAPI app with its own image (amd64 and arm64) and its own test
suite. They talk to each other over ClusterIP; nothing is exposed outside the cluster.

---

## Requirements

| | why |
|---|---|
| A Kubernetes cluster | k3s, minikube, kubeadm — any of them |
| `kubectl` with access to it | the installation runs from your machine |
| `helm` 3 | `brew install helm`, `winget install Helm.Helm`, or the [install script](https://helm.sh/docs/intro/install/) |
| A PostgreSQL database | the easiest is a free [Supabase](https://supabase.com) project |
| Outbound internet from the cluster | to Hyperliquid, to your database, and to ghcr.io for the images |

You do **not** need a StorageClass (the stack has no volumes), an ingress, cert-manager or a
service mesh.

### The database connection

In your Supabase dashboard, open **Connect** and copy the **session pooler** string:

```
postgresql://postgres.PROJECT_REF:PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres?sslmode=require
```

| connection | port | |
|---|---|---|
| **Session pooler** | 5432 | what you want: reachable over IPv4, meant for long-lived connections |
| Transaction pooler | 6543 | works too, but is meant for short-lived connections |
| Direct (`db.<ref>.supabase.co`) | 5432 | **IPv6 only** — does not work from an IPv4 cluster |

> **Never point two gridstatic stacks at the same database.** The bot recognises its grids by
> their settings. A second stack would find the first one's grids, treat them as its own and
> place duplicate orders. A Kubernetes namespace does not prevent this: it separates pods, not
> database rows.

You do not need to create any tables. The dal applies an idempotent schema (`db/schema.sql`)
every time it starts.

---

## Quick start

```bash
curl -fsSLO https://raw.githubusercontent.com/njwgroeneveld/gridstatic/master/install.sh
bash install.sh
```

The script asks one question — your connection string, with hidden input — and then:

1. checks that `kubectl`, `helm` and your cluster are available
2. creates the `gridstatic` namespace
3. stores the connection string in a Kubernetes Secret
4. writes `gridstatic-values.yaml` with a default BTC grid in shadow mode
5. installs the Helm chart
6. **verifies the result**: the schema was applied, all pods run, the bot built its grid, and
   the fill loop runs without errors

You end with a single verdict: done, or what went wrong and where to look.

| option | |
|---|---|
| `--dry-run` | show every step without touching anything |
| `--namespace`, `--release`, `--chart` | override the defaults |
| `GRIDSTATIC_DB_URL` | skip the question (useful in automation) |

The connection string deliberately has no command-line flag: it would end up in your shell
history. Re-running the script is safe; it reuses the Secret and upgrades the release.

---

## Manual installation

The same steps by hand, if you prefer to see each one.

**1. Namespace**

```bash
kubectl create namespace gridstatic
```

**2. The database secret**

This chart contains no passwords and creates no Secrets — you create them and pass only their
names (see [Security](#security) for why).

```bash
read -rsp 'Connection string: ' DB_URL; echo
kubectl -n gridstatic apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: gridstatic-db
type: Opaque
data:
  url: $(printf '%s' "$DB_URL" | base64 | tr -d '
')
EOF
unset DB_URL
```

This looks more roundabout than `kubectl create secret --from-literal=url=...`, on purpose. With
`--from-literal` the value lands in your shell history and in the process list. Here it is read
with hidden input, handed to `base64` over a pipe, and sent to `kubectl` over stdin. Encoding it
also keeps a password with quotes or backslashes from breaking the YAML.

**3. Values** — create `gridstatic-values.yaml`:

```yaml
database:
  existingSecret: gridstatic-db

grid:
  startBalance: 1000
  coins:
    BTC-20:
      coin: BTC
      active: true
      shadow: true
      allocationPct: 100
      upper: 83000
      lower: 75000
      numLines: 20
      leverage: 1
```

**4. Install**

```bash
helm install gridstatic oci://ghcr.io/njwgroeneveld/charts/gridstatic \
  -n gridstatic -f gridstatic-values.yaml
```

**5. Check**

```bash
kubectl -n gridstatic logs deploy/gridstatic-dal -c schema
kubectl -n gridstatic logs deploy/gridstatic-grid-static -f
```

The first command should show `CREATE TABLE` lines. If it keeps printing
`waiting for the database...`, the cluster cannot reach your database — almost always the
direct connection instead of the session pooler.

The bot should log `Initialising grid` followed by `Grid ready — N BUY orders placed`. If it
logs `Recovering state from DAL + exchange` on a database you just created, your connection
string points at the wrong database.

---

## Configuration

All settings live in your values file. The full list with comments is in
[`chart/values.yaml`](chart/values.yaml).

### Grids

```yaml
grid:
  strategyAllocationPct: 80       # share of startBalance available to all grids
  startBalance: 1000              # basis for order sizing -- see below
  fillsPollIntervalSeconds: 30    # how often fills are checked
  healthCheckIntervalSeconds: 60  # how often empty lines are refilled
  coins:
    SOL-20:                       # free-form label, used in log lines only
      coin: SOL                   # Hyperliquid symbol
      active: true
      shadow: true                # false = real orders
      allocationPct: 100          # this grid's share of the strategy allocation
      lower: 93
      upper: 107
      numLines: 20
      leverage: 1
```

**Order size per line** is:

```
startBalance × strategyAllocationPct% × allocationPct% ÷ numLines
```

With the defaults above, 1000 × 80% × 100% ÷ 20 = **$40 per line**, and up to $800 in the
market if every line fills. Realised profit is added to `startBalance` as the grid trades.

`startBalance` is the basis for sizing **in live mode too** — it is not a simulation budget. The
bot deliberately does not size from your account value: that value moves with unrealised
losses, so lines would shrink exactly while the grid is buying its way down. Set it to the amount
you are willing to trust this stack with.

Hyperliquid rejects orders below **$10**, so keep the size per line comfortably above that.

### Other settings

| key | default | |
|---|---|---|
| `database.existingSecret` | *(required)* | Secret with the key `url` |
| `hyperliquid.testnet` | `true` | `false` means mainnet |
| `hyperliquid.existingSecret` | empty | Secret with `private_key` and `wallet_address`; only needed to go live |
| `telegram.enabled` | `false` | deploys the alerter |
| `telegram.existingSecret` | empty | Secret with `bot_token` and `chat_id` |
| `scheduling.nodeSelector`, `scheduling.tolerations` | empty | pin the pods if you need to |
| `resources` | 64Mi / 50m requested | applied to every service |
| `images.*.tag` | `latest` | pin a commit SHA for reproducible deploys |

### Changing a running grid

Edit your values file and upgrade:

```bash
helm upgrade gridstatic oci://ghcr.io/njwgroeneveld/charts/gridstatic \
  -n gridstatic -f gridstatic-values.yaml
```

> **Changing `coin`, `numLines`, `lower`, `upper` or `leverage` creates a new grid.** The bot
> recognises its grid by exactly those five settings. Change one and it no longer finds the old
> grid: it creates a new one and places a second layer of orders, while the old orders stay on
> the exchange unmanaged. Cancel the old grid's orders first. Changing `shadow`,
> `allocationPct` or `startBalance` is safe.

---

## Shadow mode: what it simulates

Shadow mode uses **real prices** — the connector reads them from Hyperliquid's public API, no
key needed — but **simulated fills**. When the price crosses one of your lines, the order counts
as filled at exactly that line's price.

That makes shadow results optimistic in three ways:

- **Fills are ideal.** Always the exact line price, instantly, in full. A real limit order waits
  in a queue, so a price that touches your line and bounces may not fill it at all.
- **Fees are modelled** at Hyperliquid's maker rate (0.015%), not read from real fills.
- **Funding is ignored.** A shadow grid holds no position, so it pays no funding. With leverage
  and a persistently positive funding rate, that overstates the result.

Shadow tells you whether your lines are in the right place. It does not tell you your return.

---

## Going live

Manual on purpose: it takes two steps, and neither does anything on its own.

### 1. Create a Hyperliquid secret

Use a dedicated Hyperliquid API wallet with a limited balance, never your main account key.

```bash
read -rsp 'Private key: ' HL_KEY; echo
kubectl -n gridstatic apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: gridstatic-hl
type: Opaque
data:
  private_key: $(printf '%s' "$HL_KEY" | base64 | tr -d '
')
stringData:
  wallet_address: "0xYOUR_WALLET_ADDRESS"
EOF
unset HL_KEY
```

The wallet address is public, so it can stay in plain text; the key goes through the same
hidden-input-and-pipe route as the database connection.

### 2. Switch a grid to live

In `gridstatic-values.yaml`:

```yaml
hyperliquid:
  testnet: true                  # false = mainnet, real money
  existingSecret: gridstatic-hl

grid:
  coins:
    SOL-20:
      shadow: false              # this grid now places real orders
```

Then upgrade, and check that the log says `shadow=False`:

```bash
helm upgrade gridstatic oci://ghcr.io/njwgroeneveld/charts/gridstatic \
  -n gridstatic -f gridstatic-values.yaml
kubectl -n gridstatic logs deploy/gridstatic-grid-static --tail=40
```

Without the secret, the connector starts but answers 503 on every route that needs the account
(orders, positions, fills). Without `shadow: false`, nothing trades even when the key is there.

**Two things to know before you switch:**

- **A grid that ran in shadow keeps its record.** The bot finds its grid by coin, lines, bounds
  and leverage — not by the shadow flag. Switching an existing shadow grid to live continues in
  the same record, mixing simulated and real trades. Give the live grid different bounds or a
  different number of lines to keep them apart.
- **Leave room for the minimum order size and your margin.** Other positions on the same account
  share its margin; an order that does not fit is rejected and its line stays empty.

To go back to shadow, set `shadow: true` and upgrade again. Resting orders on the exchange are
not cancelled automatically when you do.

---

## Checking a running grid

`tools/gridcheck.py` compares the bot's bookkeeping with the exchange and with the rules the
design depends on. It only reads; it changes nothing.

```bash
python3 tools/gridcheck.py SOL
```

| check | what it catches |
|---|---|
| exactly one live config per coin | a restart that failed to recognise its grid |
| at most one resting buy and one open position per line, never both | duplicate buys on the same line |
| open orders identical on exchange and in the database | orphaned or lost orders |
| exchange position equals the sum of open trades | a fill that was missed or double-counted |
| every exchange fill processed; every filled order backed by a real fill | the fill loop falling behind |
| no taker fills | orders crossing the book instead of resting |
| fees booked match fees charged | fee accounting drift |
| errors in the log; the same line refilled twice in a row | the refill loop stacking orders |

It needs `kubectl` access to the namespace and Python 3.

---

## Telegram alerts (optional)

Create a bot with [@BotFather](https://t.me/BotFather). **Use a token that no other application
listens on** — Telegram allows only one listener per token, and two would take updates away
from each other.

```bash
read -rsp 'Bot token: ' TG_TOKEN; echo
kubectl -n gridstatic apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: gridstatic-telegram
type: Opaque
data:
  bot_token: $(printf '%s' "$TG_TOKEN" | base64 | tr -d '
')
stringData:
  chat_id: "YOUR_CHAT_ID"
EOF
unset TG_TOKEN
```

```yaml
telegram:
  enabled: true
  existingSecret: gridstatic-telegram
```

You get alerts when a grid is built, a buy fills, a trade closes, the price nears or leaves the
range, and when something fails — plus a `/status` command with a line-by-line view of every
grid. With Telegram disabled, alerts fail silently and the bot keeps running.

---

## Security

**What this setup does.** Your database password and exchange key never pass through Helm.
That matters: Helm stores the values you install with in a Secret in your cluster
(`sh.helm.release.v1.<name>.v1`), and `helm get values` prints them back to anyone allowed to run
it. Secrets are read with hidden input and sent to `kubectl` over stdin, so they never reach your
shell history or the process list.
CI fails if the chart ever renders a Secret, or if `install.sh --dry-run` prints a password.

**What it does not do.** Kubernetes Secrets are base64-encoded, not encrypted. Without
encryption at rest on etcd they are readable on your control-plane node's disk, and anyone with
`get secrets` in the namespace can read them. For more, look at sealed-secrets,
external-secrets or SOPS.

Use a dedicated Hyperliquid API wallet with a limited balance. The connector never needs your
main account key.

---

## Uninstalling

```bash
helm uninstall gridstatic -n gridstatic
kubectl delete namespace gridstatic
```

If a grid was live, cancel its orders and close its position on Hyperliquid first — uninstalling
stops the bot, not the orders it placed. Your data stays in your database.

---

## Development

```
services/grid-static/        the strategy
services/dal/                database access, schema applied on startup
services/connector/          Hyperliquid access
services/telegram-alerter/   alerts and /status
db/schema.sql                the database schema (source of truth)
chart/                       the Helm chart; chart/files/schema.sql is a checked copy
tools/gridcheck.py           read-only consistency check for a running grid
install.sh                   one-command installer
docs/design.md               design decisions and the incidents behind them
```

Run the tests (91 in total):

```bash
for s in grid-static dal connector telegram-alerter; do
  PYTHONPATH="$PWD/services/$s" python -m pytest "services/$s/tests/" -q
done
```

Lint the installer (`pip install shellcheck-py` ships the real binary):

```bash
shellcheck install.sh
```

**CI.** Each service has a workflow that runs its tests and builds a multi-arch image when its
directory changes. The chart workflow lints and renders the chart and enforces three rules:
installing without `database.existingSecret` must fail, the default values may never contain
`shadow: false`, and the chart may never render a Secret. A separate workflow runs `shellcheck`
and a dry run of `install.sh` that fails if a password shows up in the output.

---

## Design decisions

[`docs/design.md`](docs/design.md) explains the choices behind this setup — why a grid line is
identified by its number and not its price, why the database stores money as `numeric` and the
dal converts it back, why secrets bypass Helm, why the schema is applied by an initContainer
instead of a Helm hook — and the production incidents that led to several of them.
