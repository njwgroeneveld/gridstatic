#!/usr/bin/env bash
#
# gridstatic — laat één grid met echt geld handelen.
#
#   bash golive.sh
#
# Dit is met opzet een apart script. install.sh laat alles in shadow draaien; dit
# script is de bewuste stap naar de beurs.
#
# Het past je gridstatic-values.yaml aan -- één bestand, zodat je bij elke volgende
# helm upgrade maar één -f hoeft mee te geven. Voor de zekerheid: er komt eerst een
# kopie (.bak), de bewerking blijft binnen het blok van de gekozen grid, en je krijgt
# de diff te zien voordat je bevestigt.

set -euo pipefail

NAMESPACE="${GRIDSTATIC_NAMESPACE:-gridstatic}"
RELEASE="${GRIDSTATIC_RELEASE:-gridstatic}"
CHART="${GRIDSTATIC_CHART:-oci://ghcr.io/njwgroeneveld/charts/gridstatic}"
VALUES_FILE="gridstatic-values.yaml"
BACKUP_FILE="gridstatic-values.yaml.bak"
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

Terug naar shadow: zet shadow terug op true in gridstatic-values.yaml (of herstel
gridstatic-values.yaml.bak) en draai helm upgrade opnieuw.
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

# ── 4. De wijziging opstellen ────────────────────────────────────────────────
stap "Wat er verandert in $VALUES_FILE"

nieuwe_waarden() {
  # Twee bewerkingen, allebei bewust smal gehouden:
  #  1. binnen het blok van de gekozen grid gaat shadow op false -- de lus stopt bij
  #     het volgende grid, dus een andere grid met shadow: true blijft ongemoeid;
  #  2. bestaande hyperliquid:- en telegram:-blokken worden verwijderd en hieronder
  #     opnieuw geschreven, zodat opnieuw draaien hetzelfde resultaat geeft.
  awk -v grid="$GRID" '
    /^(hyperliquid|telegram):[[:space:]]*$/ { skip=1; next }
    skip && /^[A-Za-z#]/ { skip=0 }
    skip { next }
    $0 ~ "^    " grid ":[[:space:]]*$" { ingrid=1; print; next }
    ingrid && /^    [A-Za-z0-9_.-]+:[[:space:]]*$/ { ingrid=0 }
    ingrid && /^      shadow:/ { sub(/shadow:.*/, "shadow: false"); print; next }
    { print }
  ' "$VALUES_FILE"

  printf '
hyperliquid:
  testnet: %s
  existingSecret: %s
' "$TESTNET" "$HL_SECRET"
  if [ -n "$TG_TOKEN" ]; then
    printf '
telegram:
  enabled: true
  existingSecret: %s
' "$TG_SECRET"
  fi
}

nieuw_bestand="$(mktemp)"
trap 'rm -f "$nieuw_bestand"' EXIT
nieuwe_waarden > "$nieuw_bestand"

if command -v diff >/dev/null 2>&1; then
  diff -u "$VALUES_FILE" "$nieuw_bestand" | sed 's/^/  /' || true
else
  zeg "  (diff niet beschikbaar; nieuwe inhoud:)"
  sed 's/^/  /' "$nieuw_bestand"
fi

if ! grep -q "shadow: false" "$nieuw_bestand"; then
  stop "de bewerking heeft geen shadow: false opgeleverd — controleer $VALUES_FILE met de hand"
fi

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

if [ "$DRY_RUN" = 1 ]; then
  stap "Dry-run klaar"
  zeg "  Er is niets aangeraakt, geen secret aangemaakt en $VALUES_FILE is ongewijzigd."
  exit 0
fi

printf '  Typ de naam van de grid om te bevestigen (%s): ' "$GRID"
read -r bevestiging
[ "$bevestiging" = "$GRID" ] || stop "niet bevestigd — er is niets veranderd"

# ── 5. Secrets ───────────────────────────────────────────────────────────────
stap "Secrets"

kubectl -n "$NAMESPACE" create secret generic "$HL_SECRET"   --from-literal=private_key="$HL_KEY"   --from-literal=wallet_address="$HL_WALLET"   --dry-run=client -o yaml | kubectl apply -f - >/dev/null
goed "$HL_SECRET aangemaakt of bijgewerkt"

if [ -n "$TG_TOKEN" ]; then
  kubectl -n "$NAMESPACE" create secret generic "$TG_SECRET"     --from-literal=bot_token="$TG_TOKEN"     --from-literal=chat_id="$TG_CHAT"     --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  goed "$TG_SECRET aangemaakt of bijgewerkt"
fi

# ── 6. Waardenbestand ────────────────────────────────────────────────────────
stap "Waardenbestand bijwerken"

cp "$VALUES_FILE" "$BACKUP_FILE"
cp "$nieuw_bestand" "$VALUES_FILE"
goed "$VALUES_FILE bijgewerkt (kopie in $BACKUP_FILE)"

# ── 7. Uitrollen ─────────────────────────────────────────────────────────────
stap "Uitrollen"

helm upgrade "$RELEASE" "$CHART" -n "$NAMESPACE" -f "$VALUES_FILE"

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

  Terug naar shadow: zet shadow terug op true in $VALUES_FILE (of herstel
  $BACKUP_FILE) en draai:
    helm upgrade $RELEASE $CHART -n $NAMESPACE -f $VALUES_FILE

  Controleer de eerste orders ook op Hyperliquid zelf. Wat de bot denkt te hebben
  en wat er op de beurs staat, hoort gelijk te zijn.
EOF
