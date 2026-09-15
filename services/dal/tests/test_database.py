from decimal import Decimal
from unittest.mock import MagicMock, patch
from src.database import Database


def test_pool_max_size_is_at_least_30():
    # Regression test: with 9+ static grids plus trailing grids each making
    # several DAL calls per health-check/fill-loop cycle, a cap of 10
    # concurrent connections caused "connection pool exhausted" PoolErrors
    # (surfacing as 500s) that recurred for hours, not just once at startup.
    with patch("psycopg2.pool.ThreadedConnectionPool") as mock_pool:
        Database("postgresql://fake/db")
    args = mock_pool.call_args[0]
    minconn, maxconn = args[0], args[1]
    assert maxconn >= 30


def test_insert_grid_config_calls_correct_sql(db):
    db._execute = MagicMock(return_value=[{"id": 1, "coin": "BTC"}])
    result = db.insert_grid_config({
        "coin": "BTC", "strategy": "STATIC",
        "upper": 68000, "lower": 57000, "num_lines": 10,
        "leverage": 1.0, "shadow": True, "active": True
    })
    assert result["coin"] == "BTC"
    sql = db._execute.call_args[0][0]
    assert "INSERT INTO gridtrading.grid_configs" in sql


def test_get_trades_exposes_buy_level_per_config(db):
    # buy_level is what callers match a position to its grid line on. Without it
    # the grid engine fell back to comparing buy_price against the line price --
    # which never matches on a live grid, so it re-bought occupied lines.
    db._execute = MagicMock(return_value=[])
    db.get_trades("BTC", status="OPEN", grid_config_id=15)
    sql = db._execute.call_args[0][0]
    assert "go.level AS buy_level" in sql
    assert "gt.status = %s" in sql


def test_get_trades_exposes_buy_level_without_config(db):
    db._execute = MagicMock(return_value=[])
    db.get_trades("BTC", status="OPEN")
    sql = db._execute.call_args[0][0]
    assert "go.level AS buy_level" in sql
    # LEFT JOIN, not JOIN: a trade whose buy order was pruned still has to come
    # back, just without a level.
    assert "LEFT JOIN" in sql


def test_get_orders_filters_by_status(db):
    db._execute = MagicMock(return_value=[])
    db.get_orders("BTC", status="OPEN")
    sql = db._execute.call_args[0][0]
    assert "status=%s" in sql


def test_patch_order_builds_set_clause(db):
    db._execute = MagicMock(return_value=[])
    db.patch_order(42, {"status": "FILLED", "filled_price": 94000.0})
    sql = db._execute.call_args[0][0]
    assert "UPDATE gridtrading.grid_orders" in sql
    assert "status=%s" in sql
    assert "filled_price=%s" in sql


def test_patch_grid_config_builds_set_clause(db):
    db._execute = MagicMock(return_value=[])
    db.patch_grid_config(9, {"upper": 82.0, "lower": 73.0})
    sql = db._execute.call_args[0][0]
    assert "UPDATE gridtrading.grid_configs" in sql
    assert "upper=%s" in sql
    assert "lower=%s" in sql


def test_insert_grid_trade_returns_row(db):
    db._execute = MagicMock(return_value=[{"id": 7, "coin": "BTC", "status": "OPEN"}])
    result = db.insert_grid_trade({"coin": "BTC", "buy_order_id": 3, "buy_price": 94000.0, "size_usd": 800.0})
    assert result["id"] == 7




def test_execute_returns_floats_not_decimals(db):
    """Regression test. Money columns are numeric, and psycopg2 returns them as
    Decimal. FastAPI serialises a Decimal on a route with a dict annotation as a JSON
    string (Pydantic v2), so the bot received "83000" where it expected 83000. It
    compares its bounds with these values to recognise its own grid: a string never
    matches, so on every restart it would create a new config and a second layer of
    orders."""
    cur = MagicMock()
    cur.fetchall.return_value = [{
        "upper": Decimal("83000"),
        "price": Decimal("79210.526315789"),
        "coin": "BTC",
        "level": 10,
        "shadow": True,
        "filled_price": None,
    }]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    db._conn = MagicMock(return_value=conn)
    db._release = MagicMock()

    row = db._execute("SELECT 1")[0]

    assert isinstance(row["upper"], float) and row["upper"] == 83000
    assert isinstance(row["price"], float)
    # anything that is not a Decimal is left untouched
    assert row["coin"] == "BTC" and row["level"] == 10
    assert row["shadow"] is True and row["filled_price"] is None
