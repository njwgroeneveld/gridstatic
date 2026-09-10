# gridstatic

Een grid-tradingbot voor Hyperliquid: vier services op Kubernetes, geïnstalleerd met één
Helm-commando.

De bot legt koop-orders op vaste prijslijnen tussen een onder- en bovengrens. Wordt een koop
gevuld, dan komt er een verkoop één lijn hoger; wordt die gevuld, dan keert de koop terug. De
winst is het verschil tussen twee lijnen, minus fees.

**Standaard draait alles in shadow** — een simulatie met een eigen boekhouding, zonder dat er
een order naar de beurs gaat. Live gaan vraagt twee losse, bewuste stappen; zie
[Live gaan](#live-gaan).

---

## Wat je nodig hebt

| | waarom |
|---|---|
| Een Kubernetes-cluster | k3s, minikube, kubeadm — alles voldoet |
| `kubectl` met toegang tot dat cluster | de installatie draait vanaf jouw machine |
| `helm` 3 | `winget install Helm.Helm`, `brew install helm`, of het [installatiescript](https://helm.sh/docs/intro/install/) |
| Een eigen PostgreSQL | het makkelijkst is een gratis Supabase-project |
| Uitgaand internet vanuit het cluster | naar Hyperliquid, naar je database, en naar ghcr.io voor de images |

Wat je **niet** nodig hebt: een StorageClass (de stack gebruikt geen volumes), een ingress, een
cert-manager of een service mesh. De vier services praten onderling via ClusterIP en er komt
niets van buiten naar binnen.

---

## De database

Maak een **eigen** Supabase-project aan (of gebruik een andere Postgres). Pak onder
*Project Settings → Database* de **directe** connectiestring op poort 5432 — níet de pooler op
6543: die draait in transaction-pooling en dat gaat slecht samen met de prepared statements van
psycopg2.

> **Wijs dit nooit naar een database waarin al een gridstatic-stack werkt.** Daar staan actieve
> `grid_configs` in. Een tweede bot herkent die als de zijne, gaat dezelfde grids beheren en legt
> dubbele orders. Een Kubernetes-namespace helpt daar niet tegen: die scheidt pods, niet
> databaserijen. De scheiding zit in de connectiestring.

Het schema hoef je niet zelf aan te maken. De dal brengt het bij elke start aan met een
idempotent script (`db/schema.sql`): vier tabellen en zeven indexen.

---

## Installeren

### 1. Namespace

```bash
kubectl create namespace gridstatic
```

### 2. Het database-secret

**Deze chart bevat geen wachtwoorden en maakt geen Secrets aan.** Je maakt ze zelf en geeft
alleen de naam door. Dat is met opzet: Helm bewaart de waarden waarmee je installeert in een
Secret in je cluster (`sh.helm.release.v1.<naam>.v1`), en `helm get values` toont ze terug. Wat
niet door Helm loopt, kan daar niet lekken.

```bash
kubectl -n gridstatic create secret generic gridstatic-db \
  --from-literal=url='postgresql://postgres:JOUW_WACHTWOORD@db.JOUWPROJECT.supabase.co:5432/postgres?sslmode=require'
```

De sleutel moet `url` heten.

### 3. Je waarden

Maak `mijn-values.yaml` — **buiten** deze repo, bijvoorbeeld in je home-map:

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

`startBalance` is de rekenbasis voor de ordergrootte, **ook live**. Bij 20 lijnen en de
standaard `strategyAllocationPct: 80` betekent 1000 een inleg van $40 per lijn. Zet het op het
bedrag dat je aan deze stack wilt toevertrouwen, niet op je hele vermogen.

De sleutel `BTC-20` is een vrij te kiezen label dat alleen in de logregels terugkomt. Het gedrag
komt uit de velden eronder.

### 4. Installeren

```bash
helm install gridstatic oci://ghcr.io/njwgroeneveld/charts/gridstatic \
  -n gridstatic -f mijn-values.yaml
```

### 5. Controleren

Eerst het schema, dat door een initContainer van de dal wordt aangebracht:

```bash
kubectl -n gridstatic logs deploy/gridstatic-dal -c schema
```

Blijft daar "wacht op de database..." staan, dan is je connectiestring niet bereikbaar vanuit
het cluster — controleer poort 5432 en `sslmode=require`.

Dan de bot:

```bash
kubectl -n gridstatic logs deploy/gridstatic-grid-static -f
```

Bij een verse database hoor je `Initialising grid` te zien, gevolgd door
`Grid ready — N BUY orders placed`. Zie je `Recovering state from DAL + exchange`, dan staan er
al configs in die database en wijst je URL naar de verkeerde plek.

---

## Live gaan

Twee losse stappen, allebei met opzet apart. Eén ervan alleen doet niets.

**1. Een Hyperliquid-secret aanmaken.** De wallet moet op de agent/API-wallet van je
Hyperliquid-account staan, niet op je hoofdaccount.

```bash
kubectl -n gridstatic create secret generic gridstatic-hl \
  --from-literal=private_key='0x...' \
  --from-literal=wallet_address='0x...'
```

**2. In je waarden:**

```yaml
hyperliquid:
  testnet: true              # false = mainnet, met echt geld
  existingSecret: gridstatic-hl

grid:
  coins:
    BTC-20:
      shadow: false          # deze grid handelt nu echt
```

Zonder secret start de connector gewoon, maar geven zijn order-routes een 503 — genoeg voor
shadow, te weinig om te handelen. Zonder `shadow: false` gebeurt er niets met echt geld, ook al
staat de sleutel er.

Toepassen met `helm upgrade`:

```bash
helm upgrade gridstatic oci://ghcr.io/njwgroeneveld/charts/gridstatic \
  -n gridstatic -f mijn-values.yaml
```

**Controleer na een wijziging aan de grids of het aantal actieve configs gelijk is gebleven.**
De bot herkent zijn eigen grid aan coin, aantal lijnen, boven- en ondergrens en hefboom. Wijzig
je één daarvan, dan is het voor hem een nieuw grid: hij maakt een nieuwe config aan en legt een
tweede laag orders bovenop de bestaande.

```bash
kubectl -n gridstatic exec deploy/gridstatic-dal -- \
  curl -s "http://localhost:8080/grid-configs?strategy=STATIC&active=true" | grep -o '"id"' | wc -l
```

---

## Telegram (optioneel)

```bash
kubectl -n gridstatic create secret generic gridstatic-telegram \
  --from-literal=bot_token='123456:ABC...' \
  --from-literal=chat_id='123456789'
```

```yaml
telegram:
  enabled: true
  existingSecret: gridstatic-telegram
```

Je krijgt dan meldingen bij het aanleggen van een grid, gevulde orders, gesloten trades en
fouten, plus een `/status`-opdracht in de chat. Staat telegram uit, dan komt de alerter-pod er
niet en mislukken de meldingen stil — de bot draait gewoon door.

---

## Over de veiligheid van je sleutels

Wat deze opzet wél doet: je Hyperliquid-sleutel en je databasewachtwoord komen niet in je
waardenbestand, niet in het Helm-release-secret en niet in `helm get values`.

Wat het **niet** doet: Kubernetes-Secrets zijn base64, geen versleuteling. Zonder
encryption-at-rest op etcd staan ze leesbaar op de schijf van je control-plane-node, en iedereen
met `get secrets` in die namespace kan ze lezen. Wil je verder gaan, kijk dan naar
sealed-secrets, external-secrets of SOPS.

Verder: gebruik voor deze bot een aparte Hyperliquid-agent-wallet met beperkt saldo, niet je
hoofdaccount.

---

## Verwijderen

```bash
helm uninstall gridstatic -n gridstatic
kubectl delete namespace gridstatic
```

Je data blijft in je eigen database staan; die ruim je daar op.

---

## De repo

```
services/grid-static/        de bot zelf
services/dal/                HTTP-laag boven de database
services/connector/          koppeling met Hyperliquid
services/telegram-alerter/   meldingen en de /status-opdracht
db/schema.sql                het databaseschema (bron)
chart/                       de Helm chart; chart/files/schema.sql is een kopie
```

Tests draaien:

```bash
for s in grid-static dal connector telegram-alerter; do
  PYTHONPATH="$PWD/services/$s" python -m pytest services/$s/tests/ -q
done
```

87 tests: 38 voor de bot, 13 voor de dal, 17 voor de connector, 19 voor de alerter. Elke service
bouwt zijn eigen image (amd64 en arm64) zodra zijn map verandert. De chart-workflow lint en
rendert de chart bij elke wijziging, en bewaakt daarbij drie afspraken: installeren zonder
`database.existingSecret` moet falen, de meegeleverde waarden mogen nergens `shadow: false`
bevatten, en de chart mag zelf geen Secret renderen.
