#!/usr/bin/env bash
#
# gridstatic -- install the stack in shadow mode.
#
#   curl -fsSLO https://raw.githubusercontent.com/njwgroeneveld/gridstatic/master/install.sh
#   bash install.sh
#
# One question: your database connection. Everything else has a default, and every
# grid runs in shadow -- a simulation that never sends an order to the exchange.
# Going live is a manual step described in the README, deliberately not a script.

set -euo pipefail

NAMESPACE="${GRIDSTATIC_NAMESPACE:-gridstatic}"
RELEASE="${GRIDSTATIC_RELEASE:-gridstatic}"
CHART="${GRIDSTATIC_CHART:-oci://ghcr.io/njwgroeneveld/charts/gridstatic}"
VALUES_FILE="gridstatic-values.yaml"
DB_SECRET="gridstatic-db"
DRY_RUN=0

red=$'\033[31m'; green=$'\033[32m'; yellow=$'\033[33m'; bold=$'\033[1m'; reset=$'\033[0m'
say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s\n' "$bold" "$reset" "$*"; }
ok()   { printf '%s  ok%s  %s\n' "$green" "$reset" "$*"; }
warn() { printf '%s  warning%s  %s\n' "$yellow" "$reset" "$*"; }
err()  { printf '%s  error%s  %s\n' "$red" "$reset" "$*" >&2; }
die()  { err "$*"; exit 1; }

# In dry-run mode, show what would run instead of running it. Secrets never pass
# through this function: they go over stdin, see create_secret.
run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '       would run: %s\n' "$*"
  else
    "$@"
  fi
}

usage() {
  cat <<'EOF'
Install gridstatic (shadow mode)

  bash install.sh [options]

Options
  --namespace NAME   default: gridstatic
  --release NAME     default: gridstatic
  --chart REF        default: oci://ghcr.io/njwgroeneveld/charts/gridstatic
                     (point it at ./chart to test a local version)
  --dry-run          show what would happen without touching anything
  --help             this text

Environment variables (each one skips the matching question)
  GRIDSTATIC_DB_URL      connection string for your PostgreSQL / Supabase
  GRIDSTATIC_NAMESPACE   same as --namespace
  GRIDSTATIC_RELEASE     same as --release
  GRIDSTATIC_CHART       same as --chart

The database connection deliberately has no command-line option: it would end up
in your shell history and be visible in ps.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --release)   RELEASE="$2";   shift 2 ;;
    --chart)     CHART="$2";     shift 2 ;;
    --dry-run)   DRY_RUN=1;      shift ;;
    --help|-h)   usage; exit 0 ;;
    *)           die "unknown option: $1 (try --help)" ;;
  esac
done

# ── 1. Prerequisites ─────────────────────────────────────────────────────────
step "Checking this machine"

missing=0
if ! command -v kubectl >/dev/null 2>&1; then
  err "kubectl is missing -- https://kubernetes.io/docs/tasks/tools/"
  missing=1
else
  ok "kubectl found"
fi

if ! command -v helm >/dev/null 2>&1; then
  err "helm is missing -- curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash"
  missing=1
else
  ok "helm found ($(helm version --short 2>/dev/null || echo unknown))"
fi

if [ "$missing" = 1 ]; then
  if [ "$DRY_RUN" = 1 ]; then
    warn "dry-run: continuing anyway"
  else
    die "install the above and try again"
  fi
elif ! kubectl cluster-info >/dev/null 2>&1; then
  if [ "$DRY_RUN" = 1 ]; then
    warn "dry-run: no cluster reachable, continuing anyway"
  else
    die "no cluster reachable -- check your kubeconfig with 'kubectl cluster-info'"
  fi
else
  ok "cluster reachable ($(kubectl config current-context))"
fi

# ── 2. Database ──────────────────────────────────────────────────────────────
step "Your database"

DB_URL="${GRIDSTATIC_DB_URL:-}"

# Reinstalling is a normal case: helm uninstall only removes what Helm created, so
# the Secret is usually still there. Then there is no need to look up the
# connection string again.
REUSE_SECRET=0
if [ -z "$DB_URL" ] && [ "$DRY_RUN" = 0 ] && command -v kubectl >/dev/null 2>&1 \
   && kubectl -n "$NAMESPACE" get secret "$DB_SECRET" >/dev/null 2>&1; then
  say "  A Secret $DB_SECRET already exists in namespace $NAMESPACE."
  printf '  Use it? [Y/n]: '
  read -r answer
  case "$answer" in
    n|N) : ;;
    *)   REUSE_SECRET=1 ;;
  esac
