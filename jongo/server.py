"""HTTP servers built on the standard library, plus the dev auto-reloader.

For production you can also point any WSGI server at your app: ``gunicorn app:app``.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import ServerHandler, WSGIRequestHandler, WSGIServer

from .errors import CompileError

log = logging.getLogger("jongo")
SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".jongo", ".pytest_cache", "site-packages"}


def color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stderr.isatty() else text


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


class _ServerHandler(ServerHandler):
    def log_exception(self, exc_info):
        if not isinstance(exc_info[1], (BrokenPipeError, ConnectionResetError)):
            super().log_exception(exc_info)


class _RequestHandler(WSGIRequestHandler):
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
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        pass


def setup_logging(dev: bool):
    if logging.getLogger().handlers or log.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(color("jongo", "38;5;209") + "  %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO if dev else logging.WARNING)
    log.propagate = False


def auto_migrate(allow_destructive=False) -> None:
    from . import db

    if not db.models_registry:
        return
    for op, applied in db.migrate(allow_destructive=allow_destructive):
        if applied:
            log.info("migrated: %s", op.describe())
        else:
            log.warning("skipped destructive change: %s (run `jongo migrate --allow-destructive`)", op.describe())


def serve(app, host="127.0.0.1", port=8000, *, migrate=None):
    setup_logging(app.dev)
    if migrate or (migrate is None and app.dev):
        auto_migrate()
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
            sys.stderr.write(color(f"\n  Port {port} is already in use. Try `--port {port + 1}`.\n", "31"))
            raise SystemExit(1) from None
        raise
    server.set_app(app)
    mode = color("dev", "38;5;209") if app.dev else color("production", "32")
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"
    sys.stderr.write(f"\n  {color('Jongo', '1')} {mode} server running at {color(url, '4')}\n  Press Ctrl+C to stop.\n\n")
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
                current = _snapshot(root)
                if current != snapshot:
                    changed = sorted(set(current) ^ set(snapshot) | {p for p in current if snapshot.get(p) != current[p]})
                    snapshot = current
                    names = ", ".join(os.path.relpath(p, root) for p in changed[:3])
                    sys.stderr.write(color(f"\n  ↻ {names} changed, restarting…\n", "38;5;209"))
                    break
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
