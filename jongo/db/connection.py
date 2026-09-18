"""Connection management, transactions and raw SQL helpers.

SQLite is the default and needs no configuration. Pass a ``postgres://`` URL to
``Jongo(database=…)`` (or ``JONGO_DATABASE``) to run the same models on PostgreSQL;
that needs the optional ``psycopg`` driver (``pip install "jongo[postgres]"``).
"""

from __future__ import annotations

import itertools
import os
import sqlite3
import time
import threading
import weakref
from contextlib import ContextDecorator, contextmanager
from typing import Any, Callable, Iterator, Sequence

from .. import log as _jlog
from .dialect import Dialect, for_backend

MEMORY = ":memory:"
DEFAULT_DATABASE = "db.sqlite3"
ENV_VAR = "JONGO_DATABASE"
POSTGRES_SCHEMES = ("postgres", "postgresql")

_config_lock = threading.RLock()
_memory_lock = threading.RLock()
_local = threading.local()
_open_connections: "weakref.WeakSet" = weakref.WeakSet()
_database: str | None = None
_backend: str = "sqlite"
_dialect: Dialect = Dialect()
_generation = 0
_memory_connection: "_Connection | None" = None
_savepoint_ids = itertools.count(1)


_WRITE = frozenset(("INSERT", "UPDATE", "DELETE", "REPLACE"))


class _Connection(sqlite3.Connection):
    """A sqlite3 connection that can be weakly referenced (so we can track it).

    Its ``execute`` is the single choke point for data-modifying statements: it
    times and logs INSERT/UPDATE/DELETE via ``jongo.log`` — including the
    row-level writes ``Model.save`` issues straight on the connection, which
    would otherwise bypass the module-level ``execute``. Reads are logged in
    ``fetch`` (which knows the row count); transaction-control and PRAGMA
    statements are left silent. When SQL logging is off (production) this adds
    only one ``isEnabledFor`` check per statement.
    """

    def execute(self, sql, parameters=()):  # type: ignore[override]
        if not _jlog.sql_enabled():
            return super().execute(sql, parameters)
        head = sql.lstrip()[:12].split(None, 1)[0].upper() if sql.strip() else ""
        if head not in _WRITE:
            return super().execute(sql, parameters)  # reads -> fetch(); control -> silent
        t0 = time.perf_counter()
        cursor = super().execute(sql, parameters)
        ms = (time.perf_counter() - t0) * 1000
        rows = cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else None
        _jlog.sql(sql, ms, rows)
        _jlog.record_sql(ms)
        return cursor


class _Row(dict):
    """A PostgreSQL row that behaves like ``sqlite3.Row``.

    Jongo's SQL layer reads rows both ways — ``row["title"]`` and ``row[0]`` — and
    iterates them for values, so the two backends hand back the same shape.
    """

    __slots__ = ("_values",)

    def __init__(self, names: Sequence[str], values: Sequence[Any]):
        super().__init__(zip(names, values))
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return dict.__getitem__(self, key)

    def __iter__(self):
        return iter(self._values)


def _row_factory(cursor):
    names = [column.name for column in cursor.description or ()]
    return lambda values: _Row(names, values)


class _PostgresConnection:
    """Adapts a psycopg connection to the small surface Jongo uses on SQLite.

    Jongo builds SQL with ``?`` placeholders; this translates each statement for
    psycopg's ``%s`` paramstyle, keeps the write logging identical, and exposes
    ``in_transaction`` the way ``sqlite3.Connection`` does.
    """

    def __init__(self, raw, dialect: Dialect):
        self._raw = raw
        self._dialect = dialect
        self._trace = None

    def set_trace_callback(self, callback) -> None:
        """Call ``callback(sql)`` for every statement, as sqlite3 connections do."""
        self._trace = callback

    @property
    def raw(self):
        return self._raw

    @property
    def in_transaction(self) -> bool:
        import psycopg

        return self._raw.info.transaction_status != psycopg.pq.TransactionStatus.IDLE

    def execute(self, sql, parameters=()):
        params = list(parameters) if parameters else None
        statement = self._dialect.translate(sql, params is not None)
        if self._trace is not None:
            self._trace(sql)
        if not _jlog.sql_enabled():
            return self._raw.execute(statement, params)
        head = sql.lstrip()[:12].split(None, 1)[0].upper() if sql.strip() else ""
        if head not in _WRITE:
            return self._raw.execute(statement, params)
        t0 = time.perf_counter()
        cursor = self._raw.execute(statement, params)
        ms = (time.perf_counter() - t0) * 1000
        rows = cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else None
        _jlog.sql(sql, ms, rows)
        _jlog.record_sql(ms)
        return cursor

    def close(self) -> None:
        self._raw.close()


def _parse_url(url: str | os.PathLike) -> tuple[str, str]:
    """Turn a database URL or path into ``(backend, target)``."""
    url = os.fspath(url).strip()
    if not url:
        raise ValueError("Database URL must not be empty.")
    if "://" in url:
        scheme, _, rest = url.partition("://")
        scheme = scheme.lower()
        if scheme in POSTGRES_SCHEMES:
            return "postgres", url
        if scheme != "sqlite":
            raise ValueError(
                f"Unsupported database URL {url!r}: Jongo supports sqlite:// and postgres:// URLs."
            )
        if rest in ("", "/", MEMORY, "/" + MEMORY):
            return "sqlite", MEMORY
        url = rest[1:] if rest.startswith("/") else rest
    if url == MEMORY:
        return "sqlite", MEMORY
    return "sqlite", os.path.abspath(os.path.expanduser(url))


