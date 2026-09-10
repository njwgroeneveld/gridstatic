import os
from functools import lru_cache

import psycopg2
import psycopg2.extras
import psycopg2.pool


class Database:
    def __init__(self, url: str) -> None:
        self._pool = psycopg2.pool.ThreadedConnectionPool(2, 30, url)

    def _conn(self):
        return self._pool.getconn()

    def _release(self, conn) -> None:
        self._pool.putconn(conn)

    def _execute(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = self._conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                conn.commit()
                try:
                    return [dict(r) for r in cur.fetchall()]
                except psycopg2.ProgrammingError:
                    return []
        except Exception:
            conn.rollback()
            raise
        finally:
            self._release(conn)

    # grid_configs
    def insert_grid_config(self, data: dict) -> dict:
        row = self._execute(
            """INSERT INTO gridtrading.grid_configs
               (coin, strategy, upper, lower, num_lines, leverage, shadow, active)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (data["coin"], data.get("strategy", "STATIC"),
             data["upper"], data["lower"], data["num_lines"],
             data.get("leverage", 1.0), data.get("shadow", True), data.get("active", True))
        )
        return row[0]

    def get_active_configs(self, coin: str | None, strategy: str) -> list[dict]:
        if coin:
            return self._execute(
                "SELECT * FROM gridtrading.grid_configs WHERE coin=%s AND strategy=%s AND active=TRUE",
                (coin, strategy)
            )
        return self._execute(
            "SELECT * FROM gridtrading.grid_configs WHERE strategy=%s AND active=TRUE ORDER BY coin",
            (strategy,)
        )

    def patch_grid_config(self, config_id: int, fields: dict) -> None:
        sets = ", ".join(f"{k}=%s" for k in fields)
        self._execute(
            f"UPDATE gridtrading.grid_configs SET {sets} WHERE id=%s",
            (*fields.values(), config_id)
        )

    # grid_orders
    def insert_grid_order(self, data: dict) -> dict:
        row = self._execute(
            """INSERT INTO gridtrading.grid_orders
               (grid_config_id, coin, strategy, side, level, price, size_usd,
                exchange_order_id, status, shadow, fee_usd)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (data.get("grid_config_id"), data["coin"],
             data.get("strategy", "STATIC"), data["side"],
             data["level"], data["price"], data["size_usd"],
             data.get("exchange_order_id"), data.get("status", "OPEN"),
             data.get("shadow", True), data.get("fee_usd"))
        )
        return row[0]

    def get_orders(self, coin: str, status: str | None = None) -> list[dict]:
        if status:
            return self._execute(
                "SELECT * FROM gridtrading.grid_orders WHERE coin=%s AND status=%s ORDER BY level",
                (coin, status)
            )
        return self._execute(
            "SELECT * FROM gridtrading.grid_orders WHERE coin=%s ORDER BY level", (coin,)
        )

    def patch_order(self, order_id: int, fields: dict) -> None:
        sets = ", ".join(f"{k}=%s" for k in fields)
        self._execute(
            f"UPDATE gridtrading.grid_orders SET {sets} WHERE id=%s",
            (*fields.values(), order_id)
        )

    # grid_trades
    def insert_grid_funding(self, data: dict) -> dict | None:
        """Same hour can be offered twice when a poll overlaps; the unique index
        makes that a no-op instead of a duplicate row."""
        row = self._execute(
            """INSERT INTO gridtrading.grid_funding
               (coin, grid_config_id, shadow, funding_time, usdc, funding_rate, szi)
               VALUES (%s,%s,%s,to_timestamp(%s/1000.0),%s,%s,%s)
               ON CONFLICT (coin, funding_time, shadow) DO NOTHING
               RETURNING *""",
            (data["coin"], data.get("grid_config_id"), data.get("shadow", False),
             data["time"], data["usdc"], data.get("funding_rate"), data.get("szi"))
        )
        return row[0] if row else None

    def get_funding_total(self, coin: str, grid_config_id: int | None = None) -> float:
        if grid_config_id is not None:
            rows = self._execute(
                "SELECT coalesce(sum(usdc), 0) AS som FROM gridtrading.grid_funding "
                "WHERE coin=%s AND grid_config_id=%s", (coin, grid_config_id))
        else:
            rows = self._execute(
                "SELECT coalesce(sum(usdc), 0) AS som FROM gridtrading.grid_funding "
                "WHERE coin=%s", (coin,))
        return float(rows[0]["som"]) if rows else 0.0

    def get_last_funding_ms(self, coin: str, shadow: bool) -> int | None:
        rows = self._execute(
            "SELECT EXTRACT(EPOCH FROM MAX(funding_time)) * 1000 AS ms "
            "FROM gridtrading.grid_funding WHERE coin=%s AND shadow=%s",
            (coin, shadow))
        ms = rows[0]["ms"] if rows else None
        return int(ms) if ms is not None else None

    def insert_grid_trade(self, data: dict) -> dict:
        row = self._execute(
            """INSERT INTO gridtrading.grid_trades
               (coin, strategy, buy_order_id, buy_price, size_usd, status, shadow,
                fee_usd, opened_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW()) RETURNING *""",
            (data["coin"], data.get("strategy", "STATIC"),
             data.get("buy_order_id"), data.get("buy_price"),
             data.get("size_usd"), data.get("status", "OPEN"),
             data.get("shadow", True), data.get("fee_usd"))
        )
        return row[0]

    def get_trades(self, coin: str, status: str | None = None,
                   grid_config_id: int | None = None) -> list[dict]:
        """Every trade row carries buy_level: the grid line the position was
        opened on. That is the only stable handle callers have on "which line
        does this belong to" -- buy_price cannot serve, because the connector
        rounds the limit price to five significant digits, the exchange fills at
        a price of its own, and a partial fill averages the entry across pieces.
        """
        if grid_config_id is not None:
            sql = """SELECT gt.*, go.level AS buy_level FROM gridtrading.grid_trades gt
                     JOIN gridtrading.grid_orders go ON gt.buy_order_id = go.id
                     WHERE go.grid_config_id = %s"""
            params: tuple = (grid_config_id,)
            if status:
                sql += " AND gt.status = %s"
                params += (status,)
            return self._execute(sql, params)
        sql = """SELECT gt.*, go.level AS buy_level FROM gridtrading.grid_trades gt
                 LEFT JOIN gridtrading.grid_orders go ON gt.buy_order_id = go.id
                 WHERE gt.coin = %s"""
        params = (coin,)
        if status:
            sql += " AND gt.status = %s"
            params += (status,)
        return self._execute(sql, params)

    def patch_trade(self, trade_id: int, fields: dict) -> None:
        sets = ", ".join(f"{k}=%s" for k in fields)
        self._execute(
            f"UPDATE gridtrading.grid_trades SET {sets} WHERE id=%s",
            (*fields.values(), trade_id)
        )


@lru_cache(maxsize=1)
def get_db() -> Database:
    url = os.environ["DATABASE_URL"]
    return Database(url)
