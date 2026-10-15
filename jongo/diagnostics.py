"""``jongo doctor``: one report on whether this app, here, is healthy — and what to do if not.

    from jongo import doctor
    report = doctor(app)            # {"ok": bool, "summary": {...}, "checks": [...]}
    report = doctor(app, production=True)   # judge it as a production deploy

    jongo doctor [--json] [--production] [--port N]

Each check is ``{id, status, message, hint}`` with ``status`` one of ``ok``, ``warn`` or
``fail`` and, where the problem has one, the catalogue ``code`` (``jongo explain CODE``)
and a ``file``/``line``. ``ok`` is false when any check fails; the CLI then exits 1.

The doctor only reads: it never creates a database file, applies a migration or writes
a key. Nothing secret is reported — database URLs are shown without their password.
"""
from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from .errors import CompileError, hint_for

__all__ = ["doctor", "check_result", "environment_checks", "format_report"]

MIN_SECRET_LENGTH = 32


def check_result(id: str, status: str, message: str, hint: str | None = None, code: str | None = None,
                 **extra) -> dict:
    out = {"id": id, "status": status, "message": message, "hint": hint}
    if code:
        out["code"] = code
        if out["hint"] is None and status != "ok":
            out["hint"] = hint_for(code)
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


# -------------------------------------------------------------------------- environment --


def environment_checks(port: int | None = None) -> list[dict]:
    """Checks that need no app: Python, Jongo, the port, node."""
    from . import __version__

    checks = []
    version = platform.python_version()
    if sys.version_info >= (3, 10):
        checks.append(check_result("python", "ok", f"Python {version} ({sys.executable})"))
    else:
        checks.append(check_result("python", "fail", f"Python {version} is older than 3.10", code="J511"))
    checks.append(check_result("jongo", "ok", f"jongo {__version__} ({Path(__file__).resolve().parent})"))
    if port is not None:
        checks.append(_port_check(port))
    node = shutil.which("node")
    if node:
        try:
            out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            out = "unknown version"
        checks.append(check_result("node", "ok", f"node {out} found (only Jongo's own parity tests use it)"))
    else:
        checks.append(check_result("node", "ok", "node not found — not needed: Jongo compiles components itself"))
    return checks


def _port_check(port: int) -> dict:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return check_result("port", "warn", f"port {port} is already in use (fine if it's this app running)",
                                f"pick another with --port {port + 1}", code="J505")
    return check_result("port", "ok", f"port {port} is free")


# -------------------------------------------------------------------------------- the app --


#: Environment variables that mean "this is a deployed server" (JONGO_ENV=production, or a platform's own).
PRODUCTION_SIGNALS = ("RAILWAY_ENVIRONMENT", "DYNO", "FLY_APP_NAME", "K_SERVICE", "RENDER", "VERCEL_ENV")


def is_production_environment(app) -> bool:
    """Judge as production when JONGO_ENV says so or a hosting platform's variables are set — never in dev mode."""
    if app.dev:
        return False
    env = os.environ.get("JONGO_ENV", "").lower()
    if env:
        return env in ("production", "prod")
    return any(os.environ.get(name) for name in PRODUCTION_SIGNALS)


def doctor(app=None, *, production: bool | None = None, port: int | None = None, load_error: dict | None = None) -> dict:
    """Run every check and return the report.

    ``production`` (judge secrets, cookies and dev mode as a deploy) defaults to
    :func:`is_production_environment`: ``JONGO_ENV=production`` or a hosting platform's
    variables, and never while the app is in dev mode.
    """
    from . import __version__

    checks = environment_checks(port)
    if load_error is not None:
        checks.append(check_result("app", "fail", load_error["message"], load_error.get("hint"),
                                   code=load_error.get("code"), file=load_error.get("file"),
                                   line=load_error.get("line")))
    elif app is None:
        checks.append(check_result("app", "fail", "no app given", code="J503"))
    else:
        if production is None:
            production = is_production_environment(app)
        checks += _app_checks(app, production)
    summary = {s: sum(1 for c in checks if c["status"] == s) for s in ("ok", "warn", "fail")}
    return {
        "jongo": __version__,
        "app": getattr(app, "name", None),
        "production": bool(production),
        "ok": summary["fail"] == 0,
        "summary": summary,
        "checks": checks,
    }


