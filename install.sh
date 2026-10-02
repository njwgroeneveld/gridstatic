#!/usr/bin/env bash
#
# gridstatic -- install the stack on Hyperliquid testnet.
#
#   curl -fsSLO https://raw.githubusercontent.com/njwgroeneveld/gridstatic/master/install.sh
#   bash install.sh
#
# One question: your Hyperliquid testnet key -- plus, if you like, a Telegram bot
# for alerts. Everything else has a default, and every order goes to testnet --
# nothing here costs real money. Moving to mainnet is a manual step described in
# the README, deliberately not a script.

set -euo pipefail

NAMESPACE="${GRIDSTATIC_NAMESPACE:-gridstatic}"
RELEASE="${GRIDSTATIC_RELEASE:-gridstatic}"
CHART="${GRIDSTATIC_CHART:-oci://ghcr.io/njwgroeneveld/charts/gridstatic}"
VALUES_FILE="gridstatic-values.yaml"
HL_SECRET="gridstatic-hyperliquid"
TG_SECRET="gridstatic-telegram"
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
Install gridstatic (Hyperliquid testnet)

  bash install.sh [options]

Options
  --namespace NAME   default: gridstatic
  --release NAME     default: gridstatic
  --chart REF        default: oci://ghcr.io/njwgroeneveld/charts/gridstatic
                     (point it at ./chart to test a local version)
  --dry-run          show what would happen without touching anything
  --help             this text

Environment variables (each one skips the matching question)
  GRIDSTATIC_HL_KEY      private key of the testnet account (0x + 64 hex)
  GRIDSTATIC_HL_WALLET   subaccount to trade on; empty = the key's own account
  GRIDSTATIC_TG_TOKEN    Telegram bot token; set it to include alerts
  GRIDSTATIC_TG_CHAT     Telegram chat id; looked up for you when empty
  GRIDSTATIC_NAMESPACE   same as --namespace
  GRIDSTATIC_RELEASE     same as --release
  GRIDSTATIC_CHART       same as --chart

The key and the bot token deliberately have no command-line option: they would end
up in your shell history and be visible in ps.
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

# ── 2. Hyperliquid key ───────────────────────────────────────────────────────
step "Your Hyperliquid testnet key"

HL_KEY="${GRIDSTATIC_HL_KEY:-}"
HL_WALLET="${GRIDSTATIC_HL_WALLET:-}"

# Reinstalling is a normal case: helm uninstall only removes what Helm created, so
# the Secret is usually still there. Then there is no need to paste the key again.
REUSE_SECRET=0
if [ -z "$HL_KEY" ] && [ "$DRY_RUN" = 0 ] && command -v kubectl >/dev/null 2>&1 \
   && kubectl -n "$NAMESPACE" get secret "$HL_SECRET" >/dev/null 2>&1; then
  say "  A Secret $HL_SECRET already exists in namespace $NAMESPACE."
  printf '  Use it? [Y/n]: '
  read -r answer
  case "$answer" in
    n|N) : ;;
    *)   REUSE_SECRET=1 ;;
  esac
fi

if [ "$REUSE_SECRET" = 1 ]; then
  ok "reusing the existing Secret -- no key needed"
elif [ -z "$HL_KEY" ]; then
  cat <<'EOF'
  The bot trades on Hyperliquid TESTNET: test money, real exchange. Create a
  testnet account at https://app.hyperliquid-testnet.xyz, claim test USDC from
  its faucet, and export the account's private key.

  Give the bot an account of its own -- a fresh one, or a subaccount. Every round
  it checks that the position adds up to its own orders; a manual position or
  another bot on the same coin puts the grid on hold.

EOF
  printf '  Private key (input stays hidden): '
  read -rs HL_KEY
  printf '\n'
  printf '  Subaccount address to trade on (empty: the key'"'"'s own account): '
  read -r HL_WALLET
fi

