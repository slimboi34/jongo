"""Backends: the handful of places SQLite and PostgreSQL actually differ.

Jongo builds one SQL string — ``?`` placeholders, SQLite's type vocabulary
(``TEXT``/``INTEGER``/``REAL``) — and the dialect adapts it at the connection edge.
SQLite stays the zero-config default and its dialect is the identity, so nothing
about the default path changes.

Both backends store the same representations (datetimes and dates as ISO-8601 text,
booleans as integers, JSON as text) so a model behaves identically whichever one it
runs on. That consistency is deliberate; see ``docs`` for the trade-off.
"""

from __future__ import annotations

from typing import Any


def translate(sql: str, has_params: bool, placeholder: str) -> str:
    """Rewrite ``?`` placeholders outside string literals into ``placeholder``.

    Jongo inlines SQL-safe literals in some paths (a large ``__in`` list), so a
    ``?`` or ``%`` inside a quoted string must be left alone. When parameters are
    bound and the target paramstyle is ``%s``, literal ``%`` is doubled for the driver.
    """
    if placeholder == "?":
        return sql
    escape_percent = has_params
    out: list[str] = []
    quote_char: str | None = None
    for char in sql:
        if quote_char is not None:
            out.append("%%" if escape_percent and char == "%" else char)
            if char == quote_char:
                quote_char = None
        elif char in ("'", '"'):
            quote_char = char
            out.append(char)
        elif char == "?":
            out.append(placeholder)
        elif char == "%" and escape_percent:
            out.append("%%")
        else:
            out.append(char)
    return "".join(out)


class Dialect:
    """SQLite: the default, and the shape every other backend is adapted to."""

    name = "sqlite"
    placeholder = "?"
    supports_returning = False
    #: SQLite can only ALTER a table in narrow ways, so most changes rebuild it.
    rebuilds_tables = True

    # -- statements ------------------------------------------------------------

    def translate(self, sql: str, has_params: bool) -> str:
        return translate(sql, has_params, self.placeholder)

    def begin(self) -> str:
        return "BEGIN IMMEDIATE"

    def random(self) -> str:
        return "RANDOM()"

    def limit_offset(self, limit: int | None, offset: int) -> tuple[str, list]:
        """A LIMIT/OFFSET clause; SQLite spells "no limit" as ``LIMIT -1``."""
        return " LIMIT ? OFFSET ?", [-1 if limit is None else limit, offset]

    def not_equal(self, column: str) -> str:
        """``!=`` that also matches NULL rows, as Python's ``!=`` would."""
        return f"{column} IS NOT ?"

    def negate(self, sql: str) -> str:
        """NOT, with NULL comparisons counted as "no match" so they survive it."""
        return f"NOT COALESCE(({sql}), 0)"

    def text_condition(self, column: str, lookup: str, text: str) -> tuple[str, list]:
        """Compile a substring/case-insensitive lookup.

        SQLite's LIKE is case-*insensitive* for ASCII and GLOB is case-sensitive,
        so the case-sensitive lookups go through GLOB here.
        """
        from .query import escape_glob, escape_like

        if lookup == "iexact":
            return f"{column} LIKE ? ESCAPE '\\'", [escape_like(text)]
        if lookup.startswith("i"):
            pattern = {"icontains": "%{}%", "istartswith": "{}%", "iendswith": "%{}"}[lookup]
            return f"{column} LIKE ? ESCAPE '\\'", [pattern.format(escape_like(text))]
        pattern = {"contains": "*{}*", "startswith": "{}*", "endswith": "*{}"}[lookup]
        return f"{column} GLOB ?", [pattern.format(escape_glob(text))]

    # -- writes ----------------------------------------------------------------

    def insert(self, sql: str) -> str:
        return sql

    def inserted_id(self, cursor) -> Any:
        return cursor.lastrowid

    def after_explicit_id(self, conn, table: str, column: str = "id") -> None:
        """Nothing to do: SQLite's AUTOINCREMENT already tracks the largest id."""

    # -- DDL -------------------------------------------------------------------

    def ddl_type(self, type_: str) -> str:
        return type_

    def pk_ddl(self) -> str:
        return "PRIMARY KEY AUTOINCREMENT"

    def normalize_type(self, type_: str) -> str:
        """Map a type read back from the live schema into Jongo's vocabulary."""
        return (type_ or "").upper()

    # -- errors ----------------------------------------------------------------

    def integrity_errors(self) -> tuple[type[Exception], ...]:
        import sqlite3

        return (sqlite3.IntegrityError,)

    def constraint_violation(self, exc: Exception) -> tuple[str, str | None] | None:
        """Classify an integrity error as ``(kind, column)``; None to re-raise it."""
        msg = str(exc)
        if "UNIQUE constraint failed" in msg:
            return "unique", msg.split(":", 1)[1].strip() if ":" in msg else None
        if "NOT NULL constraint failed" in msg:
            return "notnull", msg.split(":", 1)[1].strip() if ":" in msg else None
        if "FOREIGN KEY constraint failed" in msg:
            return "foreign_key", None
        return None