def _app_checks(app, production: bool) -> list[dict]:
    from .introspect import describe

    checks = [check_result("app", "ok", f"loaded {app.name!r} from {app.root}")]

    # -- mode, secret, cookies, status endpoint -------------------------------------------
    if app.dev:
        if production:
            checks.append(check_result("dev_mode", "fail", "dev mode is on, and this is being checked as production",
                                       code="J502"))
        else:
            checks.append(check_result("dev_mode", "ok",
                                       "dev mode (production checks are skipped; run `jongo doctor --production`)"))
    elif production:
        checks.append(check_result("dev_mode", "ok", "production: dev mode is off"))
    else:
        checks.append(check_result("dev_mode", "ok", "local: dev mode is off and no production environment detected "
                                   "(production checks: `jongo doctor --production` or JONGO_ENV=production)"))

    source = getattr(app, "secret_source", None) or "unknown"
    length = getattr(app, "secret_length", None)
    if not production:
        checks.append(check_result("secret_key", "ok", f"secret key from {source} (not judged as production)"))
    elif source == "ephemeral":
        checks.append(check_result("secret_key", "fail",
                                   "no secret key: a random one is used, so sessions reset on every restart",
                                   code="J501"))
    elif source == "file":
        checks.append(check_result("secret_key", "warn",
                                   "secret key read from .jongo/secret; a fresh container or a second machine "
                                   "gets a different one (everyone is signed out on deploy)", code="J501"))
    elif length is not None and length < MIN_SECRET_LENGTH:
        checks.append(check_result("secret_key", "fail",
                                   f"secret key from {source} is only {length} characters (use {MIN_SECRET_LENGTH}+)",
                                   code="J501"))
    else:
        checks.append(check_result("secret_key", "ok", f"secret key from {source}"
                                   + (f" ({length} characters)" if length else "")))

    if not production:
        checks.append(check_result("cookies", "ok", "cookie security checks are for production"))
    elif app.trust_proxy:
        checks.append(check_result("cookies", "ok",
                                   "trust_proxy=True: cookies are Secure when the proxy says the request was HTTPS"))
    else:
        checks.append(check_result("cookies", "warn",
                                   "session cookies are marked Secure only when the app itself sees HTTPS; behind "
                                   "a TLS-terminating proxy they won't be", code="J507"))

    token = os.environ.get("JONGO_STATUS_TOKEN") or ""
    if app.dev:
        checks.append(check_result("status_endpoint", "ok", "/_jongo/status is open (dev mode)"))
    elif token and len(token) < 16:
        checks.append(check_result("status_endpoint", "warn",
                                   "JONGO_STATUS_TOKEN is shorter than 16 characters, so it is ignored",
                                   "use a long random token", code="J508"))
    else:
        how = "an admin session or the JONGO_STATUS_TOKEN bearer token" if token else "an admin session"
        checks.append(check_result("status_endpoint", "ok", f"/_jongo/status needs {how}"))

    # -- code: compile + server functions ------------------------------------------------------
    try:
        app.check()
        from .vdom import COMPONENTS

        checks.append(check_result("components", "ok", f"{len(COMPONENTS)} components compile"))
    except CompileError as exc:
        file = exc.filename
        if file:
            try:
                file = str(Path(file).resolve().relative_to(app.root))
            except (ValueError, OSError):
                pass
        checks.append(check_result("components", "fail", exc.message, exc.hint, code=exc.code,
                                   file=file, line=exc.lineno))
    except Exception as exc:  # a component that fails in some other way still gets reported
        from .errors import error_dict

        d = error_dict(exc, app.root)
        checks.append(check_result("components", "fail", d["message"], d["hint"], code=d["code"],
                                   file=d.get("file"), line=d.get("line")))

    d = describe(app)
    broken = [s for s in d["server_functions"] if s.get("error")]
    for s in broken:
        checks.append(check_result("server_functions", "fail", s["error"], code="J405", file=s["file"], line=s["line"]))
    if not broken:
        checks.append(check_result("server_functions", "ok",
                                   f"{len(d['server_functions'])} server functions; every type hint resolves"))

    # -- database --------------------------------------------------------------------------------
    checks += _database_checks(app, d)

    # -- static ----------------------------------------------------------------------------------
    static = app.root / "static"
    if static.is_dir():
        checks.append(check_result("static", "ok", f"{app.static_url}/ serves {static}"))
    elif _mentions_static(app):
        checks.append(check_result("static", "warn",
                                   f"the app references {app.static_url}/ but {static} doesn't exist", code="J506"))
    else:
        checks.append(check_result("static", "ok", "no static/ directory (only needed for static files)"))
    return checks


def _mentions_static(app) -> bool:
    needle = app.static_url + "/"
    if any(needle in str(item) for item in app.head):
        return True
    for path in app.root.glob("*.py"):
        try:
            if needle in path.read_text(errors="ignore"):
                return True
        except OSError:
            continue
    return False


