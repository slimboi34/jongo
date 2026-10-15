"""The live system view: what the running app has been doing, recorded in-process.

    from jongo import status
    status(app)        # -> dict: recent requests, errors, per-route counters, channels, SQL

    GET /_jongo/status                       # the same as JSON (dev; admin or token in production)
    jongo status --url http://127.0.0.1:8000 # read it from a running server

Every request the app handles is appended to a fixed-size ring buffer, every unhandled
error (with its catalogue code, a short traceback and the request id that ties it to the
log line) to another, and each route keeps a running count, error count and timing. It
is all standard library, guarded by one lock, and costs one short critical section per
request — no I/O, no formatting until someone asks.

What is never recorded: query strings (they carry tokens), headers, cookies, bodies,
form values, SQL parameters, the database URL. Error messages are redacted of
``scheme://user:password@`` credentials and truncated.
"""
from __future__ import annotations

import os
import platform
import re
import threading
import time
from collections import deque

from .errors import CATALOGUE, app_location, classify, short_traceback

__all__ = ["Monitor", "status"]

_CREDENTIALS = re.compile(r"(://[^:/@\s]*:)[^@\s]*@")
_MAX_MESSAGE = 300
_MAX_PATH = 200


def redact(text: str, limit: int = _MAX_MESSAGE) -> str:
    text = _CREDENTIALS.sub(r"\1***@", text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Monitor:
    """Ring buffers and counters for one app. Thread-safe; cheap to write, formatted on read."""

    def __init__(self, max_requests: int = 200, max_errors: int = 50, max_n_plus_one: int = 20):
        self._lock = threading.Lock()
        self.started = time.time()
        self.requests: deque = deque(maxlen=max_requests)
        self.errors: deque = deque(maxlen=max_errors)
        self.n_plus_one: deque = deque(maxlen=max_n_plus_one)
        self.routes: dict[str, list] = {}   # pattern -> [count, errors, total_ms, max_ms, sql]
        self.total = 0
        self.by_class = [0, 0, 0, 0, 0, 0]  # index = status // 100
        self.sql_total = 0

    # -- writing (the hot path) --------------------------------------------------------------

    def record_request(self, method: str, path: str, status: int, ms: float, rid: str | None,
                       route: str | None, sql: int = 0, sql_ms: float = 0.0, repeated=None,
                       code: str | None = None) -> None:
        now = time.time()
        key = route or "(no route)"
        with self._lock:
            self.total += 1
            self.by_class[status // 100 if 0 < status < 600 else 5] += 1
            self.requests.append((now, method, path, status, ms, rid, key, sql, code))
            counters = self.routes.get(key)
            if counters is None:
                counters = self.routes[key] = [0, 0, 0.0, 0.0, 0]
            counters[0] += 1
            if status >= 500:
                counters[1] += 1
            counters[2] += ms
            if ms > counters[3]:
                counters[3] = ms
            counters[4] += sql
            self.sql_total += sql
            if repeated is not None:
                self.n_plus_one.append((now, method, path, rid, repeated[0], repeated[1], sql))

    def record_error(self, exc: BaseException, *, rid: str | None, method: str | None, path: str | None,
                     root=None, code: str | None = None, hint: str | None = None) -> dict:
        """Remember an unhandled error. Returns the stored entry."""
        if code is None:
            code, hint = classify(exc)
        file, line = app_location(exc, root)
        entry = {
            "time": time.time(),
            "code": code,
            "title": CATALOGUE.get(code, {}).get("title"),
            "type": type(exc).__name__,
            "message": redact(str(exc) or type(exc).__name__),
            "hint": hint,
            "file": file,
            "line": line,
            "traceback": short_traceback(exc, root=root),
            "request_id": rid,
            "method": method,
            "path": (path or "")[:_MAX_PATH] or None,
        }
        with self._lock:
            self.errors.append(entry)
        return entry

    def clear(self) -> None:
        with self._lock:
            self.requests.clear()
            self.errors.clear()
            self.n_plus_one.clear()
            self.routes.clear()
            self.total = self.sql_total = 0
            self.by_class = [0, 0, 0, 0, 0, 0]

    # -- reading -----------------------------------------------------------------------------

    def snapshot(self, recent: int = 50) -> dict:
        with self._lock:
            requests = list(self.requests)[-recent:]
            errors = list(self.errors)
            n_plus_one = list(self.n_plus_one)
            routes = {k: list(v) for k, v in self.routes.items()}
            total, by_class, sql_total = self.total, list(self.by_class), self.sql_total
        return {
            "requests": {
                "total": total,
                "by_status": {f"{i}xx": n for i, n in enumerate(by_class) if n and i},
                "server_errors": by_class[5],
                "recent": [
                    {"time": _iso(t), "method": m, "path": p, "status": s, "ms": round(ms, 1),
                     "request_id": rid, "route": route, "sql": sql, "code": code}
                    for (t, m, p, s, ms, rid, route, sql, code) in reversed(requests)
                ],
            },
            "routes": sorted(
                ({"route": k, "count": c, "errors": e, "avg_ms": round(tot / c, 1) if c else 0.0,
                  "max_ms": round(mx, 1), "sql": q}
                 for k, (c, e, tot, mx, q) in routes.items()),
                key=lambda r: -r["count"],
            ),
            "errors": [{**e, "time": _iso(e["time"])} for e in reversed(errors)],
            "sql": {
                "queries": sql_total,
                "n_plus_one": [
                    {"time": _iso(t), "method": m, "path": p, "request_id": rid,
                     "statement": " ".join(stmt.split())[:200], "repeated": count, "queries": sql}
                    for (t, m, p, rid, stmt, count, sql) in reversed(n_plus_one)
                ],
            },
        }


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"


def status(app, *, recent: int = 50) -> dict:
    """The app's live state as a dict: what ``/_jongo/status`` and ``jongo status`` show."""
    from . import __version__
    from . import log as jlog
    from .live import hub

    snap = app.monitor.snapshot(recent=recent)
    try:
        from .db import connection

        backend = connection.backend()
    except Exception:
        backend = None
    with hub._lock:
        per_channel = {name: len(listeners) for name, listeners in hub._channels.items() if listeners}
        subscribers = len({s for listeners in hub._channels.values() for s in listeners})
    top = dict(sorted(per_channel.items(), key=lambda kv: -kv[1])[:20])
    snap["sql"]["timing"] = jlog.sql_enabled()
    return {
        "jongo": __version__,
        "app": app.name,
        "mode": "dev" if app.dev else "production",
        "pid": os.getpid(),
        "python": platform.python_version(),
        "started": _iso(app.monitor.started),
        "uptime_s": round(time.time() - app.monitor.started, 1),
        "database": {"backend": backend},
        **snap,
        "channels": {
            "declared": [ch.pattern for ch in app.channels],
            "subscribers": subscribers,
            "active": top,
        },
    }
