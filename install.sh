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
# docs/operations.md, deliberately not a script.

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
  GRIDSTATIC_HL_KEY      private key: an API wallet's, or the account's own (0x + 64 hex)
  GRIDSTATIC_HL_WALLET   account to trade on: the API wallet's account or a subaccount;
                         empty = the key's own account
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

# ── 2. Namespace ─────────────────────────────────────────────────────────────
# Before the key: the key check below runs as a pod in this namespace.
step "Namespace $NAMESPACE"

if [ "$DRY_RUN" = 0 ] && kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
  ok "already exists"
else
  run kubectl create namespace "$NAMESPACE"
  if [ "$DRY_RUN" = 0 ]; then ok "created"; fi
fi

# ── 3. Hyperliquid key ───────────────────────────────────────────────────────
step "Your Hyperliquid testnet key"

HL_KEY="${GRIDSTATIC_HL_KEY:-}"
HL_WALLET="${GRIDSTATIC_HL_WALLET:-}"

explain_key() {
  cat <<'EOF'
  The bot trades on Hyperliquid TESTNET: test money, real exchange. You need an
  account at https://app.hyperliquid-testnet.xyz with test USDC from its faucet.
  Two questions follow: a private key, and the address of the account to trade on.

  Recommended: an API wallet. Create one on the site's API page and approve it
  with your account. It can trade, but it can never withdraw.
    private key  ->  the API wallet's private key (shown once, when you create it)
    address      ->  the account it trades for: your account, or a subaccount.
                     NOT the API wallet's own address.

  Or the account's own key. It works, but a leaked key could withdraw everything.
    private key  ->  exported from your wallet app
    address      ->  empty, or the address of one of its subaccounts

  Give the bot an account of its own -- a fresh one, or a subaccount. Every round
  it checks that the position adds up to its own orders; a manual position or
  another bot on the same coin puts the grid on hold.

EOF
}

ask_key() {
  printf '  Private key (input stays hidden): '
  read -rs HL_KEY
  printf '\n'
  printf '  Address of the account to trade on (empty: the key'"'"'s own account): '
  read -r HL_WALLET
}

# Format only: whether the key and the address belong together is for check_key.
key_format_ok() {
  [ -n "$HL_KEY" ] || { err "no key given"; return 1; }
  case "$HL_KEY" in
    0x*) : ;;
    *)   HL_KEY="0x$HL_KEY" ;;
  esac
  if ! printf '%s' "$HL_KEY" | grep -qE '^0x[0-9a-fA-F]{64}$'; then
    err "that does not look like a private key (expected 0x followed by 64 hex characters)"
    return 1
  fi
  if [ -n "$HL_WALLET" ] && ! printf '%s' "$HL_WALLET" | grep -qE '^0x[0-9a-fA-F]{40}$'; then
    err "that does not look like an address (expected 0x followed by 40 hex characters)"
    return 1
  fi
}

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
# The key check. It runs in a short-lived pod from the connector's own image, which
# has the library that turns a private key into its address, and reads the key from
# the Secret -- exactly what the connector will read. It only asks Hyperliquid's
# public info endpoint about addresses; the key itself never leaves the pod.
# Output: one "kind<TAB>message" per line, and a last line "RESULT ok|wrong|unknown".
read -r -d '' KEYCHECK_PY <<'PY' || true
import json, os, urllib.request

API = "https://api.hyperliquid-testnet.xyz/info"

def say(kind, msg):
    print(f"{kind}\t{msg}", flush=True)

def info(body):
    req = urllib.request.Request(API, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)

def role(addr):
    r = info({"type": "userRole", "user": addr})
    return r.get("role"), (r.get("data") or {}).get("user", "").lower()

def value(addr):
    perp = float(info({"type": "clearinghouseState", "user": addr})["marginSummary"]["accountValue"])
    spot = info({"type": "spotClearinghouseState", "user": addr})["balances"]
    return perp + sum(float(b["total"]) for b in spot if b["coin"] == "USDC")

def subaccounts(main):
    return {s["subAccountUser"].lower(): s for s in (info({"type": "subAccounts", "user": main}) or [])}

