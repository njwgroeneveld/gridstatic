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
- [Prometheus and Grafana (optional)](#prometheus-and-grafana-optional)
- [Telegram alerts (optional)](#telegram-alerts-optional)
- [Security](#security)
- [Uninstalling](#uninstalling)

---

## Requirements

| | why |
|---|---|
| A Kubernetes cluster | any: k3s, minikube, or one set up with kubeadm |
| `kubectl` with access to it | the installation runs from your machine |
| `helm` 3 | `brew install helm`, `winget install Helm.Helm`, or the [install script](https://helm.sh/docs/intro/install/) |
| A Hyperliquid testnet account | [app.hyperliquid-testnet.xyz](https://app.hyperliquid-testnet.xyz), with test USDC from its faucet |
| Outbound internet from the cluster | to Hyperliquid, and to ghcr.io for the images |

Nothing else: no database and no persistent storage, since the bot keeps no state of its own.

### Give the bot an account of its own

Use a fresh account or a subaccount, and trade nothing else on it. Every round the bot checks
that the position adds up to its own orders. A position you opened by hand, or another bot on the
same coin, makes it hold. That is correct, because it can no longer tell what is its own.

### Which key

| setup | `private_key` | `wallet_address` |
|---|---|---|
| the account itself | that account's key | empty |
| a subaccount | the **main** account's key | the subaccount's address |
| an API wallet for the account | the **API wallet's** key | the account's address |
| an API wallet for a subaccount | the **API wallet's** key | the subaccount's address |

The address is always the account that holds the money, never the API wallet's own address,
even though Hyperliquid's API page shows that one most prominently. `install.sh` catches
that mistake before it installs anything.

**Prefer an API wallet.** Create one under *API* on Hyperliquid, approved by your main account. It
can trade, but it cannot withdraw or move funds, so a leaked key cannot drain the account. At
startup the connector asks the exchange (`userRole`) whether the key is an API wallet and whether
`wallet_address` is a subaccount, and signs accordingly. An API wallet expires on the date you set
when you created it; after that the exchange refuses its orders. The bot keeps reading and
retrying, and alerts at most once an hour per grid that orders are refused, with the exchange's
reason.

---

## Quick start

```bash
curl -fsSLO https://raw.githubusercontent.com/njwgroeneveld/gridstatic/master/install.sh
bash install.sh
```

The script asks for your testnet key (hidden input) and an optional subaccount, and then:

1. checks that `kubectl`, `helm` and your cluster are available
2. creates the `gridstatic` namespace
3. stores the key in a Kubernetes Secret and **checks it with Hyperliquid testnet**: whether
   the key is an API wallet or an account key, whether the address is an account it may
   trade for, and whether that account holds USDC. If they do not fit together, it says why,
   lists the accounts this key can trade on, and asks again. The check runs as a short-lived
   pod from the connector image and asks only Hyperliquid's public info endpoint about
   addresses; the key never leaves the cluster.
4. offers to set up [Telegram alerts](#telegram-alerts-optional): checks your bot token, looks
   up your chat id and sends a test message
5. writes `gridstatic-values.yaml`, a copy of
   [`gridstatic-values.example.yaml`](../gridstatic-values.example.yaml): three grids on
   testnet, BTC, SOL and Nasdaq-100, **all switched off**. An existing values file is left alone: it holds
   your settings
6. installs the Helm chart
7. **verifies the result**: all pods run and the bot started; with a grid active, also that
   its first rounds run without errors, or that it is on hold, and why

You end with a single verdict: done, or what went wrong and where to look.

### Starting a grid

**Nothing trades after the installation.** Every grid in `gridstatic-values.yaml` has
`active: false`, so you decide which one starts, and when:

1. **Check the bounds** against the current price on
   [the testnet site](https://app.hyperliquid-testnet.xyz): `lower` below the price, `upper`
   above it. Testnet prices drift from mainnet, so the defaults may not fit (BTC 75,000-83,000,
   SOL 93-107, Nasdaq-100 29,000-32,000).
2. **Nasdaq-100 only:** move USDC to the `xyz` dex first. It keeps a balance of its own; see
   [HIP-3 markets](#stocks-and-indices-hip-3-markets).
3. **BTC and SOL together:** both trade on the default dex and share its balance, so their
   `allocationPct` may add up to at most 100. The example has BTC at 90 and SOL at 45: lower
   BTC to 45 before you switch both on.
4. Set `active: true` on that grid.
5. Upgrade:

   ```bash
   helm upgrade gridstatic oci://ghcr.io/njwgroeneveld/charts/gridstatic \
     -n gridstatic -f gridstatic-values.yaml
   ```

6. Follow the log: `kubectl -n gridstatic logs deploy/gridstatic-grid-static -f`. The grid logs
   `started: ...`, then a `BUY` for every cell below the price.

To stop a grid again, set `active: false` and upgrade. grid-static then cancels its buys and
sells; its stop-loss stays as long as there is a position. Close that position yourself.

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

This chart contains no keys and creates no Secrets: you create them and pass only their names
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

**3. Values**: start from the example in the repo:

```bash
curl -fsSLo gridstatic-values.yaml   https://raw.githubusercontent.com/njwgroeneveld/gridstatic/master/gridstatic-values.example.yaml
```

Both its grids are switched off. Check the BTC grid's bounds against the current testnet price
and set `active: true` on it, or leave both off and start one later
([Starting a grid](#starting-a-grid)).

**4. Install**

```bash
helm install gridstatic oci://ghcr.io/njwgroeneveld/charts/gridstatic \
  -n gridstatic -f gridstatic-values.yaml
```

**5. Check**

```bash
kubectl -n gridstatic logs deploy/gridstatic-grid-static -f
```

The bot should log `started: 20 lines 75000-83000, $45.0 per line now, 1x, 90% of its dex` (with $1,000 on the default dex), followed by a `BUY` line
for every cell below the price. If it logs `Refusing to start` instead, the next line says which
setting is wrong and why; fix it in your values file and upgrade.

---

## Configuration

All settings live in your values file. The full list with comments is in
[`chart/values.yaml`](../chart/values.yaml).

### Grids

```yaml
grid:
  coins:
    SOL-20:                       # free-form label, used in logs and alerts
      coin: SOL                   # the market as Hyperliquid names it; HIP-3: "dex:NAME"
      active: true
      allocationPct: 90           # share of its dex's realised equity
      maxEquityUsd: 500           # optional: size from at most this much; leave out for no limit
      lower: 93
      upper: 107
      numLines: 20
      leverage: 1                 # always isolated margin
      stopLoss: 88                # optional: sell everything below this; must lie below lower
```

**Order size per line** follows the money on the grid's dex, every round:

```
(account value of the dex − unrealised P&L on it) × allocationPct% ÷ numLines
```

With $1,000 on the default dex, 90% and 20 lines that is **$45 per line**. Closed profit raises
it, a closed loss lowers it; the price of an open position does not, so the grid does not shrink
while it buys its way down. Orders already resting keep their size.

**Every perp dex has its own balance.** BTC trades on the default dex (which shares with spot
USDC), `xyz:XYZ100` on the `xyz` dex. Each grid grows with the profit on its own dex. To shift
capital between grids on different dexes, transfer USDC between the dexes yourself; the next
round sizes from it. **Every USDC on a dex counts as grid capital**: on the default dex
spot USDC too, so do not keep money for something else on the same wallet. Grids on the
same dex share it: their `allocationPct` may add up to at most
100; keep it near 90 to leave room for fees and funding.

**`allocationPct` adds up per dex, not across all grids.** A BTC grid at 90 and a Nasdaq-100
grid at 90, as in the example values, use 90% of two different balances: the default dex's
and the `xyz` dex's. Two grids on the default dex, say BTC and SOL, share one balance and may
use at most 100 together; the bot refuses to start otherwise and names the dex.

**Leverage is always isolated.** The connector sets every grid's leverage as isolated margin,
never cross: each position has margin of its own, and a loss on one grid cannot pull the
margin from under another.

**A ceiling, if you want one.** `maxEquityUsd` caps the equity a grid sizes from: with $612 on
the dex, a cap of 500, 90% and 10 lines, the size is $45 a line instead of $55. Money above it
stays on the dex. Leave it out and the grid grows with everything on its dex. When the cap is
reached the bot says so once, and `/status` shows `capped`; raise it to let the grid grow. It
also limits what a stray deposit or a wrong reading can do to the empty cells after a restart.

**Adding a grid** never needs a sale: the other grids keep their fingerprint, orders, positions
and stops; the upgrade only restarts grid-static for a few seconds. On a shared dex, lower the
other grids' `allocationPct` first if the total would pass 100. That only makes their next buys
smaller.

**At startup the bot checks the settings**, and refuses to start, naming the problem, when two
grids trade the same coin (the exchange keeps one position per coin, so they could not tell theirs
apart), when the grids on one dex together use more than 100% of it, or when a value is out of
range, such as a `stopLoss` above `lower`. A wrong setting then costs a restart, not money.

**An order under the exchange's $10 minimum** only gives a warning: the size depends on the money
on the dex, which can change. Those cells stay empty until the dex holds enough.

`grid.coins` has no default: the chart refuses to install without an active grid. That is on
purpose. Helm merges maps, so a default grid would quietly run next to the ones in your file.

### Other settings

| key | default | |
|---|---|---|
| `grid.roundIntervalSeconds` | `30` | how often each grid runs a round |
| `grid.fillLookbackHours` | `72` | how far back the bot reads fills. Only matters after an outage: a buy that filled more than this long before the bot saw it cannot be tied to its cell, and the grid holds |
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
> carries a fingerprint of exactly those five settings. After the upgrade the new grid cancels
> the old grid's buys and sells on that coin in its first round, and places its own. What the old
> grid had bought stays as a position that belongs to no cell of the new one: the grid then
> holds, places no buys and says how much it is about. Close that position on the exchange and
> it resumes by itself. The old stop-loss stays as long as there is such a position and the new
> grid has no stop of its own. So change these settings while the grid is flat, if you can.
> Changing `allocationPct` or `maxEquityUsd` is safe: it only sizes the next buys differently.
>
> **Moving a grid to another coin, or switching it off:** keep the old entry in your values file
> with `active: false`. At its next start grid-static cancels the buys and sells it left on that
> coin, and a later `helm uninstall` finds it too. An entry you delete is forgotten: its orders
> stay on the exchange until you cancel them there.

---

## Stocks and indices: HIP-3 markets

Hyperliquid also lists markets deployed by third parties on perp dexes of their own (HIP-3):
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
  an `xyz` position. Transfer USDC to that dex first. The startup margin check reads the
  balance of the coin's own dex and warns if it is short.

  Hyperliquid also offers *dex abstraction*, which lets dexes share one pool of collateral.
  Avoid it for gridstatic: the bot assumes each dex has a balance of its own, so with a shared
  pool a BTC grid at 90 and a Nasdaq-100 grid at 90 would both size from the same money. If you
  use it anyway, keep the `allocationPct` of all grids together at or below 100.
- **Check the market on the network you use.** A market can be busy on mainnet and nearly empty
  on testnet. On testnet a grid there mostly tests the plumbing: with no one trading, its orders
  rarely fill.
- **Look at the market's own rules** in its listing (maximum leverage, isolated-only, trading
  hours) before you pick a leverage.

`tools/gridcheck.py` handles HIP-3 coins the same way.

---

## Stop-loss (optional)

A grid without a stop-loss holds every position it bought on the way down for as long as the
price stays below the range. With one (`stopLoss` in the [grid example](#grids)), it sells the
whole position when the price falls through it.

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

On hold, the bot places **no new buys**. It still places sells for what its cells hold, and
it resumes by itself as soon as the cause is gone. You get a `grid_hold` alert with the reason,
and a `hold_cleared` alert when it resumes. `/status` shows it too.

| reason | what to do |
|---|---|
| the position holds more than the cells account for | a manual position, another bot on the coin, or fills older than `fillLookbackHours`. Close the extra position, or sell it yourself. |
| the position holds more than the cells account for, *most likely what the earlier grid on this coin bought* | you changed the grid's bounds, lines or leverage while it held coin. The old orders are already cancelled; close the old position on the exchange. |
| the bot's sells and what its cells hold add up to more than the position | someone sold what the bot had a sell for, or closed the position by hand. Cancel the excess sell; an amount without a sell clears once its buy is older than `fillLookbackHours`. |
| none of the orders placed last round is on the book or filled | the bot reads another account than it trades on. Check `wallet_address`; see [Which key](#which-key). |
| short position | a grid only ever holds long. Close the short. |

The rule behind it: a buy too few costs one missed cycle; a buy too many is how a grid once
bought the same line eight times. See [`docs/design.md`](design.md).

---

## Going to mainnet

Manual on purpose. Run on testnet until the grid has done what you expect for long enough to
trust it, then:

1. Create a Secret with your **mainnet** key, the same way as the testnet one: a separate
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

**What the bot sees**: every cell, the position, and whether it holds:

```bash
kubectl -n gridstatic port-forward svc/gridstatic-grid-static 8080:8080
curl -s localhost:8080/status
```

**An independent check**: `tools/gridcheck.py` reads your grid settings from the cluster and
your account straight from Hyperliquid's public API: no key, no connector, nothing changed.

```bash
pip install pyyaml
python3 tools/gridcheck.py --address 0xTHE_ACCOUNT_THE_BOT_TRADES_ON
```

| check | what it catches |
|---|---|
| no orders from an earlier grid | a settings change whose old orders the bot could not cancel |
| at most one buy and one sell per cell | duplicate orders |
| every sell is reduce-only, no buy at or above the price | orders that could open a short or cross the book |
| the position adds up to the bot's own orders | anything that would make the bot hold |
| what the next round would place or cancel | a grid that is not complete when it should be |

It needs `kubectl` access to the namespace (or `--settings settings.yaml`) and Python 3.

---

## Prometheus and Grafana (optional)

Not part of the installation; nothing here is needed to run a grid. grid-static and the connector
serve Prometheus metrics on `:8080/metrics/`. For a cluster with kube-prometheus-stack,
[`monitoring/`](../monitoring/README.md) holds the scrape targets, two alert rules with their
runbooks, and a Grafana dashboard. They are applied separately from `install.sh`, with `kubectl`
from a checkout of this repository; the steps, including how to check that your stack picks them
up, are in [Applying it](../monitoring/README.md#applying-it).

---

## Telegram alerts (optional)

Create a bot with [@BotFather](https://t.me/BotFather). **Use a token that no other application
listens on**: Telegram allows only one listener per token, and two would take updates away
from each other.

**The easy way:** run `bash install.sh` again and answer yes to the Telegram question. It checks
the token, looks up your chat id once you have sent the bot a message, sends a test message,
creates the Secret and turns the alerter on, without the token ever showing on screen.

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
off hold, the price nears or leaves the range, and when something fails, plus a `/status`
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

Unless it is an API wallet, the key in the Secret can withdraw funds. Keep only what the grid
needs on that account.

---

## Uninstalling

```bash
helm uninstall gridstatic -n gridstatic
kubectl delete namespace gridstatic
```

Before Helm removes anything, a short Job asks grid-static to stop for good: it finishes the
round it is in, runs no more, and cancels every buy and sell it placed on the coins in your
values file, those of grids with `active: false` included.
Orders without its tag, placed by hand or by another bot, stay. The output ends with what was
cancelled per grid, and a Telegram message says the same.

- **The stop-loss stays while there is a position**, so what the grid holds remains protected.
  Close the position on Hyperliquid yourself; the stop goes with it. Without a position the stop
  is cancelled too.
- **If a cancel fails**, the uninstall stops and nothing is removed. The grids are already
  stopped and place nothing new. Run `kubectl -n gridstatic logs job/gridstatic-cancel-orders`
  to see what failed, then run `helm uninstall` again. To remove the stack without cancelling
  anything, use `helm uninstall --no-hooks`.
- **Only `helm uninstall` cancels.** A restart, an upgrade, a node that goes down or
  `kubectl delete namespace` on its own leaves the orders where they are; the next start picks
  them up. To keep the orders on an uninstall, set `uninstall.cancelOrders: false`.
