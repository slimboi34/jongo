"""Jongo's logging — one clear, beautiful stream for humans *and* AI agents.

Everything Jongo does at runtime flows through here: every request, every SQL
query, every server-function call, every compile, every error. The goal is that
you (or an AI agent debugging your app) can read the terminal top-to-bottom and
understand exactly what happened, in order, with timings — no guessing.

Two shapes, same data:

* **pretty** (default in a terminal) — aligned, colour-coded columns:

      12:34:56.789  INFO   http    a3f2  GET /todos → 200   12.4ms  sql=3·2.1ms
      12:34:56.781  DEBUG  sql     a3f2  SELECT * FROM todos ORDER BY id   1.4ms ·8 rows
      12:34:56.902  ERROR  http    b1c7  POST /todos → 500   3.0ms
                    ✗ IntegrityError: UNIQUE constraint failed: todos.title
                      app.py:41  in add_todo   →  Todo.create(title=title)

* **json** (default when the stream is piped, or ``JONGO_LOG=json``) — one JSON
  object per line, every field structured, ready for an agent to parse.

Design notes for anyone (human or model) extending Jongo:

* Get a domain logger with :func:`get_logger` — e.g. ``get_logger("sql")`` is the
  standard ``logging.getLogger("jongo.sql")``. Domains: http, sql, db, rpc,
  render, compile, server, auth, admin.
* Attach structured fields with the ``extra=fields(...)`` helper. They show up as
  ``key=value`` in pretty mode and as real keys in json mode. Never invent your
  own ``extra`` keys — collisions with stdlib record attributes bite.
* Requests are correlated by a short **request id** (the ``a3f2`` above), carried
  in a :mod:`contextvars` var so *every* log line emitted while handling a
  request is tagged with it automatically. Use :func:`request_scope`.
* Per-request counters (SQL count + time) live in the same context, so the one
  request-summary line can tell you "this endpoint ran 12 queries" — the single
  most useful number for spotting N+1s.

Zero third-party dependencies: just ``logging``, ``contextvars`` and ANSI.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import sys
import time
import traceback as _tb
from typing import Any, Iterator

ROOT = "jongo"

# ── request-scoped context ─────────────────────────────────────────────────
# Each HTTP request runs in its own thread with its own contextvar context, so
# these are naturally isolated per request.
_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("jongo_rid", default=None)
_stats: contextvars.ContextVar[dict | None] = contextvars.ContextVar("jongo_stats", default=None)

# ── ANSI palette ────────────────────────────────────────────────────────────
RESET = "\033[0m"
_LEVEL_STYLE = {
    "DEBUG": "38;5;245",           # grey
    "INFO": "38;5;39",             # blue
    "WARNING": "38;5;214",         # amber
    "ERROR": "38;5;203",           # red
    "CRITICAL": "1;97;48;5;203",   # white on red
}
_LEVEL_NAME = {"WARNING": "WARN", "CRITICAL": "CRIT"}
_DIM = "38;5;245"
_DOMAIN = "38;5;109"
_KEY = "38;5;108"
_RID_PALETTE = ["38;5;209", "38;5;42", "38;5;39", "38;5;170", "38;5;214",
                "38;5;79", "38;5;169", "38;5;111", "38;5;150", "38;5;203"]
# status → colour for the "→ 200" arrow
_STATUS_STYLE = {2: "38;5;42", 3: "38;5;39", 4: "38;5;214", 5: "38;5;203"}


def fields(**kw: Any) -> dict:
    """Wrap structured fields for a log call: ``logger.info("hi", extra=fields(user=3))``."""
    return {"_kv": kw}


def get_logger(domain: str = "") -> logging.Logger:
    """Return the ``jongo`` logger, or a ``jongo.<domain>`` child (http, sql, …)."""
    return logging.getLogger(f"{ROOT}.{domain}" if domain else ROOT)


# ── request lifecycle ─────────────────────────────────────────────────────────
@contextlib.contextmanager
def request_scope(rid: str | None = None) -> Iterator[str]:
    """Tag every log line emitted inside the block with a short request id, and
    reset the per-request SQL counters. Yields the request id."""
    rid = rid or os.urandom(2).hex()
    tok_id = _request_id.set(rid)
    tok_stats = _stats.set({"sql": 0, "sql_ms": 0.0})
    try:
        yield rid
    finally:
        _request_id.reset(tok_id)
        _stats.reset(tok_stats)


def record_sql(ms: float) -> None:
    """Count one data query against the current request (for its summary line)."""
    s = _stats.get()
    if s is not None:
        s["sql"] += 1
        s["sql_ms"] += ms


def current_stats() -> dict:
    return dict(_stats.get() or {"sql": 0, "sql_ms": 0.0})


def sql_enabled() -> bool:
    """True when it's worth timing SQL (dev/debug or an active request wants counts)."""
    return get_logger("sql").isEnabledFor(logging.DEBUG) or _stats.get() is not None