def show_accounts(main):
    say("hint", "Accounts you can trade on with this key:")
    say("hint", f"  {main}  {'your account':<26} ${value(main):,.2f}")
    for addr, s in subaccounts(main).items():
        v = float(s["clearinghouseState"]["marginSummary"]["accountValue"])
        say("hint", f"  {addr}  {'subaccount ' + s['name']:<26} ${v:,.2f}")

def wrong(msg, main=None):
    say("error", msg)
    if main:
        show_accounts(main)
    print("RESULT wrong")
    raise SystemExit

def main():
    from eth_account import Account
    signer = Account.from_key(os.environ["HL_PRIVATE_KEY"].strip()).address.lower()
    wallet = os.environ.get("WALLET_ADDRESS", "").strip().lower()
    srole, owner = role(signer)

    if srole == "missing":
        wrong(f"Hyperliquid testnet does not know this key (it belongs to {signer}). "
              "An API wallet made on mainnet does not exist on testnet, and an account "
              "only exists once it has received test USDC.")
    if wallet == signer:
        wrong("That address is the key's own address: leave the address empty for that.")

    if srole == "agent":
        if not wallet:
            wrong("This is the key of an API wallet. Give the address of the account it "
                  "trades for as well.", owner)
        if wallet == owner:
            account, what = owner, "an API wallet, trading for your account"
        elif wallet in subaccounts(owner):
            account, what = wallet, "an API wallet, trading for a subaccount"
        elif role(wallet)[0] == "agent":
            wrong(f"{wallet} is the address of an API wallet, not of an account. Give the "
                  "address of the account it trades for.", owner)
        else:
            wrong(f"This API wallet belongs to {owner}, and {wallet} is not that account "
                  "or one of its subaccounts.", owner)
    elif srole == "user":
        if not wallet:
            account, what = signer, "the account's own key"
        elif wallet in subaccounts(signer):
            account, what = wallet, "the account's own key, trading for a subaccount"
        elif role(wallet)[0] == "agent":
            wrong(f"{wallet} is the address of an API wallet, not of an account.", signer)
        else:
            wrong(f"{wallet} is not a subaccount of this key's account.", signer)
    else:
        wrong(f"Hyperliquid reports this key as {srole!r}; expected an account or an API wallet.")

    say("ok", f"key checked with Hyperliquid testnet: {what}")
    if srole == "user":
        say("warn", "this key can withdraw funds; an API wallet cannot (docs/operations.md, 'Which key')")
    usd = value(account)
    if usd > 0:
        say("ok", f"${usd:,.2f} on {account}")
    else:
        say("warn", f"no USDC on {account} yet -- the bot cannot place orders without margin")
    print("RESULT ok")

try:
    main()
except SystemExit:
    pass
except Exception as e:
    say("warn", f"could not check the key: {type(e).__name__}: {e}")
    print("RESULT unknown")
PY

CHECK_IMAGE="${GRIDSTATIC_CHECK_IMAGE:-ghcr.io/njwgroeneveld/gridstatic-connector:latest}"
CHECK_POD="$RELEASE-keycheck"

# Sets CHECK_RESULT to ok, wrong or unknown. Never fails the install on its own: a
# check that cannot run (no image, no network) is a warning, not a verdict.
check_key() {
  local overrides out kind msg
  say "  checking the key with Hyperliquid testnet (the first time pulls an image)..."
  overrides="$(printf '{"spec":{"containers":[{"name":"%s","image":"%s","stdin":true,"stdinOnce":true,"command":["python","-"],"env":[{"name":"HL_PRIVATE_KEY","valueFrom":{"secretKeyRef":{"name":"%s","key":"private_key"}}},{"name":"WALLET_ADDRESS","valueFrom":{"secretKeyRef":{"name":"%s","key":"wallet_address"}}}]}]}}' \
    "$CHECK_POD" "$CHECK_IMAGE" "$HL_SECRET" "$HL_SECRET")"
  kubectl -n "$NAMESPACE" delete pod "$CHECK_POD" --ignore-not-found >/dev/null 2>&1 || true
  out="$(kubectl -n "$NAMESPACE" run "$CHECK_POD" --rm -i --quiet --restart=Never \
           --image="$CHECK_IMAGE" --pod-running-timeout=3m --overrides="$overrides" \
           <<<"$KEYCHECK_PY" 2>&1)" || true
  CHECK_RESULT="$(printf '%s\n' "$out" | sed -n 's/^RESULT \([a-z]*\)$/\1/p' | tail -1)"
  if [ -z "$CHECK_RESULT" ]; then
    CHECK_RESULT=unknown
    warn "could not run the key check -- continuing; the verification at the end still runs"
    printf '%s\n' "$out" | tail -3 | sed 's/^/       /'
    return
  fi
  while IFS=$'\t' read -r kind msg; do
    case "$kind" in
      ok)    ok "$msg" ;;
      warn)  warn "$msg" ;;
      error) err "$msg" ;;
      hint)  say "       $msg" ;;
    esac
  done <<<"$out"
}

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

