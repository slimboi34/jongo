"""Shared test setup.

By default every database test runs against a fresh in-memory SQLite database.
Set ``JONGO_TEST_DATABASE`` to a ``postgres://`` URL to run the same suite against
PostgreSQL — the backends are expected to behave identically:

    JONGO_TEST_DATABASE=postgres://localhost/jongo_test .venv/bin/python -m pytest
"""

from __future__ import annotations

import os

import pytest

from jongo import db
from jongo.db import connection

TEST_DATABASE = os.environ.get("JONGO_TEST_DATABASE") or ":memory:"


def on_postgres() -> bool:
    return TEST_DATABASE.split("://", 1)[0] in connection.POSTGRES_SCHEMES


def use_test_database() -> None:
    """Point Jongo at the database under test, wiped clean."""
    db.configure(TEST_DATABASE)
    if on_postgres():
        drop_all_tables()


def drop_all_tables() -> None:
    """Drop every table in the target schema, so each test starts empty."""
    with connection.locked() as conn:
        rows = conn.execute(
            "SELECT tablename AS name FROM pg_tables WHERE schemaname = ANY (current_schemas(false))"
        ).fetchall()
        for row in rows:
            conn.execute(f'DROP TABLE IF EXISTS "{row["name"]}" CASCADE')


#: Tests that poke SQLite internals (PRAGMAs, sqlite3 types, trace callbacks).
sqlite_only = pytest.mark.skipif(on_postgres(), reason="exercises SQLite-specific internals")
