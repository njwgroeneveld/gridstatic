#!/usr/bin/env bash
#
# gridstatic — laat één grid met echt geld handelen.
#
#   bash golive.sh
#
# Dit is met opzet een apart script. install.sh laat alles in shadow draaien; dit
# script is de bewuste stap naar de beurs. Het bewerkt je waardenbestand niet, maar
# schrijft een tweede bestand (gridstatic-live.yaml) met alleen de live-instellingen.
# Terug naar shadow is daarmee: dat bestand weggooien en opnieuw upgraden.

set -euo pipefail

NAMESPACE="${GRIDSTATIC_NAMESPACE:-gridstatic}"
RELEASE="${GRIDSTATIC_RELEASE:-gridstatic}"
CHART="${GRIDSTATIC_CHART:-oci://ghcr.io/njwgroeneveld/charts/gridstatic}"
VALUES_FILE="gridstatic-values.yaml"
LIVE_FILE="gridstatic-live.yaml"
HL_SECRET="gridstatic-hl"
TG_SECRET="gridstatic-telegram"
DRY_RUN=0

rood=$'\033[31m'; groen=$'\033[32m'; geel=$'\033[33m'; vet=$'\033[1m'; uit=$'\033[0m'
zeg()   { printf '%s\n' "$*"; }
stap()  { printf '\n%s==>%s %s\n' "$vet" "$uit" "$*"; }
goed()  { printf '%s  ok%s  %s\n' "$groen" "$uit" "$*"; }
let_op(){ printf '%s  let op%s  %s\n' "$geel" "$uit" "$*"; }
fout()  { printf '%s  fout%s  %s\n' "$rood" "$uit" "$*" >&2; }
stop()  { fout "$*"; exit 1; }

draai() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '       zou draaien: %s\n' "$*"
  else
    "$@"
  fi
}

gebruik() {
  cat <<'EOF'
één grid live zetten

  bash golive.sh [opties]

Opties
  --namespace NAAM   standaard: gridstatic
  --release NAAM     standaard: gridstatic
  --chart REF        standaard: oci://ghcr.io/njwgroeneveld/charts/gridstatic
  --dry-run          toon wat er zou gebeuren, raak niets aan
  --help             deze tekst

Omgevingsvariabelen (slaan de bijbehorende vraag over)
  GRIDSTATIC_HL_KEY      private key van je Hyperliquid agent-wallet
  GRIDSTATIC_HL_WALLET   walletadres
  GRIDSTATIC_TESTNET     true of false
  GRIDSTATIC_TG_TOKEN    Telegram bot-token (optioneel)
  GRIDSTATIC_TG_CHAT     Telegram chat-id (optioneel)
  GRIDSTATIC_GRID        naam van de grid die live gaat

Terug naar shadow:
  rm gridstatic-live.yaml
  helm upgrade <release> <chart> -n <namespace> -f gridstatic-values.yaml
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --release)   RELEASE="$2";   shift 2 ;;
    --chart)     CHART="$2";     shift 2 ;;
    --dry-run)   DRY_RUN=1;      shift ;;
    --help|-h)   gebruik; exit 0 ;;
    *)           stop "onbekende optie: $1 (probeer --help)" ;;
  esac
done

# ── 1. Controleren ───────────────────────────────────────────────────────────
stap "Controleren"

for gereedschap in kubectl helm; do
  if ! command -v "$gereedschap" >/dev/null 2>&1; then
    if [ "$DRY_RUN" = 1 ]; then
      let_op "$gereedschap ontbreekt — dry-run gaat toch verder"
    else
      stop "$gereedschap ontbreekt"
    fi
  fi
done
[ -f "$VALUES_FILE" ] || stop "$VALUES_FILE niet gevonden — draai eerst install.sh in deze map"

if [ "$DRY_RUN" = 0 ] && ! helm status "$RELEASE" -n "$NAMESPACE" >/dev/null 2>&1; then
  stop "release $RELEASE draait niet in namespace $NAMESPACE — draai eerst install.sh"
fi
goed "release $RELEASE gevonden, waarden uit $VALUES_FILE"

# ── 2. Welke grid ────────────────────────────────────────────────────────────
stap "Welke grid gaat live"

# De grids staan onder 'coins:' met twee niveaus inspringing. Dit leest alleen de
# namen; de details halen we hieronder per grid op.
grids="$(awk '/^  coins:/{f=1;next} f && /^    [A-Za-z0-9_.-]+:[[:space:]]*$/{gsub(/[[:space:]:]/,"");print} f && /^[^ ]/{f=0}' "$VALUES_FILE")"
[ -n "$grids" ] || stop "geen grids gevonden in $VALUES_FILE"