if [ "$REUSE_SECRET" = 0 ] && [ -z "$HL_KEY" ]; then
  explain_key
  ask_key
fi

HL_CHANGED=0
TG_CHANGED=0

# Ask again on a wrong answer, as long as someone is there to answer: a key from the
# environment, without a terminal, fails at once.
attempt=1
while :; do
  if [ "$REUSE_SECRET" = 0 ]; then
    if ! key_format_ok; then
      if [ ! -t 0 ] || [ "$attempt" -ge 3 ]; then die "no usable key"; fi
      attempt=$((attempt + 1)); ask_key; continue
    fi
    create_secret
    if [ "$DRY_RUN" = 0 ]; then ok "Secret $HL_SECRET created or updated"; HL_CHANGED=1; fi
  else
    ok "reusing the existing Secret $HL_SECRET"
  fi

  if [ "$DRY_RUN" = 1 ]; then
    say "       would check the key with Hyperliquid testnet"
    break
  fi
  check_key
  [ "$CHECK_RESULT" = wrong ] || break

  if [ ! -t 0 ] || [ "$attempt" -ge 3 ]; then
    die "the key and the address do not work together -- see above"
  fi
  attempt=$((attempt + 1))
  REUSE_SECRET=0
  say ""
  say "  Try again (attempt $attempt of 3)."
  ask_key
done

# ── 4. Telegram (optional) ───────────────────────────────────────────────────
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
    *) ok "skipped -- 'Telegram alerts' in docs/operations.md adds it later" ;;
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
    TG_CHANGED=1
  fi
fi
unset TG_TOKEN

TG_VALUES=""
if [ "$TG_ENABLED" = 1 ]; then
  TG_VALUES=$'telegram:\n  enabled: true\n  existingSecret: '"$TG_SECRET"$'\n'
fi

# ── 5. Values file ───────────────────────────────────────────────────────────
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
  # true: every order goes to testnet. See 'Going to mainnet' in docs/operations.md
  # before you change this.
  testnet: true
  existingSecret: $HL_SECRET

${TG_VALUES}
grid:
  # Each grid uses allocationPct% of the realised equity on its dex, over its
  # lines, set every round. Hyperliquid refuses orders under \$10 (leverage counts).
  coins:
    BTC-20:
      coin: BTC
      active: true
      allocationPct: 90
      upper: 83000
      lower: 75000
      numLines: 20
      leverage: 1
EOF
    ok "written -- check the grid bounds against the current testnet price"
  fi
fi

# ── 6. Install ───────────────────────────────────────────────────────────────
step "Deploying the chart"

if [ "$DRY_RUN" = 0 ] && helm status "$RELEASE" -n "$NAMESPACE" >/dev/null 2>&1; then
  say "  release $RELEASE already exists -- upgrading"
  run helm upgrade "$RELEASE" "$CHART" -n "$NAMESPACE" -f "$VALUES_FILE"
  # A pod reads its Secret only when it starts, and helm upgrade restarts only pods
  # whose template changed. A new key would otherwise wait for the next restart.
  if [ "$HL_CHANGED" = 1 ]; then
    run kubectl -n "$NAMESPACE" rollout restart deployment/"$RELEASE"-connector >/dev/null
    ok "connector restarted to pick up the new key"
  fi
  if [ "$TG_CHANGED" = 1 ] && kubectl -n "$NAMESPACE" get deployment "$RELEASE"-alerter >/dev/null 2>&1; then
    run kubectl -n "$NAMESPACE" rollout restart deployment/"$RELEASE"-alerter >/dev/null
    ok "alerter restarted to pick up the new bot token"
  fi
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

  Trading with real money: see 'Going to mainnet' in docs/operations.md. That step is
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