fi

if [ "$REUSE_SECRET" = 1 ]; then
  ok "reusing the existing Secret -- no connection string needed"
elif [ -z "$DB_URL" ]; then
  cat <<'EOF'
  You need your own PostgreSQL; a free Supabase project is enough.
  In the dashboard under "Connect", copy the SESSION POOLER string:

    postgresql://postgres.PROJECT_REF:PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres?sslmode=require

  Not the direct connection (db.<ref>.supabase.co): it is IPv6-only and does not
  work from an IPv4 cluster.

  WARNING: do not use a database another gridstatic stack is already using. Two
  bots on the same grid_configs place duplicate orders.

EOF
  printf '  Connection string (input stays hidden): '
  read -rs DB_URL
  printf '\n'
fi

if [ "$REUSE_SECRET" = 0 ]; then
  [ -n "$DB_URL" ] || die "no connection string given"
  case "$DB_URL" in
    postgresql://*|postgres://*) : ;;
    *) die "that does not look like a connection string (expected postgresql://...)" ;;
  esac
  case "$DB_URL" in
    *:6543/*) warn "this is the transaction pooler (6543); the session pooler on 5432 suits a long-running service better" ;;
  esac
  ok "connection string received (${#DB_URL} characters)"
fi

# ── 3. Namespace ─────────────────────────────────────────────────────────────
step "Namespace $NAMESPACE"

if [ "$DRY_RUN" = 0 ] && kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
  ok "already exists"
else
  run kubectl create namespace "$NAMESPACE"
  if [ "$DRY_RUN" = 0 ]; then ok "created"; fi
fi

# ── 4. Secret ────────────────────────────────────────────────────────────────
step "Secret $DB_SECRET"

create_secret() {
  # Through apply, so running again does not fail with "already exists". The value
  # travels over a pipe and never appears in the process table.
  if [ "$DRY_RUN" = 1 ]; then
    printf '       would run: kubectl -n %s create secret generic %s --from-literal=url=<hidden> | kubectl apply -f -\n' \
      "$NAMESPACE" "$DB_SECRET"
    return
  fi
  kubectl -n "$NAMESPACE" create secret generic "$DB_SECRET" \
    --from-literal=url="$DB_URL" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}
if [ "$REUSE_SECRET" = 1 ]; then
  ok "left unchanged"
else
  create_secret
  if [ "$DRY_RUN" = 0 ]; then ok "created or updated"; fi
fi

# ── 5. Values file ───────────────────────────────────────────────────────────
step "Values file $VALUES_FILE"

if [ -f "$VALUES_FILE" ]; then
  ok "already exists -- left unchanged"
else
  if [ "$DRY_RUN" = 1 ]; then
    printf '       would write: %s\n' "$VALUES_FILE"
  else
    cat > "$VALUES_FILE" <<EOF
# Created by install.sh. There is no password in here: the connection lives in the
# Secret $DB_SECRET. You can keep this file and put it under version control.
database:
  existingSecret: $DB_SECRET

grid:
  # Basis for order sizing, live as well. With 20 lines and 80% allocation,
  # 1000 means \$40 per line.
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
EOF
    ok "written"
  fi
fi

# ── 6. Install ───────────────────────────────────────────────────────────────
step "Deploying the chart"

if [ "$DRY_RUN" = 0 ] && helm status "$RELEASE" -n "$NAMESPACE" >/dev/null 2>&1; then
  say "  release $RELEASE already exists -- upgrading"
  run helm upgrade "$RELEASE" "$CHART" -n "$NAMESPACE" -f "$VALUES_FILE"
else
  run helm install "$RELEASE" "$CHART" -n "$NAMESPACE" -f "$VALUES_FILE"
fi

if [ "$DRY_RUN" = 1 ]; then
  step "Dry run finished"
  say "  Nothing was touched. Run without --dry-run to do it for real."
  exit 0
fi
ok "deployed"

# ── 7. Verify ────────────────────────────────────────────────────────────────
step "Checking that it works"

say "  waiting for the pods to become ready (the dal applies the schema first)..."
if ! kubectl -n "$NAMESPACE" wait --for=condition=available --timeout=180s \
     deployment/"$RELEASE"-dal deployment/"$RELEASE"-grid-static >/dev/null 2>&1; then
  warn "not every pod was ready within three minutes"
fi

problems=0

# Accept the old Dutch message too, so this script still reads an older chart right.
schema_log="$(kubectl -n "$NAMESPACE" logs deploy/"$RELEASE"-dal -c schema 2>/dev/null || true)"
if printf '%s' "$schema_log" | grep -q "CREATE TABLE"; then
  ok "schema applied"
elif printf '%s' "$schema_log" | grep -qE "waiting for the database|wacht op de database"; then
  err "the dal cannot reach your database"
  say "       Almost always the direct connection instead of the session pooler:"
  say "       db.<ref>.supabase.co is IPv6-only."
  problems=1
else
  ok "schema was already in place"
fi

pods="$(kubectl -n "$NAMESPACE" get pods --no-headers 2>/dev/null || true)"
not_running="$(printf '%s\n' "$pods" | awk '$3 != "Running" && NF > 0')"
if [ -n "$not_running" ]; then
  err "not every pod is running:"
  printf '%s\n' "$not_running" | sed 's/^/       /'
  problems=1
else
  ok "all pods are running"
fi

restarts="$(printf '%s\n' "$pods" | awk '$4 > 0 && NF > 0 {print $1" ("$4"x)"}')"
if [ -n "$restarts" ]; then
  warn "restarted: $restarts"
  say "       kubectl -n $NAMESPACE logs deploy/$RELEASE-grid-static --previous"
fi

# The readiness probe makes the wait above meaningful, but the startup log line can
# still trail it by a moment. Poll for it instead of looking once.
say "  waiting for the bot to build its grid..."
bot_log=""
for _ in $(seq 1 30); do
  bot_log="$(kubectl -n "$NAMESPACE" logs deploy/"$RELEASE"-grid-static --tail=80 2>/dev/null || true)"
  if printf '%s' "$bot_log" | grep -qE "Initialising grid|Recovering state"; then
    break
  fi
  sleep 3
done

if printf '%s' "$bot_log" | grep -q "Initialising grid"; then
  ok "grid built: $(printf '%s' "$bot_log" | grep -o 'Grid ready — .*' | head -1)"
elif printf '%s' "$bot_log" | grep -q "Recovering state"; then
  ok "existing grid recovered"
else
  err "the bot did not build a grid within 90 seconds"
  last_lines="$(printf '%s' "$bot_log" | tail -3)"
  if [ -n "$last_lines" ]; then
    printf '%s\n' "$last_lines" | sed 's/^/       /'
  fi
  problems=1
fi

say "  checking that the fill loop runs cleanly (35 seconds)..."
sleep 35
fill_error="$(kubectl -n "$NAMESPACE" logs deploy/"$RELEASE"-grid-static --since=40s 2>/dev/null | grep "Fill loop error" | head -1 || true)"
if [ -n "$fill_error" ]; then
  err "error in the fill loop:"
  say "       $fill_error"
  problems=1
else
  ok "fill loop runs cleanly"
fi

# ── Verdict ──────────────────────────────────────────────────────────────────
if [ "$problems" = 0 ]; then
  step "${green}Done${reset} -- your stack is running"
  cat <<EOF

  Follow along:
    kubectl -n $NAMESPACE logs deploy/$RELEASE-grid-static -f

  Change your grids: edit $VALUES_FILE, then run
    helm upgrade $RELEASE $CHART -n $NAMESPACE -f $VALUES_FILE

  Trading with real money: see 'Going live' in the README. That step is manual on
  purpose -- it is not something you want to do quickly.
EOF
else
  step "${red}Something is wrong${reset}"
  cat <<EOF

  The problems are listed above. To see more:
    kubectl -n $NAMESPACE get pods
    kubectl -n $NAMESPACE logs deploy/$RELEASE-dal -c schema
    kubectl -n $NAMESPACE logs deploy/$RELEASE-grid-static --tail=50

  If no grid is live yet, starting over is safe:
    helm uninstall $RELEASE -n $NAMESPACE
  If a grid already trades with real money, check the exchange before you do.
EOF
  exit 1
fi
