"""SQLite connection management, transactions and raw SQL helpers."""

from __future__ import annotations

import itertools
import os
import sqlite3
import threading
import weakref
from contextlib import ContextDecorator, contextmanager
from typing import Any, Callable, Iterator, Sequence

MEMORY = ":memory:"
DEFAULT_DATABASE = "db.sqlite3"
ENV_VAR = "JONGO_DATABASE"

_config_lock = threading.RLock()
_memory_lock = threading.RLock()
_local = threading.local()
_open_connections: "weakref.WeakSet[_Connection]" = weakref.WeakSet()
_database: str | None = None
_generation = 0
_memory_connection: _Connection | None = None
_savepoint_ids = itertools.count(1)


class _Connection(sqlite3.Connection):
    """A sqlite3 connection that can be weakly referenced (so we can track it)."""


def _parse_url(url: str | os.PathLike) -> str:
    """Turn a database URL or path into a filesystem path or ``":memory:"``."""
    url = os.fspath(url).strip()
    if not url:
        raise ValueError("Database URL must not be empty.")
    if "://" in url:
        scheme, _, rest = url.partition("://")
        if scheme.lower() != "sqlite":
            raise ValueError(f"Unsupported database URL {url!r}: Jongo only supports sqlite:// URLs.")
        if rest in ("", "/", MEMORY, "/" + MEMORY):
            return MEMORY
        url = rest[1:] if rest.startswith("/") else rest
    if url == MEMORY:
        return MEMORY
    return os.path.abspath(os.path.expanduser(url))


def configure(url: str | os.PathLike) -> None:
    """Point Jongo at a database; closes any connections to the previous one."""
    global _database
    database = _parse_url(url)
    with _config_lock:
        close_connections()
        _database = database


def database_path() -> str:
    """The configured database path (or ``":memory:"``), resolving the default."""
    global _database
    with _config_lock:
        if _database is None:
            _database = _parse_url(os.environ.get(ENV_VAR) or DEFAULT_DATABASE)
        return _database


def is_memory() -> bool:
    return database_path() == MEMORY


def _connect(database: str) -> _Connection:
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


def get_connection() -> sqlite3.Connection:
    """Return this thread's connection (or the shared one for in-memory databases)."""
    global _memory_connection
    database = database_path()
    if database == MEMORY:
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


def _safe_close(conn: sqlite3.Connection) -> None:
    try:
        conn.close()
    except sqlite3.Error:
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
def locked() -> Iterator[sqlite3.Connection]:
    """Yield the connection, holding the shared-connection lock for in-memory databases."""
    if is_memory():
        with _memory_lock:
            yield get_connection()
    else:
        yield get_connection()


def execute(sql: str, params: Sequence[Any] | dict = ()) -> sqlite3.Cursor:
    """Execute one SQL statement and return the cursor."""
    with locked() as conn:
        return conn.execute(sql, params)


def fetch(sql: str, params: Sequence[Any] | dict = ()) -> list[sqlite3.Row]:
    """Execute a query and return all rows (fetched while holding the lock)."""
    with locked() as conn:
        return conn.execute(sql, params).fetchall()


def query(sql: str, params: Sequence[Any] | dict = ()) -> list[dict[str, Any]]:
    """Execute a query and return the rows as plain dicts."""
    return [dict(row) for row in fetch(sql, params)]


class _Transaction(ContextDecorator):
    """Context manager / decorator: BEGIN/COMMIT outermost, SAVEPOINTs when nested."""

    def __init__(self) -> None:
        self._frames: list[tuple[sqlite3.Connection, str | None, bool]] = []

    def _recreate_cm(self) -> _Transaction:
        return _Transaction()

    def __enter__(self) -> sqlite3.Connection:
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
                conn.execute("BEGIN IMMEDIATE")
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
    def _finish(conn: sqlite3.Connection, rollback: bool) -> None:
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