# ── domain helpers (keep call sites tiny and consistent) ──────────────────────
def request_done(method: str, path: str, status: int, ms: float, *, route: str | None = None) -> None:
    """The one-line summary that closes out a request."""
    s = current_stats()
    extra = {"method": method, "path": path, "status": status, "ms": round(ms, 1),
             "sql": s["sql"], "sql_ms": round(s["sql_ms"], 1)}
    if route:
        extra["route"] = route
    get_logger("http").info("%s %s → %s", method, path, status, extra=fields(**extra))


def sql(statement: str, ms: float, rows: int | None = None) -> None:
    """Log one SQL statement with its timing (DEBUG)."""
    logger = get_logger("sql")
    if not logger.isEnabledFor(logging.DEBUG):
        return
    one_line = " ".join(statement.split())
    extra = {"ms": round(ms, 2)}
    if rows is not None:
        extra["rows"] = rows
    logger.debug(one_line, extra=fields(**extra))


def rpc_call(name: str, ms: float, ok: bool, error: str | None = None) -> None:
    logger = get_logger("rpc")
    extra = {"fn": name, "ms": round(ms, 1), "ok": ok}
    if error:
        extra["error"] = error
    logger.log(logging.INFO if ok else logging.WARNING, "@server %s", name, extra=fields(**extra))


def compiled(components: int, kb: float, build_hash: str, ms: float) -> None:
    get_logger("compile").info(
        "compiled %d components → %.1f KB", components, kb,
        extra=fields(components=components, kb=round(kb, 1), build=build_hash, ms=round(ms, 1)))


# ── formatters ────────────────────────────────────────────────────────────────
def _rid_style(rid: str) -> str:
    return _RID_PALETTE[sum(rid.encode()) % len(_RID_PALETTE)]


class PrettyFormatter(logging.Formatter):
    """Aligned, colour-coded, human-first — but still column-regular so an agent
    can parse it. Renders exceptions as a compact, app-frame-highlighted trace."""

    def __init__(self, color: bool = True):
        super().__init__()
        self.color = color

    def _c(self, text: str, style: str) -> str:
        return f"\033[{style}m{text}{RESET}" if self.color else text

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        ts = f"{ts}.{int(record.msecs):03d}"
        levelname = _LEVEL_NAME.get(record.levelname, record.levelname)
        domain = record.name[len(ROOT) + 1:] if record.name.startswith(ROOT + ".") else record.name
        rid = getattr(record, "_rid", None) or _request_id.get()

        head = (
            self._c(ts, _DIM) + "  "
            + self._c(f"{levelname:<5}", _LEVEL_STYLE.get(record.levelname, "0")) + "  "
            + self._c(f"{domain[:7]:<7}", _DOMAIN) + "  "
            + (self._c(f"{rid:<4}", _rid_style(rid)) if rid else "    ") + "  "
        )
        msg = record.getMessage()

        kv = getattr(record, "_kv", None) or {}
        # Special, prettier rendering for the request-summary line.
        if record.name == f"{ROOT}.http" and "status" in kv:
            msg = self._request_line(kv)
            kv = {}
        tail = self._render_kv(kv)

        line = head + msg + tail
        if record.exc_info:
            line += "\n" + self._exception(record.exc_info)
        elif record.levelno >= logging.ERROR and getattr(record, "stack_info", None):
            line += "\n" + self._c(record.stack_info, _DIM)
        return line

    def _request_line(self, kv: dict) -> str:
        status = kv["status"]
        arrow = self._c(f"→ {status}", _STATUS_STYLE.get(status // 100, _DIM))
        out = f"{kv['method']} {kv['path']}  {arrow}   {self._c(f'{kv[\"ms\"]}ms', _DIM)}"
        if kv.get("sql"):
            out += self._c(f"  sql={kv['sql']}·{kv['sql_ms']}ms", _DIM)
        if kv.get("route"):
            out += self._c(f"  route={kv['route']}", _DIM)
        return out

    def _render_kv(self, kv: dict) -> str:
        if not kv:
            return ""
        parts = [self._c(f"{k}=", _KEY) + str(v) for k, v in kv.items()]
        return "  " + self._c("·", _DIM) + " " + "  ".join(parts)

    def _exception(self, exc_info) -> str:
        etype, evalue, tb = exc_info
        lines = [self._c(f"    ✗ {etype.__name__}: {evalue}", "1;38;5;203")]
        for fr in _tb.extract_tb(tb):
            where = f"{os.path.basename(fr.filename)}:{fr.lineno}  in {fr.name}"
            app_frame = "site-packages" not in fr.filename and not fr.filename.startswith(sys.prefix)
            style = "38;5;252" if app_frame else _DIM
            src = f"   →  {fr.line}" if (fr.line and app_frame) else ""
            lines.append(self._c(f"      {where}", style) + self._c(src, _DIM))
        return "\n".join(lines)


class JsonFormatter(logging.Formatter):
    """One JSON object per line — every field structured, for machine/agent consumers."""

    def format(self, record: logging.LogRecord) -> str:
        domain = record.name[len(ROOT) + 1:] if record.name.startswith(ROOT + ".") else record.name
        obj: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)) + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "domain": domain,
            "msg": record.getMessage(),
        }
        rid = getattr(record, "_rid", None) or _request_id.get()
        if rid:
            obj["rid"] = rid
        obj.update(getattr(record, "_kv", None) or {})
        if record.exc_info:
            etype, evalue, tb = record.exc_info
            obj["error"] = {"type": etype.__name__, "message": str(evalue),
                            "traceback": _tb.format_exception(etype, evalue, tb)}
        return json.dumps(obj, separators=(",", ":"), default=str)