veld() { # veld <gridnaam> <veldnaam>
  awk -v grid="$1" -v veld="$2" '
    $0 ~ "^    " grid ":[[:space:]]*$" {f=1; next}
    f && /^    [A-Za-z0-9_.-]+:[[:space:]]*$/ {f=0}
    f && $1 == veld":" {print $2; exit}
  ' "$VALUES_FILE"
}

GRID="${GRIDSTATIC_GRID:-}"
if [ -z "$GRID" ]; then
  zeg "  Beschikbaar in $VALUES_FILE:"
  printf '%s\n' "$grids" | while read -r g; do
    printf '    %-12s %s, %s lijnen, hefboom %s, shadow: %s\n' \
      "$g" "$(veld "$g" coin)" "$(veld "$g" numLines)" "$(veld "$g" leverage)" "$(veld "$g" shadow)"
  done
  printf '\n  Welke grid gaat live? '
  read -r GRID
fi
printf '%s\n' "$grids" | grep -qx "$GRID" || stop "grid '$GRID' staat niet in $VALUES_FILE"

coin="$(veld "$GRID" coin)"
lijnen="$(veld "$GRID" numLines)"
hefboom="$(veld "$GRID" leverage)"
alloc="$(veld "$GRID" allocationPct)"
was_shadow="$(veld "$GRID" shadow)"
start_balance="$(awk '/^  startBalance:/{print $2; exit}' "$VALUES_FILE")"
strategie_pct="$(awk '/^  strategyAllocationPct:/{print $2; exit}' "$VALUES_FILE")"
[ -n "$start_balance" ] || start_balance=1000
[ -n "$strategie_pct" ] || strategie_pct=80
per_lijn="$(awk -v b="$start_balance" -v s="$strategie_pct" -v a="${alloc:-100}" -v n="${lijnen:-1}" \
            'BEGIN{printf "%.2f", (b*s/100*a/100)/n}')"

# ── 3. Sleutels ──────────────────────────────────────────────────────────────
stap "Je Hyperliquid-sleutel"

HL_KEY="${GRIDSTATIC_HL_KEY:-}"
HL_WALLET="${GRIDSTATIC_HL_WALLET:-}"
if [ -z "$HL_KEY" ]; then
  zeg "  Gebruik een agent- of API-wallet met beperkt saldo, niet je hoofdaccount."
  printf '  Private key (verborgen): '
  read -rs HL_KEY
  printf '\n'
fi
[ -n "$HL_KEY" ] || stop "geen sleutel opgegeven"
if [ -z "$HL_WALLET" ]; then
  printf '  Walletadres (0x...): '
  read -r HL_WALLET
fi
[ -n "$HL_WALLET" ] || stop "geen walletadres opgegeven"

TESTNET="${GRIDSTATIC_TESTNET:-}"
if [ -z "$TESTNET" ]; then
  printf '  Testnet of mainnet? [testnet/mainnet]: '
  read -r antwoord
  case "$antwoord" in
    mainnet) TESTNET=false ;;
    *)       TESTNET=true ;;
  esac
fi

TG_TOKEN="${GRIDSTATIC_TG_TOKEN:-}"
TG_CHAT="${GRIDSTATIC_TG_CHAT:-}"
if [ -z "$TG_TOKEN" ] && [ -t 0 ]; then
  printf '  Telegram-meldingen instellen? [j/N]: '
  read -r antwoord
  if [ "$antwoord" = "j" ] || [ "$antwoord" = "J" ]; then
    printf '  Bot-token (verborgen): '
    read -rs TG_TOKEN
    printf '\n  Chat-id: '
    read -r TG_CHAT
  fi
fi

# ── 4. Bevestigen ────────────────────────────────────────────────────────────
stap "Wat er gaat gebeuren"

netwerk="Hyperliquid testnet"
[ "$TESTNET" = "false" ] && netwerk="${rood}Hyperliquid MAINNET — echt geld${uit}"

cat <<EOF

  grid           $GRID  ($coin, $lijnen lijnen, hefboom $hefboom)
  netwerk        $netwerk
  inleg          \$$per_lijn per lijn, dus maximaal \$$(awk -v p="$per_lijn" -v n="${lijnen:-1}" 'BEGIN{printf "%.2f", p*n}') in de markt
  telegram       $([ -n "$TG_TOKEN" ] && echo "aan" || echo "uit")

EOF

