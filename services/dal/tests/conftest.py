import pytest
from unittest.mock import patch
from src.database import Database


@pytest.fixture
def db():
    with patch("psycopg2.pool.ThreadedConnectionPool"):
        d = Database("postgresql://fake/db")
    return d
