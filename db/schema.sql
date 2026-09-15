-- gridstatic database schema
--
-- Idempotent: safe to run as often as you like. The dal's initContainer runs it on
-- every start.
--
-- Prices, amounts and profit are `numeric`, not `double precision`: money should be
-- stored exactly, and floating point rounding shows up in sums. psycopg2 returns
-- `numeric` as Python Decimal, which FastAPI would serialise as a JSON *string* on
-- routes annotated with dict -- so the dal converts every Decimal to float before a
-- row leaves the service (see Database._execute). `leverage` stays double precision:
-- it is a multiplier, not money.

CREATE SCHEMA IF NOT EXISTS gridtrading;

-- ── grid_configs ─────────────────────────────────────────────────────────────
-- One row per grid: its bounds, number of lines and leverage. On startup the bot
-- recognises its own grid by coin + num_lines + upper + lower + leverage.

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
-- One row per order. `level` is the grid line number, and it is the key the bot
-- uses to recognise an occupied line -- not the price, which is changed along the
-- way by tick rounding and partial fills.

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

-- Two resting buy orders on the same line must never exist. BUY only: two
-- different buys may legitimately both sell one line higher.
CREATE UNIQUE INDEX IF NOT EXISTS grid_orders_one_open_buy_per_level
    ON gridtrading.grid_orders USING btree (grid_config_id, level)
    WHERE ((status = 'OPEN'::text) AND (side = 'BUY'::text));

-- ── grid_trades ──────────────────────────────────────────────────────────────
-- One row per buy-sell round trip. buy_order_id points at the order, and a join on
-- it yields the trade's grid line.

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
-- Funding payments, kept apart from trading profit. The unique key stops the same
-- payment from being booked twice after a restart.

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
