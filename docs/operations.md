# Operating gridstatic

Everything needed to install, configure and run gridstatic. For what the project is and why it
is built this way, see the [README](../README.md) and [design decisions](design.md).

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Manual installation](#manual-installation)
- [Configuration](#configuration)
- [Stocks and indices: HIP-3 markets](#stocks-and-indices-hip-3-markets)
- [Stop-loss (optional)](#stop-loss-optional)
- [When the grid holds](#when-the-grid-holds)
- [Going to mainnet](#going-to-mainnet)
- [Checking a running grid](#checking-a-running-grid)
- [Telegram alerts (optional)](#telegram-alerts-optional)
- [Security](#security)
- [Uninstalling](#uninstalling)
- [Upgrading from 0.3](#upgrading-from-03)

---

## Requirements

| | why |
|---|---|
| A Kubernetes cluster | k3s, minikube, kubeadm — any of them |
| `kubectl` with access to it | the installation runs from your machine |
| `helm` 3 | `brew install helm`, `winget install Helm.Helm`, or the [install script](https://helm.sh/docs/intro/install/) |
| A Hyperliquid testnet account | [app.hyperliquid-testnet.xyz](https://app.hyperliquid-testnet.xyz), with test USDC from its faucet |
| Outbound internet from the cluster | to Hyperliquid, and to ghcr.io for the images |

You do **not** need a database, a StorageClass (the stack has no volumes), an ingress,
cert-manager or a service mesh.

### Give the bot an account of its own

Use a fresh account or a subaccount, and trade nothing else on it. Every round the bot checks
that the position adds up to its own orders. A position you opened by hand, or another bot on the
same coin, makes it hold — correctly, because it can no longer tell what is its own.

### Which key

| setup | `private_key` | `wallet_address` |
|---|---|---|
| the account itself | that account's key | empty |
| a subaccount | the **main** account's key | the subaccount's address |

**API wallets are not supported yet.** An API wallet trades on behalf of another account, and
the connector would read the API wallet's own, empty account instead of the one it trades for.
The bot detects this — none of its orders ever show up — and holds, but it cannot trade.

---

## Quick start

```bash
curl -fsSLO https://raw.githubusercontent.com/njwgroeneveld/gridstatic/master/install.sh
bash install.sh
```

The script asks for your testnet key (hidden input) and an optional subaccount, and then:

1. checks that `kubectl`, `helm` and your cluster are available
2. creates the `gridstatic` namespace
3. stores the key in a Kubernetes Secret
4. offers to set up [Telegram alerts](#telegram-alerts-optional): checks your bot token, looks
   up your chat id and sends a test message
5. writes `gridstatic-values.yaml` with a BTC grid on testnet
6. installs the Helm chart
7. **verifies the result**: all pods run, the bot started its grid, and its first rounds run
   without errors — or tells you it is on hold, and why

You end with a single verdict: done, or what went wrong and where to look.

> **Check the bounds.** The default grid runs from 75,000 to 83,000. Testnet prices drift from
> mainnet; if the testnet price is outside that range, edit `gridstatic-values.yaml` and upgrade
> (see [Changing a running grid](#changing-a-running-grid)).

| option | |
|---|---|
| `--dry-run` | show every step without touching anything |
| `--namespace`, `--release`, `--chart` | override the defaults |
| `GRIDSTATIC_HL_KEY`, `GRIDSTATIC_HL_WALLET` | skip the questions (useful in automation) |
| `GRIDSTATIC_TG_TOKEN`, `GRIDSTATIC_TG_CHAT` | include Telegram without asking; the chat id is looked up when empty |

The key and the bot token deliberately have no command-line flag: they would end up in your shell
history. Re-running the script is safe; it reuses the Secrets and upgrades the release, and adds
Telegram to an existing values file if you set it up later.

---

## Manual installation

The same steps by hand, if you prefer to see each one.

**1. Namespace**

```bash
kubectl create namespace gridstatic
```

**2. The Hyperliquid secret**

This chart contains no keys and creates no Secrets — you create them and pass only their names
(see [Security](#security) for why).

```bash
read -rsp 'Private key: ' HL_KEY; echo
read -rp  'Subaccount address (empty for none): ' HL_WALLET
kubectl -n gridstatic apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: gridstatic-hyperliquid
type: Opaque
data:
  private_key: $(printf '%s' "$HL_KEY" | base64 | tr -d '\n')
  wallet_address: "$(printf '%s' "$HL_WALLET" | base64 | tr -d '\n')"
EOF
unset HL_KEY HL_WALLET
```

This looks more roundabout than `kubectl create secret --from-literal=...`, on purpose. With
`--from-literal` the key lands in your shell history and in the process list. Here it is read
with hidden input, handed to `base64` over a pipe, and sent to `kubectl` over stdin.

**3. Values** — create `gridstatic-values.yaml`:

```yaml
hyperliquid:
  testnet: true
  existingSecret: gridstatic-hyperliquid

grid:
  startBalance: 1000
  coins:
    BTC-20:
      coin: BTC
      active: true
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
kubectl -n gridstatic logs deploy/gridstatic-grid-static -f
```

The bot should log `started: 20 lines 75000-83000, $40.0 per line, 1x`, followed by a `BUY` line
for every cell below the price. If it logs `Refusing to start`, the message below it names the
setting it will not trade with.

---

## Configuration

All settings live in your values file. The full list with comments is in
[`chart/values.yaml`](../chart/values.yaml).

### Grids

```yaml
grid:
  strategyAllocationPct: 80       # share of startBalance available to all grids
  startBalance: 1000              # basis for order sizing -- see below
  roundIntervalSeconds: 30        # how often the bot reads the exchange and repairs the grid
  fillLookbackHours: 72           # how far back it looks for coin a cell bought but did not sell
  coins:
    SOL-20:                       # free-form label, used in logs and alerts
      coin: SOL                   # Hyperliquid symbol
      active: true
      allocationPct: 100          # this grid's share of the strategy allocation
      lower: 93
      upper: 107
      numLines: 20
      leverage: 1
```

**Order size per line** is fixed:

```
startBalance × strategyAllocationPct% × allocationPct% ÷ numLines
```

With the defaults above, 1000 × 80% × 100% ÷ 20 = **$40 per line**, and up to $760 in the
market if all 19 cells fill. The size does not grow with profit: the profit since the start is
not something the exchange can report once its fill history rolls over, and the bot keeps no
record of its own.

A grid can set its own `startBalance`, which then overrides the global one for that grid. That
fits grids that margin from different balances — BTC from your regular perps account, a HIP-3
market from its own dex:

```yaml
    NDX-20:
      coin: "xyz:XYZ100"
      startBalance: 500            # this grid only; the others keep grid.startBalance
```

`startBalance` is not read from your account. The bot deliberately does not size from your
account value: that value moves with unrealised losses, so lines would shrink exactly while the
grid is buying its way down. At startup it compares what a full grid needs with your account
value and warns if the account cannot carry it.

**The bot refuses to start** — with a message that names the problem — when:

| | why |
|---|---|
| an order would be under $10 (leverage counts) | Hyperliquid rejects it, and the cell would stay empty |
| two grids trade the same coin | the exchange keeps one position per coin; two grids cannot tell theirs apart |
| a grid says `shadow: true` | shadow mode is gone; starting would place real orders for a grid you believe is simulated |
| `startBalance` is missing | it sizes every order; there is no safe default |

`grid.coins` has no default: the chart refuses to install without an active grid. That is on
purpose — Helm merges maps, so a default grid would quietly run next to the ones in your file.

### Other settings

| key | default | |
|---|---|---|
| `hyperliquid.testnet` | `true` | `false` means mainnet and real money |
| `hyperliquid.existingSecret` | *(required)* | Secret with `private_key` and `wallet_address` |
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

> **Changing `coin`, `numLines`, `lower`, `upper` or `leverage` makes a new grid.** Every order
> carries a fingerprint of exactly those five settings. After the change, the old grid's orders
> carry a different fingerprint: the bot leaves them alone, but it also holds — its position no
> longer adds up. Cancel the old orders and close or keep the old position deliberately before
> you change them. Changing `allocationPct` or `startBalance` is safe; it applies to new orders.

---

## Stocks and indices: HIP-3 markets

Hyperliquid also lists markets deployed by third parties on perp dexes of their own (HIP-3) —
the Nasdaq-100, single stocks, commodities. Their names carry the dex: `xyz:XYZ100` is the
Nasdaq-100 on the `xyz` dex. Use that full name, quoted:

```yaml
grid:
  coins:
    NDX-20:
      coin: "xyz:XYZ100"
      active: true
      allocationPct: 100
      upper: 32000
      lower: 29000
      numLines: 20
      leverage: 1
```

The chart passes the dex on to the connector; nothing else to configure. Three things differ
from a regular market:

- **Each HIP-3 dex keeps its own balance.** USDC on your regular perps account does not margin
  an `xyz` position. Move USDC to that dex first — a `sendAsset` transfer to it, or enable dex
  abstraction so the account shares collateral across dexes. The startup margin check reads the
  balance of the coin's own dex and warns if it is short.
- **Check the market on the network you use.** A market can be busy on mainnet and nearly empty
  on testnet. On testnet a grid there mostly tests the plumbing: with no one trading, its orders
  rarely fill.
- **Look at the market's own rules** in its listing — maximum leverage, isolated-only, trading
  hours — before you pick a leverage.

`tools/gridcheck.py` handles HIP-3 coins the same way.

---

## Stop-loss (optional)

A grid without a stop-loss holds every position it bought on the way down for as long as the
price stays below the range. With one, it sells everything when the price falls through it:

```yaml
    BTC-10:
      coin: BTC
      lower: 82000
      upper: 91000
      stopLoss: 80000              # optional; must lie below lower
```

- **It lives on the exchange.** A reduce-only stop-market order, always the size of the whole
  position, kept up to date every round. It fires even when the bot or your cluster is down.
- **It stays while the grid holds.** A hold stops new buys; it never takes the protection away.
- **The grid resumes on its own.** After a stop the price is below the grid, so no line gets a buy.
  Once the price is back inside, the lines below it get their buys again. You get a
  `stopped_out` alert, and a `grid_resumed` alert when it starts buying again.
- **Slippage is capped at 10%** below the stop price. On a thin book a bad fill beats none.

The price can come back, fall through again and stop you out twice. A stop well below `lower`
makes that less likely and costs more when it does fire. Leave `stopLoss` out for no stop at all.

---

## When the grid holds

On hold, the bot places **no new buys**. It still places sells for coin it can account for, and
it resumes by itself as soon as the cause is gone. You get a `grid_hold` alert with the reason,
and a `hold_cleared` alert when it resumes. `/status` shows it too.

| reason | what to do |
|---|---|
| the position has coin no cell accounts for | a manual position, another bot on the coin, or fills older than `fillLookbackHours`. Close the extra position, or sell it yourself. |
| orders from an earlier grid on this coin | you changed the grid's bounds, lines or leverage. Cancel the old orders, or restore the old settings. |
| resting sells exceed the position | someone sold coin the bot had a sell for. Cancel the excess sell. |
| none of the orders placed last round is on the book or filled | the bot reads another account than it trades on. Check `wallet_address` — see [Which key](#which-key). |
| short position | a grid only ever holds long. Close the short. |

The rule behind it: a buy too few costs one missed cycle; a buy too many is how a grid once
bought the same line eight times. See [`docs/design.md`](design.md).

---

## Going to mainnet

Manual on purpose. Run on testnet until the grid has done what you expect for long enough to
trust it, then:

1. Create a Secret with your **mainnet** key, the same way as the testnet one — a separate
   account or subaccount with a balance you are willing to lose.
2. In `gridstatic-values.yaml`, set `hyperliquid.testnet: false` and point
   `hyperliquid.existingSecret` at the new Secret.
3. Check the bounds against the mainnet price, and the size per line against your balance.
4. Upgrade, and read the first rounds in the log:

```bash
helm upgrade gridstatic oci://ghcr.io/njwgroeneveld/charts/gridstatic \
  -n gridstatic -f gridstatic-values.yaml
kubectl -n gridstatic logs deploy/gridstatic-grid-static -f
```

The testnet orders stay on testnet; nothing carries over.

---

## Checking a running grid

**What the bot sees** — every cell, the position, and whether it holds:

```bash
kubectl -n gridstatic port-forward svc/gridstatic-grid-static 8080:8080
curl -s localhost:8080/status
```

**An independent check** — `tools/gridcheck.py` reads your grid settings from the cluster and
your account straight from Hyperliquid's public API: no key, no connector, nothing changed.

```bash
pip install pyyaml
python3 tools/gridcheck.py --address 0xTHE_ACCOUNT_THE_BOT_TRADES_ON
```

| check | what it catches |
|---|---|
| no orders from an earlier grid | a settings change that left orders behind |
| at most one buy and one sell per cell | duplicate orders |
| every sell is reduce-only, no buy at or above the price | orders that could open a short or cross the book |
| the position adds up to the bot's own orders | anything that would make the bot hold |
| what the next round would place or cancel | a grid that is not complete when it should be |

It needs `kubectl` access to the namespace (or `--settings settings.yaml`) and Python 3.

---

## Telegram alerts (optional)

Create a bot with [@BotFather](https://t.me/BotFather). **Use a token that no other application
listens on** — Telegram allows only one listener per token, and two would take updates away
from each other.

**The easy way:** run `bash install.sh` again and answer yes to the Telegram question. It checks
the token, looks up your chat id once you have sent the bot a message, sends a test message,
creates the Secret and turns the alerter on — without the token ever showing on screen.

**By hand:**

```bash
read -rsp 'Bot token: ' TG_TOKEN; echo
kubectl -n gridstatic apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: gridstatic-telegram
type: Opaque
data:
  bot_token: $(printf '%s' "$TG_TOKEN" | base64 | tr -d '\n')
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

You get alerts when a buy fills, a cycle closes (with its profit after fees), the grid goes on or
off hold, the price nears or leaves the range, and when something fails — plus a `/status`
command with a cell-by-cell view of every grid. With Telegram disabled, alerts fail silently and
the bot keeps running.

---

## Security

**What this setup does.** Your exchange key never passes through Helm. That matters: Helm stores
the values you install with in a Secret in your cluster (`sh.helm.release.v1.<name>.v1`), and
`helm get values` prints them back to anyone allowed to run it. Secrets are read with hidden
input and sent to `kubectl` over stdin, so they never reach your shell history or the process
list. CI fails if the chart ever renders a Secret, or if `install.sh --dry-run` prints the key.

Sells are always placed **reduce-only**: whatever the bot believes, a sell can never open a short.

**What it does not do.** Kubernetes Secrets are base64-encoded, not encrypted. Without encryption
at rest on etcd they are readable on your control-plane node's disk, and anyone with `get
secrets` in the namespace can read them. For more, look at sealed-secrets, external-secrets or
SOPS.

The key in the Secret can withdraw funds. Keep only what the grid needs on that account.

---

## Uninstalling

```bash
helm uninstall gridstatic -n gridstatic
kubectl delete namespace gridstatic
```

Uninstalling stops the bot, not the orders it placed: cancel them and close the position on
Hyperliquid yourself. Installing again with the same grid settings picks the grid up where it
was — the orders still carry its fingerprint.

---

## Upgrading from 0.3

0.4 removes the database and shadow mode.

- **Shadow grids** never placed orders, so there is nothing to carry over: uninstall 0.3, create
  a testnet key Secret, remove `database` and every `shadow` from your values file, and install
  0.4. The chart refuses a grid that still says `shadow: true`.
- **Live grids** placed their orders without a cloid, so 0.4 does not recognise them. With a
  position open it holds; with only resting buys it places its own buys next to them. Either
  way: cancel the 0.3 orders and close the position before you upgrade.
- The database and its Secret are no longer used; delete them when you no longer need the history.

---