def sqlite_location_problem(path: Path) -> str | None:
    """Why a SQLite file that doesn't exist yet couldn't be created there, or None if it could."""
    if path.parent.is_dir():
        if os.access(path.parent, os.W_OK):
            return None
        return f"the directory {path.parent} exists but isn't writable, so the database file can't be created"
    existing = path.parent
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if not existing.is_dir():
        return f"{path.parent} can't be created: {existing} is a file, not a directory"
    if not os.access(existing, os.W_OK):
        return f"{path.parent} doesn't exist and can't be created ({existing} isn't writable)"
    return None


def _database_checks(app, d: dict) -> list[dict]:
    from . import db
    from .db import connection

    checks = []
    try:
        backend = connection.backend()
        target = connection.database_path()
    except Exception as exc:
        from .errors import classify

        code, hint = classify(exc)
        return [check_result("database", "fail", str(exc), hint, code=code if code != "J000" else "J309")]

    shown = connection._redact(target) if backend == "postgres" else target
    if backend == "sqlite" and target != ":memory:":
        path = Path(target)
        if not path.exists():
            problem = sqlite_location_problem(path)
            if problem:
                return [check_result("database", "fail", f"SQLite {shown}: {problem}", code="J301")]
            return [check_result("database", "ok", f"SQLite {shown} doesn't exist yet; the first migrate creates it"),
                    check_result("migrations", "ok" if not db.models_registry else "warn",
                                 "no database yet" + (": `jongo migrate` will create the tables"
                                                      if db.models_registry else ""),
                                 code="J302" if db.models_registry else None)]
    try:
        db.query("SELECT 1 AS one")
    except Exception as exc:
        from .errors import classify
        from .monitor import redact

        code, hint = classify(exc)
        return [check_result("database", "fail", f"{backend} {shown}: {redact(str(exc))}", hint,
                             code=code if code != "J000" else "J301")]
    checks.append(check_result("database", "ok", f"{backend} {shown} is reachable"))

    try:
        plan = db.plan_migrations()
    except Exception as exc:
        from .errors import classify

        code, hint = classify(exc)
        checks.append(check_result("migrations", "fail", f"could not plan migrations: {exc}", hint,
                                   code=code if code != "J000" else "J303"))
        return checks
    if plan:
        listed = "; ".join(op.describe() for op in plan[:3]) + (f"; +{len(plan) - 3} more" if len(plan) > 3 else "")
        destructive = sum(1 for op in plan if op.destructive)
        checks.append(check_result(
            "migrations", "warn", f"{len(plan)} pending migration(s): {listed}"
            + (f" ({destructive} destructive)" if destructive else ""),
            "run `jongo migrate` (`--plan` to preview); `jongo dev` and `jongo run --migrate` apply them on start",
            code="J302", pending=[op.describe() for op in plan]))
    else:
        checks.append(check_result("migrations", "ok", "the schema matches the models"))

    if d["app"].get("admin"):
        auth = sys.modules.get("jongo.auth")  # never import it here: that would register its model
        try:
            admins = auth.User.filter(is_admin=True).count() if auth else None
        except Exception:
            admins = None
        if admins == 0:
            checks.append(check_result("admin", "warn", f"the admin is mounted at {d['app']['admin']} but no user "
                                       "has is_admin=True", code="J510"))
        elif admins:
            checks.append(check_result("admin", "ok", f"{admins} admin user(s) for {d['app']['admin']}"))
    return checks


# ---------------------------------------------------------------------------------- output --

_MARK = {"ok": ("✓", "32"), "warn": ("!", "33"), "fail": ("✗", "31;1")}


def format_report(report: dict, color: bool = False) -> str:
    def c(text, style):
        return f"\033[{style}m{text}\033[0m" if color else text

    lines = []
    for check in report["checks"]:
        mark, style = _MARK[check["status"]]
        code = f" [{check['code']}]" if check.get("code") and check["status"] != "ok" else ""
        where = f" ({check['file']}:{check['line']})" if check.get("file") else ""
        lines.append(f"  {c(mark, style)} {check['id']:<17}{check['message']}{where}{c(code, style)}")
        if check["status"] != "ok" and check.get("hint"):
            lines.append(c(f"      hint: {check['hint']}", "2"))
    s = report["summary"]
    verdict = c("healthy", "32;1") if report["ok"] and not s["warn"] else (
        c("healthy, with warnings", "33;1") if report["ok"] else c("problems found", "31;1"))
    lines.append("")
    lines.append(f"  {verdict}: {s['ok']} ok, {s['warn']} warning(s), {s['fail']} failure(s)"
                 + ("   · `jongo explain CODE` explains a code" if s["warn"] or s["fail"] else ""))
    return "\n".join(lines)