if [ "$REUSE_SECRET" = 0 ]; then
  [ -n "$HL_KEY" ] || die "no key given"
  case "$HL_KEY" in
    0x*) : ;;
    *)   HL_KEY="0x$HL_KEY" ;;
  esac
  if ! printf '%s' "$HL_KEY" | grep -qE '^0x[0-9a-fA-F]{64}$'; then
    die "that does not look like a private key (expected 0x followed by 64 hex characters)"
  fi
  if [ -n "$HL_WALLET" ] && ! printf '%s' "$HL_WALLET" | grep -qE '^0x[0-9a-fA-F]{40}$'; then
    die "that does not look like an address (expected 0x followed by 40 hex characters)"
  fi
  ok "key received${HL_WALLET:+, trading on subaccount $HL_WALLET}"
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
step "Secret $HL_SECRET"

create_secret() {
  # Through apply, so running again does not fail with "already exists".
  #
  # The key never becomes a command-line argument: printf is a shell builtin, not
  # a process, and base64 reads it from stdin, so it does not show up in ps.
  if [ "$DRY_RUN" = 1 ]; then
    printf '       would run: kubectl apply -f - (Secret %s/%s, private_key=<hidden>)\n' \
      "$NAMESPACE" "$HL_SECRET"
    return
  fi
  local key_b64 wallet_b64
  key_b64="$(printf '%s' "$HL_KEY" | base64 | tr -d '\n')"
  wallet_b64="$(printf '%s' "$HL_WALLET" | base64 | tr -d '\n')"
  printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: %s\n  namespace: %s\ntype: Opaque\ndata:\n  private_key: %s\n  wallet_address: "%s"\n' \
    "$HL_SECRET" "$NAMESPACE" "$key_b64" "$wallet_b64" | kubectl apply -f - >/dev/null
}
if [ "$REUSE_SECRET" = 1 ]; then
  ok "left unchanged"
else
  create_secret
  if [ "$DRY_RUN" = 0 ]; then ok "created or updated"; fi
fi

# ── 5. Telegram (optional) ───────────────────────────────────────────────────
step "Telegram alerts (optional)"

TG_TOKEN="${GRIDSTATIC_TG_TOKEN:-}"
TG_CHAT="${GRIDSTATIC_TG_CHAT:-}"
TG_ENABLED=0
TG_REUSE=0

# The URL carries the token, so curl reads it from stdin: a here-string is a shell
# builtin, and the token never becomes an argument that shows up in ps.
tg_api() { curl -fsS --max-time 20 --config - <<<"url = \"https://api.telegram.org/bot${TG_TOKEN}/$1\""; }

if [ -n "$TG_TOKEN" ]; then
  TG_ENABLED=1
elif [ "$DRY_RUN" = 0 ] && command -v kubectl >/dev/null 2>&1 \
     && kubectl -n "$NAMESPACE" get secret "$TG_SECRET" >/dev/null 2>&1; then
  ok "Secret $TG_SECRET already exists -- alerts stay on"
  TG_ENABLED=1
  TG_REUSE=1
elif [ -t 0 ]; then
  cat <<'EOF'
  Alerts when a buy fills, a cycle closes, a grid holds or the price leaves its
  range -- plus a /status command. It needs a Telegram bot of its own: create one
  with @BotFather (/newbot). Do not reuse the token of a bot that something else
  already listens on: Telegram allows one listener per token.

EOF
  printf '  Set up Telegram alerts? [y/N]: '
  read -r answer
  case "$answer" in
    y|Y|yes|j|J)
      printf '  Bot token (input stays hidden): '
      read -rs TG_TOKEN
      printf '\n'
      TG_ENABLED=1
      ;;
    *) ok "skipped -- 'Telegram alerts' in the README adds it later" ;;
  esac
else
  # No terminal (CI, a pipe): never wait for an answer that cannot come.
  ok "skipped (no terminal; set GRIDSTATIC_TG_TOKEN to include it)"
