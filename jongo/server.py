"""HTTP servers built on the standard library, plus the dev auto-reloader.

For production you can also point any WSGI server at your app: ``gunicorn app:app``.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import ServerHandler, WSGIRequestHandler, WSGIServer

from .errors import CompileError

log = logging.getLogger("jongo")
#: Exit status for "the port is taken": saving a file can't fix that, so the reloader stops too.
EXIT_PORT_IN_USE = 3
SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".jongo", ".pytest_cache", "site-packages"}


def color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stderr.isatty() else text


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


class _ServerHandler(ServerHandler):
    def log_exception(self, exc_info):
        if not isinstance(exc_info[1], (BrokenPipeError, ConnectionResetError, TimeoutError)):
            super().log_exception(exc_info)


class _RequestHandler(WSGIRequestHandler):
    # Seconds a socket read/write may stall. Without it a client that opens a connection
    # and never finishes its request (slowloris) holds a thread forever. Live streams are
    # unaffected: they send a keepalive every 15 seconds.
    timeout = 30

    def handle(self):
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.send_error(414)
                return
            if not self.parse_request():
                return
            handler = _ServerHandler(
                self.rfile, self.wfile, self.get_stderr(), self.get_environ(), multithread=True
            )
            handler.request_handler = self
            handler.run(self.server.get_app())
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def log_message(self, format, *args):
        pass


def setup_logging(dev: bool):
    from . import log as _log
    _log.configure(dev)


def auto_migrate(allow_destructive=False) -> None:
    from . import db

    if not db.models_registry:
        return
    for op, applied in db.migrate(allow_destructive=allow_destructive):
        if applied:
            log.info("migrated: %s", op.describe())
        else:
            log.warning("skipped destructive change: %s (run `jongo migrate --allow-destructive`)", op.describe())


def database_failure(exc: BaseException, doing: str) -> SystemExit:
    """Print a database error the way the CLI prints every error — code, message, hint — not a traceback."""
    from .errors import classify, hint_for
    from .monitor import redact

    code, hint = classify(exc)
    if code == "J000":
        code, hint = "J303", hint_for("J303")
    message = redact(str(exc)).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    sys.stderr.write(color("error: ", "31;1") + f"[{code}] couldn't {doing}: {message}\n")
    if hint:
        sys.stderr.write(color(f"  hint: {hint}\n", "2"))
    sys.stderr.write(color(f"  more: jongo explain {code} · jongo doctor checks the database\n", "2"))
    return SystemExit(1)


def warn_pending_migrations() -> None:
    """``jongo run`` without ``--migrate``: say so at start if the schema is behind the models."""
    from . import db

    if not db.models_registry:
        return
    try:
        plan = db.plan_migrations()
    except Exception as exc:
        from .errors import classify
        from .monitor import redact

        first_line = (redact(str(exc)).strip().splitlines() or [type(exc).__name__])[0]
        log.warning("couldn't check the schema [%s]: %s", classify(exc)[0], first_line)
        return
    if plan:
        listed = "; ".join(op.describe() for op in plan[:3]) + (f"; +{len(plan) - 3} more" if len(plan) > 3 else "")
        log.warning("%d pending migration(s) [J302]: %s. Run `jongo migrate`, or start with `jongo run --migrate`; "
                    "until then requests that touch them fail.", len(plan), listed)


def serve(app, host="127.0.0.1", port=8000, *, migrate=None):
    setup_logging(app.dev)
    from .diagnostics import declared_production

    if app.dev and declared_production():
        log.warning("[J502] dev mode is on but JONGO_ENV=production: error pages show tracebacks and source. "
                    "Unset JONGO_DEV / dev=True on a deployed server (/_jongo/status still needs the token).")
    if migrate or (migrate is None and app.dev):
        try:
            auto_migrate()
        except Exception as exc:
            raise database_failure(exc, "migrate the database") from None
    else:  # in the background: a slow or unreachable database must not delay binding the port
        threading.Thread(target=warn_pending_migrations, name="jongo-schema-check", daemon=True).start()
    try:
        app.check()
    except CompileError as exc:
        if not app.dev:
            raise
        log.error("%s\n%s", color("compile error", "31;1"), exc)

    try:
        server = ThreadingWSGIServer((host, port), _RequestHandler)
    except OSError as exc:
        if exc.errno in (48, 98):  # EADDRINUSE on macOS / Linux
            sys.stderr.write(color(f"\n  [J505] Port {port} is already in use. Try `--port {port + 1}`.\n", "31"))
            raise SystemExit(EXIT_PORT_IN_USE) from None
        raise
    server.set_app(app)
    from . import log as _log
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"
    sys.stderr.write(_log.banner(app.name, "dev" if app.dev else "production", url, color=sys.stderr.isatty()))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _snapshot(roots) -> dict:
    mtimes = {}
    for root in roots:
        mtimes.update(_snapshot_one(root))
    return mtimes


def _snapshot_one(root: Path) -> dict:
    mtimes = {}
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(directory, name)
                try:
                    mtimes[path] = os.stat(path).st_mtime
                except OSError:
                    pass
    return mtimes


def run_with_reloader(argv: list[str], root: Path) -> int:
    """Run ``argv`` in a child process and restart it whenever a watched .py file changes.

    Watches the project, plus Jongo itself when it's an editable checkout (not in site-packages).
    """
    env = {**os.environ, "JONGO_RUN_MAIN": "1", "JONGO_DEV": "1"}
    roots = [root]
    jongo_dir = Path(__file__).resolve().parent
    if "site-packages" not in jongo_dir.parts and not jongo_dir.is_relative_to(root):
        roots.append(jongo_dir)
    snapshot = _snapshot(roots)
    while True:
        child = subprocess.Popen(argv, env=env)
        crashed_reported = False
        try:
            while True:
                time.sleep(0.4)
                current = _snapshot(roots)
                if current != snapshot:
                    changed = sorted(set(current) ^ set(snapshot) | {p for p in current if snapshot.get(p) != current[p]})
                    snapshot = current
                    names = ", ".join(os.path.relpath(p, root) for p in changed[:3])
                    sys.stderr.write(color(f"\n  ↻ {names} changed, restarting…\n", "38;5;209"))
                    break
                if child.poll() == EXIT_PORT_IN_USE:
                    return 1
                if child.poll() is not None and not crashed_reported:
                    crashed_reported = True
                    sys.stderr.write(color("\n  The app stopped. Fix the error and save to restart.\n", "31"))
        except KeyboardInterrupt:
            child.terminate()
            try:
                child.wait(3)
            except subprocess.TimeoutExpired:
                child.kill()
            return 0
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


class _ServerModule(types.ModuleType):
    """This module, callable as the ``@server`` decorator.

    ``jongo.server`` names two things: the ``@server`` decorator (``from jongo import server``)
    and this module. Importing the module (``import jongo.server``, or Jongo's own CLI starting
    a server) makes Python set the package attribute ``jongo.server`` to the module, so code
    that runs ``from jongo import server`` afterwards gets the module. Making the module
    callable keeps ``@server`` and ``@server(login_required=True)`` working either way.
    """

    def __call__(self, *args, **kwargs):
        from .rpc import server

        return server(*args, **kwargs)


sys.modules[__name__].__class__ = _ServerModule