class PostgresDialect(Dialect):
    """PostgreSQL over psycopg 3."""

    name = "postgres"
    placeholder = "%s"
    supports_returning = True
    rebuilds_tables = False

    _TYPES = {"TEXT": "TEXT", "INTEGER": "BIGINT", "REAL": "DOUBLE PRECISION", "BLOB": "BYTEA", "": ""}
    _FROM_PG = {
        "text": "TEXT", "character varying": "TEXT", "varchar": "TEXT", "citext": "TEXT",
        "bigint": "INTEGER", "integer": "INTEGER", "smallint": "INTEGER", "int8": "INTEGER",
        "int4": "INTEGER", "int2": "INTEGER", "numeric": "REAL",
        "double precision": "REAL", "real": "REAL", "float8": "REAL", "bytea": "BLOB",
    }

    def begin(self) -> str:
        return "BEGIN"

    def limit_offset(self, limit: int | None, offset: int) -> tuple[str, list]:
        if limit is None:
            return " LIMIT ALL OFFSET ?", [offset]
        return " LIMIT ? OFFSET ?", [limit, offset]

    def not_equal(self, column: str) -> str:
        return f"{column} IS DISTINCT FROM ?"

    def negate(self, sql: str) -> str:
        return f"NOT COALESCE(({sql}), false)"  # PostgreSQL has a real boolean type

    def text_condition(self, column: str, lookup: str, text: str) -> tuple[str, list]:
        """PostgreSQL's LIKE is already case-sensitive, and ILIKE is the folded form."""
        from .query import escape_like

        escaped = escape_like(text)
        if lookup == "iexact":
            return f"{column} ILIKE ? ESCAPE '\\'", [escaped]
        operator = "ILIKE" if lookup.startswith("i") else "LIKE"
        key = lookup[1:] if lookup.startswith("i") else lookup
        pattern = {"contains": "%{}%", "startswith": "{}%", "endswith": "%{}"}[key]
        return f"{column} {operator} ? ESCAPE '\\'", [pattern.format(escaped)]

    def insert(self, sql: str) -> str:
        return f'{sql} RETURNING "id"'

    def inserted_id(self, cursor) -> Any:
        row = cursor.fetchone()
        if row is None:
            return None
        return row["id"] if isinstance(row, dict) else row[0]

    def after_explicit_id(self, conn, table: str, column: str = "id") -> None:
        """Move the identity sequence past an explicitly inserted id.

        ``GENERATED BY DEFAULT AS IDENTITY`` lets you supply your own id but does not
        advance the sequence, so the next generated id would collide. SQLite's
        AUTOINCREMENT has no such gap, and Jongo should not either.
        """
        quoted = '"' + table.replace('"', '""') + '"'
        quoted_column = '"' + column.replace('"', '""') + '"'
        conn.execute(
            f"SELECT setval(pg_get_serial_sequence(?, ?), "
            f"(SELECT MAX({quoted_column}) FROM {quoted}), true)",
            [table, column],
        )

    def ddl_type(self, type_: str) -> str:
        return self._TYPES.get((type_ or "").upper(), type_)

    def pk_ddl(self) -> str:
        return "GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"

    def normalize_type(self, type_: str) -> str:
        base = (type_ or "").split("(")[0].strip().lower()
        return self._FROM_PG.get(base, base.upper())

    def integrity_errors(self) -> tuple[type[Exception], ...]:
        import psycopg

        return (psycopg.errors.IntegrityError,)

    def constraint_violation(self, exc: Exception) -> tuple[str, str | None] | None:
        """PostgreSQL reports the constraint and column in structured diagnostics."""
        diag = getattr(exc, "diag", None)
        code = getattr(diag, "sqlstate", None) or getattr(exc, "sqlstate", None)
        column = getattr(diag, "column_name", None)
        if code == "23505":  # unique_violation — the column lives in the constraint name
            detail = getattr(diag, "message_detail", "") or ""
            name = getattr(diag, "constraint_name", "") or ""
            if not column and detail.startswith("Key ("):
                column = detail[5:].split(")", 1)[0]
            return "unique", column or name
        if code == "23502":  # not_null_violation
            return "notnull", column
        if code == "23503":  # foreign_key_violation
            return "foreign_key", column
        return None


_DIALECTS = {"sqlite": Dialect, "postgres": PostgresDialect}


def for_backend(name: str) -> Dialect:
    try:
        return _DIALECTS[name]()
    except KeyError:
        raise ValueError(f"Unknown database backend {name!r}; expected one of {', '.join(_DIALECTS)}.") from None