fi

if [ "$TG_ENABLED" = 1 ] && [ "$TG_REUSE" = 0 ]; then
  [ -n "$TG_TOKEN" ] || die "no bot token given"
  if [ "$DRY_RUN" = 1 ]; then
    say "       would check the token with Telegram and look up your chat id"
    printf '       would run: kubectl apply -f - (Secret %s/%s, bot_token=<hidden>)\n' \
      "$NAMESPACE" "$TG_SECRET"
  else
    me="$(tg_api getMe 2>/dev/null)" || die "Telegram does not accept this token -- copy it again from @BotFather"
    bot="$(printf '%s' "$me" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')"
    ok "token works: @$bot"
    if [ -z "$TG_CHAT" ]; then
      [ -t 0 ] || die "no terminal to look up the chat id -- set GRIDSTATIC_TG_CHAT"
      say "  Send any message to @$bot in Telegram, then press Enter."
      read -r _
      TG_CHAT="$(tg_api getUpdates | grep -oE '"chat":\{"id":-?[0-9]+' | head -1 | grep -oE -- '-?[0-9]+$' || true)"
      if [ -z "$TG_CHAT" ]; then
        printf '  No message found. Chat id to send alerts to: '
        read -r TG_CHAT
      fi
    fi
    printf '%s' "$TG_CHAT" | grep -qE '^-?[0-9]+$' \
      || die "that is not a chat id (a number; a group's starts with -)"
    tg_api "sendMessage?chat_id=${TG_CHAT}&text=gridstatic%20is%20connected%20--%20alerts%20will%20arrive%20here." >/dev/null \
      || die "could not send to chat $TG_CHAT -- write to @$bot from that chat first"
    ok "test message sent to chat $TG_CHAT"
    printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: %s\n  namespace: %s\ntype: Opaque\ndata:\n  bot_token: %s\n  chat_id: %s\n' \
      "$TG_SECRET" "$NAMESPACE" "$(printf '%s' "$TG_TOKEN" | base64 | tr -d '\n')" \
      "$(printf '%s' "$TG_CHAT" | base64 | tr -d '\n')" | kubectl apply -f - >/dev/null
    ok "Secret $TG_SECRET created or updated"
  fi
fi
unset TG_TOKEN

TG_VALUES=""
if [ "$TG_ENABLED" = 1 ]; then
  TG_VALUES=$'telegram:\n  enabled: true\n  existingSecret: '"$TG_SECRET"$'\n'
fi

# ── 6. Values file ───────────────────────────────────────────────────────────
step "Values file $VALUES_FILE"

if [ -f "$VALUES_FILE" ]; then
  if [ "$TG_ENABLED" = 1 ] && ! grep -q '^telegram:' "$VALUES_FILE"; then
    if [ "$DRY_RUN" = 1 ]; then
      printf '       would add the telegram block to %s\n' "$VALUES_FILE"
    else
      printf '\n%s' "$TG_VALUES" >> "$VALUES_FILE"
      ok "already exists -- telegram block added"
    fi
  else
    ok "already exists -- left unchanged"
  fi
else
  if [ "$DRY_RUN" = 1 ]; then
    printf '       would write: %s\n' "$VALUES_FILE"
  else
    cat > "$VALUES_FILE" <<EOF
# Created by install.sh. There is no key in here: it lives in the Secret
# $HL_SECRET. You can keep this file and put it under version control.
hyperliquid:
  # true: every order goes to testnet. See 'Going to mainnet' in the README
  # before you change this.
  testnet: true
  existingSecret: $HL_SECRET

${TG_VALUES}
grid:
  # Fixed size per line: startBalance x 80% / lines. With 20 lines, 1000 means
  # \$40 per line. Hyperliquid refuses orders under \$10.
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
EOF
    ok "written -- check the grid bounds against the current testnet price"
  fi
fi

# ── 7. Install ───────────────────────────────────────────────────────────────
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