if [ "$was_shadow" = "true" ]; then
  let_op "deze grid heeft al een shadow-administratie in je database"
  zeg "       De bot herkent zijn bestaande config aan coin, lijnen, grenzen en hefboom,"
  zeg "       dus hij gaat verder in diezelfde rij: simulatie en echte trades komen dan"
  zeg "       onder één config te staan. Wil je ze gescheiden houden, geef de live-grid"
  zeg "       dan andere grenzen of een ander aantal lijnen."
  zeg ""
fi

if [ "$DRY_RUN" = 0 ]; then
  printf '  Typ de naam van de grid om te bevestigen (%s): ' "$GRID"
  read -r bevestiging
  [ "$bevestiging" = "$GRID" ] || stop "niet bevestigd — er is niets veranderd"
fi

# ── 5. Secrets ───────────────────────────────────────────────────────────────
stap "Secrets"

if [ "$DRY_RUN" = 1 ]; then
  printf '       zou draaien: kubectl -n %s create secret generic %s --from-literal=private_key=<verborgen> --from-literal=wallet_address=<verborgen> | kubectl apply -f -\n' "$NAMESPACE" "$HL_SECRET"
else
  kubectl -n "$NAMESPACE" create secret generic "$HL_SECRET" \
    --from-literal=private_key="$HL_KEY" \
    --from-literal=wallet_address="$HL_WALLET" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  goed "$HL_SECRET aangemaakt of bijgewerkt"
fi

if [ -n "$TG_TOKEN" ]; then
  if [ "$DRY_RUN" = 1 ]; then
    printf '       zou draaien: kubectl -n %s create secret generic %s --from-literal=bot_token=<verborgen> --from-literal=chat_id=<verborgen> | kubectl apply -f -\n' "$NAMESPACE" "$TG_SECRET"
  else
    kubectl -n "$NAMESPACE" create secret generic "$TG_SECRET" \
      --from-literal=bot_token="$TG_TOKEN" \
      --from-literal=chat_id="$TG_CHAT" \
      --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    goed "$TG_SECRET aangemaakt of bijgewerkt"
  fi
fi

# ── 6. Live-waarden ──────────────────────────────────────────────────────────
stap "Waardenbestand $LIVE_FILE"

live_inhoud="$(cat <<EOF
# Aangemaakt door golive.sh. Alleen wat afwijkt van $VALUES_FILE staat hier.
# Weggooien en opnieuw upgraden zet alles terug in shadow.
hyperliquid:
  testnet: $TESTNET
  existingSecret: $HL_SECRET
$([ -n "$TG_TOKEN" ] && printf 'telegram:\n  enabled: true\n  existingSecret: %s\n' "$TG_SECRET")
grid:
  coins:
    $GRID:
      shadow: false
EOF
)"

if [ "$DRY_RUN" = 1 ]; then
  printf '       zou schrijven: %s\n\n' "$LIVE_FILE"
  printf '%s\n' "$live_inhoud" | sed 's/^/       /'
else
  printf '%s\n' "$live_inhoud" > "$LIVE_FILE"
  goed "geschreven"
fi

# ── 7. Uitrollen ─────────────────────────────────────────────────────────────
stap "Uitrollen"

draai helm upgrade "$RELEASE" "$CHART" -n "$NAMESPACE" -f "$VALUES_FILE" -f "$LIVE_FILE"

if [ "$DRY_RUN" = 1 ]; then
  stap "Dry-run klaar"
  zeg "  Er is niets aangeraakt en er is geen secret aangemaakt."
  exit 0
fi

zeg "  wachten tot de nieuwe pod draait..."
kubectl -n "$NAMESPACE" rollout status deployment/"$RELEASE"-grid-static --timeout=120s >/dev/null 2>&1 || \
  let_op "de pod was niet binnen twee minuten klaar"

sleep 5
bot_log="$(kubectl -n "$NAMESPACE" logs deploy/"$RELEASE"-grid-static --tail=60 2>/dev/null || true)"
if printf '%s' "$bot_log" | grep -q "shadow=False"; then
  goed "de grid draait live"
else
  let_op "kon 'shadow=False' niet in de log vinden — kijk zelf mee"
fi

stap "Klaar"
cat <<EOF

  Meekijken:
    kubectl -n $NAMESPACE logs deploy/$RELEASE-grid-static -f

  Terug naar shadow:
    rm $LIVE_FILE
    helm upgrade $RELEASE $CHART -n $NAMESPACE -f $VALUES_FILE

  Controleer de eerste orders ook op Hyperliquid zelf. Wat de bot denkt te hebben
  en wat er op de beurs staat, hoort gelijk te zijn.
EOF
