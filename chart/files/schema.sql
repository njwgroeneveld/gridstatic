-- gridstatic databaseschema
--
-- Gelezen uit de draaiende Supabase op 2026-09-10 via de systeemcatalogus
-- (pg_attribute, pg_constraint, pg_indexes) -- dezelfde bron die pg_dump gebruikt.
-- De tabel grid_dynamic_trades hoort bij de dynamic-grid en zit hier bewust niet in.
--
-- Idempotent: dit script mag zo vaak draaien als je wilt. De initContainer van de
-- dal voert het uit bij elke start.
--
-- Eén bewuste afwijking van de database van de gridtrading-stack: prijzen, bedragen
-- en winst staan hier op `numeric` in plaats van `double precision`. Geld hoort exact
-- opgeslagen te worden; `double precision` rondt af en dat zie je terug in sommen.
-- De services merken er niets van: psycopg2 leest `numeric` als Decimal, maar FastAPI
-- zet die om naar een gewone float voordat het antwoord de DAL verlaat. `fee_usd` en
-- `grid_funding.usdc` staan in de draaiende database al op `numeric` en gaan al
-- maanden zo goed. `leverage` blijft double precision: dat is een factor, geen geld.

CREATE SCHEMA IF NOT EXISTS gridtrading;

-- ── grid_configs ─────────────────────────────────────────────────────────────
-- Eén rij per grid: zijn grenzen, aantal lijnen en hefboom. De bot herkent zijn
-- eigen grid bij het opstarten aan coin + num_lines + upper + lower + leverage.
--
-- Weggelaten ten opzichte van de database van de gridtrading-stack: coin_key,
-- run_seq, started_at, ended_at en end_reason met hun index. Die horen bij het
-- run-versioning-ontwerp van 2026-09-03 en worden door geen enkele service in deze
-- stack geschreven of gelezen.

CREATE TABLE IF NOT EXISTS gridtrading.grid_configs (
    id          serial PRIMARY KEY,
    coin        text NOT NULL,
    strategy    text NOT NULL DEFAULT 'STATIC'::text,
    upper       numeric NOT NULL,
    lower       numeric NOT NULL,
    num_lines   integer NOT NULL,
    leverage    double precision NOT NULL DEFAULT 1.0,
    shadow      boolean NOT NULL DEFAULT true,
    active      boolean NOT NULL DEFAULT true,
    created_at  timestamp with time zone DEFAULT now()
);

-- ── grid_orders ──────────────────────────────────────────────────────────────
-- Eén rij per order. `level` is het lijnnummer en is de sleutel waarop de bot een
-- bezette gridlijn herkent -- niet de prijs, die onderweg door tick-afronding en
-- deelvullingen verandert.

CREATE TABLE IF NOT EXISTS gridtrading.grid_orders (
    id                serial PRIMARY KEY,
    grid_config_id    integer REFERENCES gridtrading.grid_configs(id),
    coin              text NOT NULL,
    strategy          text NOT NULL DEFAULT 'STATIC'::text,
    side              text NOT NULL,
    level             integer NOT NULL,
    price             numeric NOT NULL,
    size_usd          numeric NOT NULL,
    exchange_order_id text,
    status            text NOT NULL DEFAULT 'OPEN'::text,
    filled_price      numeric,
    filled_at         timestamp with time zone,
    shadow            boolean NOT NULL DEFAULT true,
    created_at        timestamp with time zone DEFAULT now(),
    fee_usd           numeric
);

-- Twee rustende buy-orders op dezelfde lijn horen niet te bestaan. Alleen BUY:
-- twee verschillende buys mogen legitiem allebei één lijn hoger verkopen.
CREATE UNIQUE INDEX IF NOT EXISTS grid_orders_one_open_buy_per_level
    ON gridtrading.grid_orders USING btree (grid_config_id, level)
    WHERE ((status = 'OPEN'::text) AND (side = 'BUY'::text));

-- ── grid_trades ──────────────────────────────────────────────────────────────
-- Eén rij per koop-verkoop-rondgang. buy_order_id wijst naar de order en levert
-- via een join het lijnnummer van de trade.

CREATE TABLE IF NOT EXISTS gridtrading.grid_trades (
    id             serial PRIMARY KEY,
    coin           text NOT NULL,
    strategy       text NOT NULL DEFAULT 'STATIC'::text,
    buy_order_id   integer REFERENCES gridtrading.grid_orders(id),
    sell_order_id  integer REFERENCES gridtrading.grid_orders(id),
    buy_price      numeric,
    sell_price     numeric,
    size_usd       numeric,
    profit_usd     numeric,
    status         text NOT NULL DEFAULT 'OPEN'::text,
    shadow         boolean NOT NULL DEFAULT true,
    opened_at      timestamp with time zone,
    closed_at      timestamp with time zone,
    grid_config_id integer REFERENCES gridtrading.grid_configs(id),
    fee_usd        numeric
);

CREATE INDEX IF NOT EXISTS grid_trades_grid_config_id_idx
    ON gridtrading.grid_trades USING btree (grid_config_id);

-- ── grid_funding ─────────────────────────────────────────────────────────────
-- Fundingbetalingen, apart van de handelswinst. De unieke sleutel voorkomt dat
-- dezelfde betaling twee keer wordt geboekt bij een herstart.

CREATE TABLE IF NOT EXISTS gridtrading.grid_funding (
    id             bigserial PRIMARY KEY,
    coin           text NOT NULL,
    grid_config_id integer,
    shadow         boolean NOT NULL DEFAULT false,
    funding_time   timestamp with time zone NOT NULL,
    usdc           numeric NOT NULL,
    funding_rate   numeric,
    szi            numeric,
    created_at     timestamp with time zone NOT NULL DEFAULT now(),
    UNIQUE (coin, funding_time, shadow)
);