def configure(url: str | os.PathLike) -> None:
    """Point Jongo at a database; closes any connections to the previous one."""
    global _database, _backend, _dialect
    backend, database = _parse_url(url)
    if backend == "postgres":
        _require_psycopg()
    with _config_lock:
        close_connections()
        _database, _backend, _dialect = database, backend, for_backend(backend)


def _require_psycopg():
    try:
        import psycopg  # noqa: F401
    except ModuleNotFoundError:
        raise ModuleNotFoundError(
            "PostgreSQL support needs the psycopg driver: pip install \"jongo[postgres]\""
        ) from None
    return psycopg


def _resolve() -> str:
    """Resolve the configured database once, filling in the backend and dialect."""
    global _database, _backend, _dialect
    with _config_lock:
        if _database is None:
            backend, database = _parse_url(os.environ.get(ENV_VAR) or DEFAULT_DATABASE)
            if backend == "postgres":
                _require_psycopg()
            _database, _backend, _dialect = database, backend, for_backend(backend)
        return _database


def database_path() -> str:
    """The configured database path, URL or ``":memory:"``, resolving the default."""
    return _resolve()


def backend() -> str:
    """The active backend: ``"sqlite"`` or ``"postgres"``."""
    _resolve()
    return _backend


def dialect() -> Dialect:
    """The active dialect."""
    _resolve()
    return _dialect


def is_memory() -> bool:
    return backend() == "sqlite" and database_path() == MEMORY


def is_postgres() -> bool:
    return backend() == "postgres"


def _connect(database: str):
    if _backend == "postgres":
        psycopg = _require_psycopg()
        raw = psycopg.connect(database, autocommit=True, row_factory=_row_factory)
        return _PostgresConnection(raw, _dialect)

    memory = database == MEMORY
    if not memory:
        os.makedirs(os.path.dirname(database) or ".", exist_ok=True)
    conn = sqlite3.connect(
        database,
        timeout=30,
        isolation_level=None,
        check_same_thread=False,
        factory=_Connection,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if not memory:
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_connection():
    """Return this thread's connection (or the shared one for in-memory databases)."""
    global _memory_connection
    database = database_path()
    if is_memory():
        with _config_lock:
            if _memory_connection is None:
                _memory_connection = _connect(MEMORY)
            return _memory_connection

    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "generation", None) == _generation:
        return conn
    if conn is not None:
        _safe_close(conn)
    with _config_lock:
        conn = _connect(database)
        _open_connections.add(conn)
        _local.conn, _local.generation = conn, _generation
    return conn


def _safe_close(conn) -> None:
    try:
        conn.close()
    except Exception:
        pass


def close_connections() -> None:
    """Close every connection Jongo has opened, in all threads."""
    global _memory_connection, _generation
    with _config_lock:
        _generation += 1
        for conn in list(_open_connections):
            _safe_close(conn)
        _open_connections.clear()
        if _memory_connection is not None:
            with _memory_lock:
                _safe_close(_memory_connection)
                _memory_connection = None
        _local.conn = None


@contextmanager
def locked() -> Iterator[Any]:
    """Yield the connection, holding the shared-connection lock for in-memory databases."""
    if is_memory():
        with _memory_lock:
            yield get_connection()
    else:
        yield get_connection()


def execute(sql: str, params: Sequence[Any] | dict = ()):
    """Execute one SQL statement and return the cursor.

    Data-modifying statements are timed and logged by the connection wrapper.
    """
    with locked() as conn:
        return conn.execute(sql, params)


def fetch(sql: str, params: Sequence[Any] | dict = ()) -> list:
    """Execute a query and return all rows (fetched while holding the lock)."""
    with locked() as conn:
        if not _jlog.sql_enabled():
            return conn.execute(sql, params).fetchall()
        t0 = time.perf_counter()
        rows = conn.execute(sql, params).fetchall()
        ms = (time.perf_counter() - t0) * 1000
        _jlog.sql(sql, ms, len(rows))
        _jlog.record_sql(ms)
        return rows


def query(sql: str, params: Sequence[Any] | dict = ()) -> list[dict[str, Any]]:
    """Execute a query and return the rows as plain dicts."""
    return [dict(row) for row in fetch(sql, params)]


class _Transaction(ContextDecorator):
    """Context manager / decorator: BEGIN/COMMIT outermost, SAVEPOINTs when nested."""

    def __init__(self) -> None:
        self._frames: list[tuple[Any, str | None, bool]] = []

    def _recreate_cm(self) -> _Transaction:
        return _Transaction()

    def __enter__(self):
        memory = is_memory()
        if memory:
            _memory_lock.acquire()
        try:
            conn = get_connection()
            if conn.in_transaction:
                savepoint = f"jongo_sp_{next(_savepoint_ids)}"
                conn.execute(f'SAVEPOINT "{savepoint}"')
            else:
                savepoint = None
                conn.execute(dialect().begin())
        except BaseException:
            if memory:
                _memory_lock.release()
            raise
        self._frames.append((conn, savepoint, memory))
        return conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        conn, savepoint, memory = self._frames.pop()
        try:
            if savepoint is None:
                self._finish(conn, rollback=exc_type is not None)
            elif conn.in_transaction:
                if exc_type is not None:
                    conn.execute(f'ROLLBACK TO "{savepoint}"')
                conn.execute(f'RELEASE "{savepoint}"')
        finally:
            if memory:
                _memory_lock.release()
        return False

    @staticmethod
    def _finish(conn, rollback: bool) -> None:
        if not conn.in_transaction:
            return
        if rollback:
            conn.execute("ROLLBACK")
            return
        try:
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def transaction(func: Callable | None = None):
    """Run a block atomically: ``with transaction():``, ``@transaction`` or ``@transaction()``."""
    if func is None:
        return _Transaction()
    return _Transaction()(func)
