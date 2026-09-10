#!/usr/bin/env bash
#
# gridstatic — installeer de stack in shadow.
#
#   curl -fsSLO https://raw.githubusercontent.com/njwgroeneveld/gridstatic/master/install.sh
#   bash install.sh
#
# Eén vraag: je databaseverbinding. De rest heeft een standaard, en alles draait
# in shadow -- een simulatie, zonder orders naar de beurs. Live gaan gaat met de
# hand; dat staat in de README en is met opzet geen script.

set -euo pipefail

NAMESPACE="${GRIDSTATIC_NAMESPACE:-gridstatic}"
RELEASE="${GRIDSTATIC_RELEASE:-gridstatic}"
CHART="${GRIDSTATIC_CHART:-oci://ghcr.io/njwgroeneveld/charts/gridstatic}"
VALUES_FILE="gridstatic-values.yaml"
DB_SECRET="gridstatic-db"
DRY_RUN=0

rood=$'\033[31m'; groen=$'\033[32m'; geel=$'\033[33m'; vet=$'\033[1m'; uit=$'\033[0m'
zeg()   { printf '%s\n' "$*"; }
stap()  { printf '\n%s==>%s %s\n' "$vet" "$uit" "$*"; }
goed()  { printf '%s  ok%s  %s\n' "$groen" "$uit" "$*"; }
let_op(){ printf '%s  let op%s  %s\n' "$geel" "$uit" "$*"; }
fout()  { printf '%s  fout%s  %s\n' "$rood" "$uit" "$*" >&2; }
stop()  { fout "$*"; exit 1; }

# In dry-run tonen we wat we zouden draaien. Geheimen gaan nooit door deze functie:
# die worden via stdin doorgegeven, zie maak_secret.
draai() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '       zou draaien: %s\n' "$*"
  else
    "$@"
  fi
}

gebruik() {
  cat <<'EOF'
gridstatic installeren (shadow)

  bash install.sh [opties]

Opties
  --namespace NAAM   standaard: gridstatic
  --release NAAM     standaard: gridstatic
  --chart REF        standaard: oci://ghcr.io/njwgroeneveld/charts/gridstatic
                     (wijs naar ./chart om een lokale versie te testen)
  --dry-run          toon wat er zou gebeuren, raak niets aan
  --help             deze tekst

Omgevingsvariabelen (slaan de bijbehorende vraag over)
  GRIDSTATIC_DB_URL      connectiestring naar je Postgres/Supabase
  GRIDSTATIC_NAMESPACE   idem als --namespace
  GRIDSTATIC_RELEASE     idem als --release
  GRIDSTATIC_CHART       idem als --chart

De databaseverbinding krijgt bewust geen commandoregel-optie: die zou in je
shell-historie belanden en zichtbaar zijn in ps.
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
stap "Controleren wat er op deze machine staat"

ontbreekt=0
if ! command -v kubectl >/dev/null 2>&1; then
  fout "kubectl ontbreekt — https://kubernetes.io/docs/tasks/tools/"
  ontbreekt=1
else
  goed "kubectl gevonden"
fi

if ! command -v helm >/dev/null 2>&1; then
  fout "helm ontbreekt — curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash"
  ontbreekt=1
else
  goed "helm gevonden ($(helm version --short 2>/dev/null || echo onbekend))"
fi

if [ "$ontbreekt" = 1 ]; then
  if [ "$DRY_RUN" = 1 ]; then
    let_op "dry-run: ga toch verder"
  else
    stop "installeer het bovenstaande en probeer opnieuw"
  fi
elif ! kubectl cluster-info >/dev/null 2>&1; then
  if [ "$DRY_RUN" = 1 ]; then
    let_op "dry-run: geen cluster bereikbaar, ga toch verder"
  else
    stop "geen cluster bereikbaar — controleer je kubeconfig met 'kubectl cluster-info'"
  fi
else
  goed "cluster bereikbaar ($(kubectl config current-context))"
fi

# ── 2. Vragen ────────────────────────────────────────────────────────────────
stap "Je database"

DB_URL="${GRIDSTATIC_DB_URL:-}"

# Een herinstallatie is een gewoon geval: helm uninstall haalt alleen weg wat Helm
# zelf heeft aangemaakt, dus het secret staat er meestal nog. Dan hoef je je
# connectiestring niet opnieuw op te zoeken.
HERGEBRUIK=0
if [ -z "$DB_URL" ] && [ "$DRY_RUN" = 0 ] && command -v kubectl >/dev/null 2>&1    && kubectl -n "$NAMESPACE" get secret "$DB_SECRET" >/dev/null 2>&1; then
  zeg "  Er staat al een secret $DB_SECRET in namespace $NAMESPACE."
  printf '  Dat gebruiken? [J/n]: '
  read -r antwoord
  case "$antwoord" in
    n|N) : ;;
    *)   HERGEBRUIK=1 ;;
  esac
