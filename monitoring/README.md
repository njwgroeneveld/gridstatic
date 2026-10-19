# Monitoring (optional)

Not part of the installation: a grid runs without any of this. grid-static and the connector
serve Prometheus metrics on `:8080/metrics/`; this folder holds what turns them into a dashboard
and alerts, for a cluster that runs
[kube-prometheus-stack](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack).

| file | what it is |
|---|---|
| `servicemonitors.yaml` | lets Prometheus scrape grid-static and the connector |
| `alerts.yaml` | two alert rules, `GridstaticDown` and `GridstaticHold` |
| `dashboard.json` | the Grafana dashboard |

<!-- Screenshots: put PNGs in monitoring/screenshots/ and list them here. -->

## Applying it

The files are written for the default install — release `gridstatic` in namespace `gridstatic` —
and for a kube-prometheus-stack that selects on `release: monitoring`. With other names, change
the `namespace` and `release` lines first.

```bash
kubectl apply -f monitoring/servicemonitors.yaml -f monitoring/alerts.yaml

# The dashboard, for Grafana's sidecar (kube-prometheus-stack looks for this label). The key is
# the file name the sidecar writes, shared with every other dashboard: keep it distinct.
kubectl -n gridstatic create configmap gridstatic-dashboard \
  --from-file=gridstatic.json=monitoring/dashboard.json
kubectl -n gridstatic label configmap gridstatic-dashboard grafana_dashboard=1
# ... or import monitoring/dashboard.json by hand: Dashboards → New → Import.
```

## The dashboard

One screen, four rows:

- **Grids** — per grid whether it trades or holds, its realised P&L, how much of its budget is in
  use, and its price between the lowest and highest line with the stop-loss below.
- **Alerts** — when each alert was pending or firing.
- **Exchange API** — how long Hyperliquid takes per kind of request, where the waiting adds up,
  and which requests fail.
- **Health** — whether Prometheus reaches the bot, the share of rounds that completed, seconds
  since the last round, and CPU, CPU throttling and memory per pod against their limits.

## Metrics

| metric | what it is |
|---|---|
| `grid_realized_gain_usd_total`, `grid_realized_loss_usd_total`, `grid_fees_usd_total` | `closedPnl` and fees of the grid's fills on the exchange, **stop-outs included**. Net realised P&L over a range is `increase(gain) - increase(loss) - increase(fees)`. |
| `grid_on_hold` | 1 while new buys are held back — see [When the grid holds](../docs/operations.md#when-the-grid-holds) |
| `grid_mark_price`, `grid_lower_bound`, `grid_upper_bound`, `grid_stop_loss_price` | the price and the grid around it |
| `grid_budget_usd`, `grid_position_value_usd` | the position value with every cell bought, and the position value now — both leverage included |
| `grid_last_round_timestamp_seconds` | when the last complete round ended: a bot can be up and still be stuck |
| `connector_hl_request_duration_seconds{api, request}` | how long Hyperliquid takes per request type (`userFillsByTime`, `frontendOpenOrders`, `order`, …), measured in the connector where it goes over the network |
| `connector_hl_requests_total{api, request, outcome}` | the same requests by outcome: `ok`, `rejected` (answered but refused), `rate_limited`, `http_4xx`, `http_5xx`, `timeout`, `connection` |
| `grid_profit_usd` | the bot's own view of completed buy→sell cycles. It starts at zero on every restart and leaves out stop-outs; use the realised counters above for P&L. |

The realised counters count each fill once, from the moment the pod starts. Fills that happen
while the pod is down are not counted.

## Alerts

Prometheus evaluates the rules and Alertmanager sends them wherever you route alerts — so unlike
the Telegram messages the bot sends itself, they also arrive when the bot is down.

### GridstaticDown

No healthy grid-static target for 5 minutes. No round runs, so nothing is bought or sold; the
stops on the exchange still protect the positions.

1. `kubectl -n gridstatic get pods` — is the pod there? `0/1`, `CrashLoopBackOff`, `Pending`?
2. `kubectl -n gridstatic describe pod -l app=gridstatic-grid-static` — the events say why it
   does not start: an image that cannot be pulled, a node that is gone, an OOM kill.
3. `kubectl -n gridstatic logs deploy/gridstatic-grid-static --previous` — the last words of a
   crashed pod. "Refusing to start" means a configuration error; the message names it.
4. Pod running but the alert still fires? Then Prometheus cannot reach it: check that the
   ServiceMonitor still selects the Service (`kubectl -n gridstatic get endpoints`).

### GridstaticHold

A grid has placed no new buys for 30 minutes because its position does not add up. Sells and the
stop still go out.

1. Find the reason: `kubectl -n gridstatic logs deploy/gridstatic-grid-static | grep "on hold"`,
   or the `hold` field in `/status`.
2. Look it up in [When the grid holds](../docs/operations.md#when-the-grid-holds) — every reason
   there has its fix.
3. `tools/gridcheck.py` shows, cell by cell, what the bot sees and what it would do next.