class _ContextFilter(logging.Filter):
    """Freeze the current request id onto each record (so async handlers stay correct)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "_rid"):
            record._rid = _request_id.get()
        return True


# ── configuration ─────────────────────────────────────────────────────────────
_configured = False


def _use_color(stream, mode: str) -> bool:
    if mode == "plain":
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR") is not None:
        return True
    return bool(getattr(stream, "isatty", lambda: False)())


def configure(dev: bool | None = None, *, level: int | str | None = None,
              stream=None, force: bool = False) -> None:
    """Install Jongo's log handler on the ``jongo`` logger tree. Idempotent.

    * ``dev`` — verbose (DEBUG for SQL, INFO for requests) vs quiet (WARNING).
    * ``JONGO_LOG`` env — ``pretty`` | ``json`` | ``plain`` (default: pretty on a
      TTY, json when piped).
    * ``JONGO_LOG_LEVEL`` env — overrides the level (DEBUG/INFO/WARNING/…).
    """
    global _configured
    root = get_logger()
    if (_configured or root.handlers) and not force:
        return
    for h in list(root.handlers):
        root.removeHandler(h)

    stream = stream or sys.stderr
    mode = os.environ.get("JONGO_LOG", "").lower()
    if mode not in ("pretty", "json", "plain"):
        mode = "pretty" if getattr(stream, "isatty", lambda: False)() else "json"

    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter() if mode == "json"
                         else PrettyFormatter(color=_use_color(stream, mode)))
    handler.addFilter(_ContextFilter())
    root.addHandler(handler)
    root.propagate = False

    if level is None:
        level = os.environ.get("JONGO_LOG_LEVEL") or (logging.DEBUG if dev else logging.WARNING)
    root.setLevel(level)
    # In dev, SQL is DEBUG-verbose; otherwise it stays quiet unless asked.
    get_logger("sql").setLevel(logging.DEBUG if (dev and "JONGO_LOG_LEVEL" not in os.environ) else logging.NOTSET)
    _configured = True


def banner(app_name: str, mode: str, url: str, *, color: bool = True) -> str:
    """A tidy start-up banner for the dev/prod server."""
    def c(t, s):
        return f"\033[{s}m{t}{RESET}" if color else t
    mode_style = "38;5;209" if mode == "dev" else "38;5;42"
    bar = c("─" * 46, _DIM)
    return (
        f"\n  {bar}\n"
        f"  {c('▲ Jongo', '1;38;5;209')}  {c(mode + ' server', mode_style)}\n"
        f"  {c('→', _DIM)} {c(url, '4;38;5;39')}\n"
        f"  {c('logs', _DIM)} {c(os.environ.get('JONGO_LOG', 'pretty'), _DIM)}"
        f"   {c('stop', _DIM)} Ctrl+C\n"
        f"  {bar}\n"
    )