fi

if [ "$HERGEBRUIK" = 1 ]; then
  goed "bestaand secret hergebruikt — je connectiestring is niet opnieuw nodig"
elif [ -z "$DB_URL" ]; then
  cat <<'EOF'
  Je hebt een eigen PostgreSQL nodig; een gratis Supabase-project volstaat.
  Pak in het dashboard onder "Connect" de SESSION POOLER-string:

    postgresql://postgres.PROJECTREF:WACHTWOORD@aws-0-REGIO.pooler.supabase.com:5432/postgres?sslmode=require

  Niet de directe verbinding (db.<ref>.supabase.co): die bestaat alleen over IPv6
  en werkt dus niet op een IPv4-cluster.

  LET OP: gebruik geen database waarin al een gridstatic-stack draait. Twee bots
  op dezelfde grid_configs leggen dubbele orders.

EOF
  printf '  Connectiestring (invoer blijft verborgen): '
  read -rs DB_URL
  printf '\n'
fi

if [ "$HERGEBRUIK" = 0 ]; then
  [ -n "$DB_URL" ] || stop "geen connectiestring opgegeven"
  case "$DB_URL" in
    postgresql://*|postgres://*) : ;;
    *) stop "dat ziet er niet uit als een connectiestring (verwacht postgresql://...)" ;;
  esac
  case "$DB_URL" in
    *:6543/*) let_op "je gebruikt de transaction pooler (6543); de session pooler op 5432 past beter bij een langlopende service" ;;
  esac
  goed "connectiestring ontvangen (${#DB_URL} tekens)"
fi

# ── 3. Namespace ─────────────────────────────────────────────────────────────
stap "Namespace $NAMESPACE"

if [ "$DRY_RUN" = 0 ] && kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
  goed "bestaat al"
else
  draai kubectl create namespace "$NAMESPACE"
  [ "$DRY_RUN" = 0 ] && goed "aangemaakt"
fi

# ── 4. Secret ────────────────────────────────────────────────────────────────
stap "Secret $DB_SECRET"

maak_secret() {
  # Via apply, zodat opnieuw draaien geen "already exists" geeft. De waarde gaat
  # over een pijp en staat dus niet in de procestabel.
  if [ "$DRY_RUN" = 1 ]; then
    printf '       zou draaien: kubectl -n %s create secret generic %s --from-literal=url=<verborgen> | kubectl apply -f -\n' \
      "$NAMESPACE" "$DB_SECRET"
    return
  fi
  kubectl -n "$NAMESPACE" create secret generic "$DB_SECRET" \
    --from-literal=url="$DB_URL" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}
if [ "$HERGEBRUIK" = 1 ]; then
  goed "ongewijzigd gelaten"
else
  maak_secret
  [ "$DRY_RUN" = 0 ] && goed "aangemaakt of bijgewerkt"
fi

# ── 5. Waarden ───────────────────────────────────────────────────────────────
stap "Waardenbestand $VALUES_FILE"

if [ -f "$VALUES_FILE" ]; then
  goed "bestaat al — ongewijzigd gelaten"
else
  if [ "$DRY_RUN" = 1 ]; then
    printf '       zou schrijven: %s\n' "$VALUES_FILE"
  else
    cat > "$VALUES_FILE" <<EOF
# Aangemaakt door install.sh. Hier staat geen wachtwoord in: de verbinding zit in
# het Secret $DB_SECRET. Dit bestand mag je bewaren en in versiebeheer zetten.
database:
  existingSecret: $DB_SECRET

grid:
  # Rekenbasis voor de ordergrootte, ook live. Bij 20 lijnen en 80% allocatie is
  # 1000 gelijk aan \$40 per lijn.
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
    goed "geschreven"
  fi
fi

# ── 6. Installeren ───────────────────────────────────────────────────────────
stap "Chart uitrollen"

if [ "$DRY_RUN" = 0 ] && helm status "$RELEASE" -n "$NAMESPACE" >/dev/null 2>&1; then
  zeg "  release $RELEASE bestaat al — bijwerken"
  draai helm upgrade "$RELEASE" "$CHART" -n "$NAMESPACE" -f "$VALUES_FILE"
else
  draai helm install "$RELEASE" "$CHART" -n "$NAMESPACE" -f "$VALUES_FILE"
fi

if [ "$DRY_RUN" = 1 ]; then
  stap "Dry-run klaar"
  zeg "  Er is niets aangeraakt. Draai zonder --dry-run om het echt te doen."
  exit 0
fi
goed "uitgerold"

# ── 7. Controleren ───────────────────────────────────────────────────────────
stap "Controleren of het werkt"

zeg "  wachten tot de pods klaar zijn (de dal maakt eerst het schema aan)..."
if ! kubectl -n "$NAMESPACE" wait --for=condition=available --timeout=180s \
     deployment/"$RELEASE"-dal deployment/"$RELEASE"-grid-static >/dev/null 2>&1; then
  let_op "niet alle pods waren binnen drie minuten klaar"
fi

problemen=0

schema_log="$(kubectl -n "$NAMESPACE" logs deploy/"$RELEASE"-dal -c schema 2>/dev/null || true)"
if printf '%s' "$schema_log" | grep -q "CREATE TABLE"; then
  goed "schema aangebracht"
elif printf '%s' "$schema_log" | grep -q "wacht op de database"; then
  fout "de dal komt niet bij je database"
  zeg "       Vrijwel altijd de directe verbinding in plaats van de session pooler:"
  zeg "       db.<ref>.supabase.co bestaat alleen over IPv6."
  problemen=1
else
  goed "schema stond er al"
fi

pods="$(kubectl -n "$NAMESPACE" get pods --no-headers 2>/dev/null || true)"
niet_klaar="$(printf '%s\n' "$pods" | awk '$3 != "Running" && NF > 0')"
if [ -n "$niet_klaar" ]; then
  fout "niet alle pods draaien:"
  printf '%s\n' "$niet_klaar" | sed 's/^/       /'
  problemen=1
else
  goed "alle pods draaien"
fi

herstarts="$(printf '%s\n' "$pods" | awk '$4 > 0 && NF > 0 {print $1" ("$4"x)"}')"
if [ -n "$herstarts" ]; then
  let_op "herstart: $herstarts"
  zeg "       kubectl -n $NAMESPACE logs deploy/$RELEASE-grid-static --previous"
fi

# Even geduld: er is geen readiness-probe, dus de pod heet "ready" zodra de container
# draait -- terwijl uvicorn dan nog op de connector en de dal staat te wachten. Eén
# keer kijken is te vroeg; we wachten tot de regel er is.
zeg "  wachten tot de bot zijn grid heeft opgebouwd..."
bot_log=""
for _ in $(seq 1 30); do
  bot_log="$(kubectl -n "$NAMESPACE" logs deploy/"$RELEASE"-grid-static --tail=80 2>/dev/null || true)"
  if printf '%s' "$bot_log" | grep -qE "Initialising grid|Recovering state"; then
    break
  fi
  sleep 3
done

if printf '%s' "$bot_log" | grep -q "Initialising grid"; then
  goed "grid aangelegd: $(printf '%s' "$bot_log" | grep -o 'Grid ready — .*' | head -1)"
elif printf '%s' "$bot_log" | grep -q "Recovering state"; then
  goed "bestaande grid teruggevonden"
else
  fout "de bot heeft binnen 90 seconden geen grid opgebouwd"
  laatste="$(printf '%s' "$bot_log" | tail -3)"
  [ -n "$laatste" ] && printf '%s
' "$laatste" | sed 's/^/       /'
  problemen=1
fi

zeg "  even kijken of de fill-loop schoon draait (35 seconden)..."
sleep 35
fill_fout="$(kubectl -n "$NAMESPACE" logs deploy/"$RELEASE"-grid-static --since=40s 2>/dev/null | grep "Fill loop error" | head -1 || true)"
if [ -n "$fill_fout" ]; then
  fout "fout in de fill-loop:"
  zeg "       $fill_fout"
  problemen=1
else
  goed "fill-loop draait schoon"
fi

# ── Oordeel ──────────────────────────────────────────────────────────────────
if [ "$problemen" = 0 ]; then
  stap "${groen}Klaar${uit} — je stack draait in shadow"
  cat <<EOF

  Meekijken:
    kubectl -n $NAMESPACE logs deploy/$RELEASE-grid-static -f

  Je grids aanpassen: bewerk $VALUES_FILE en draai
    helm upgrade $RELEASE $CHART -n $NAMESPACE -f $VALUES_FILE

  Met echt geld handelen: zie 'Live gaan' in de README -- dat gaat met de hand,
  met opzet: het is geen handeling die je snel wilt doen.
EOF
else
  stap "${rood}Er is iets mis${uit}"
  cat <<EOF

  Hierboven staat wat er niet klopte. Meer zien:
    kubectl -n $NAMESPACE get pods
    kubectl -n $NAMESPACE logs deploy/$RELEASE-dal -c schema
    kubectl -n $NAMESPACE logs deploy/$RELEASE-grid-static --tail=50

  Opnieuw beginnen kan zonder risico -- er staat niets met echt geld:
    helm uninstall $RELEASE -n $NAMESPACE
EOF
  exit 1
fi