# ── 8. Verify ────────────────────────────────────────────────────────────────
step "Checking that it works"

say "  waiting for the pods to become ready..."
deployments=(deployment/"$RELEASE"-connector deployment/"$RELEASE"-grid-static)
if [ "$TG_ENABLED" = 1 ]; then deployments+=(deployment/"$RELEASE"-alerter); fi
if ! kubectl -n "$NAMESPACE" wait --for=condition=available --timeout=180s \
     "${deployments[@]}" >/dev/null 2>&1; then
  warn "not every pod was ready within three minutes"
fi

problems=0

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
say "  waiting for the bot to start its grid..."
bot_log=""
for _ in $(seq 1 30); do
  bot_log="$(kubectl -n "$NAMESPACE" logs deploy/"$RELEASE"-grid-static --tail=80 2>/dev/null || true)"
  if printf '%s' "$bot_log" | grep -qE "\] started: |Refusing to start"; then
    break
  fi
  sleep 3
done

if printf '%s' "$bot_log" | grep -q "Refusing to start"; then
  err "the bot refuses this configuration:"
  printf '%s\n' "$bot_log" | sed -n '/Refusing to start/,$p' | head -6 | sed 's/^/       /'
  problems=1
elif printf '%s' "$bot_log" | grep -q "\] started: "; then
  ok "$(printf '%s' "$bot_log" | grep -o '\] started: .*' | head -1 | sed 's/^\] //')"
else
  err "the bot did not start a grid within 90 seconds"
  last_lines="$(printf '%s' "$bot_log" | tail -3)"
  if [ -n "$last_lines" ]; then
    printf '%s\n' "$last_lines" | sed 's/^/       /'
  fi
  problems=1
fi

if [ "$problems" = 0 ]; then
  say "  watching the first rounds (35 seconds)..."
  sleep 35
  recent="$(kubectl -n "$NAMESPACE" logs deploy/"$RELEASE"-grid-static --since=40s 2>/dev/null || true)"
  round_error="$(printf '%s' "$recent" | grep -E "round skipped|round failed|start failed" | head -1 || true)"
  hold="$(printf '%s' "$recent" | grep "on hold:" | head -1 || true)"
  if [ -n "$round_error" ]; then
    err "the bot cannot work with the exchange:"
    say "       $round_error"
    problems=1
  elif [ -n "$hold" ]; then
    warn "the grid is on hold -- it places no new buys:"
    say "       ${hold#*on hold: }"
  else
    ok "rounds run cleanly"
  fi
fi

# ── Verdict ──────────────────────────────────────────────────────────────────
if [ "$problems" = 0 ]; then
  step "${green}Done${reset} -- your grid runs on testnet"
  cat <<EOF

  Follow along:
    kubectl -n $NAMESPACE logs deploy/$RELEASE-grid-static -f

  See every cell:
    kubectl -n $NAMESPACE port-forward svc/$RELEASE-grid-static 8080:8080
    curl -s localhost:8080/status$( [ "$TG_ENABLED" = 1 ] && printf '
    or send /status to your Telegram bot' )

  Change your grids: edit $VALUES_FILE, then run
    helm upgrade $RELEASE $CHART -n $NAMESPACE -f $VALUES_FILE

  Trading with real money: see 'Going to mainnet' in the README. That step is
  manual on purpose -- it is not something you want to do quickly.
EOF
else
  step "${red}Something is wrong${reset}"
  cat <<EOF

  The problems are listed above. To see more:
    kubectl -n $NAMESPACE get pods
    kubectl -n $NAMESPACE logs deploy/$RELEASE-connector --tail=50
    kubectl -n $NAMESPACE logs deploy/$RELEASE-grid-static --tail=50

  On testnet, starting over is safe:
    helm uninstall $RELEASE -n $NAMESPACE
  Orders the bot placed stay on the exchange until you cancel them there.
EOF
  exit 1
fi
