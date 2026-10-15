"""The ``jongo`` command."""
from __future__ import annotations

import argparse
import getpass
import importlib
import os
import sys
import traceback
from pathlib import Path

from .errors import CompileError, JongoError, app_location, classify, hint_for

SCAFFOLD_DIR = Path(__file__).parent / "scaffold"


def _c(text, code):
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def die(message: str, code: int = 1, *, error: str | None = None, hint: str | None = None):
    """Print an error (with its catalogue code and hint) and exit."""
    tag = f"[{error}] " if error else ""
    print(_c("error: ", "31;1") + tag + message, file=sys.stderr)
    if hint:
        print(_c(f"  hint: {hint}", "2"), file=sys.stderr)
    if error:
        print(_c(f"  more: jongo explain {error}", "2"), file=sys.stderr)
    raise SystemExit(code)


class AppLoadError(JongoError):
    """The app couldn't be found or imported. ``to_dict()`` is the report the CLI prints."""


def _import_app(target: str | None):
    """Import the app; raise AppLoadError (with a code, a location and a hint) if that fails."""
    from .app import Jongo

    target = target or os.environ.get("JONGO_APP") or "app.py"
    target, _, attr = target.partition(":")
    if target.endswith(".py") or os.sep in target:
        path = Path(target).resolve()
        if not path.exists():
            raise AppLoadError(f"can't find {target}", code="J503",
                               hint="run this inside your project, pass the file, or create one with `jongo new mysite`")
        sys.path.insert(0, str(path.parent))
        os.chdir(path.parent)
        module_name = path.stem
    else:
        sys.path.insert(0, os.getcwd())
        module_name = target
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        code, hint = classify(exc)
        file, line = app_location(exc, os.getcwd())
        if code == "J000":
            code, hint = "J509", hint_for("J509")
            if isinstance(exc, ModuleNotFoundError) and exc.name:
                hint = (f"No module named {exc.name!r} in this Python ({sys.executable}): install it into this "
                        f"environment (`pip install {exc.name.split('.')[0]}`, if that's its package name), or "
                        "fix the import if it's a module of your own.")
        error = AppLoadError(f"{module_name} failed to import: {type(exc).__name__}: {exc}", code=code, hint=hint,
                             file=file, line=line)
        error.cause = exc
        raise error from exc
    if attr:
        app = getattr(module, attr, None)
        if not isinstance(app, Jongo):
            raise AppLoadError(f"{module_name}.{attr} isn't a Jongo app", code="J504")
        return app
    candidates = [v for v in vars(module).values() if isinstance(v, Jongo)]
    if not candidates:
        raise AppLoadError(f"{module_name} doesn't define a Jongo app (app = Jongo(__name__))", code="J504")
    return getattr(module, "app", None) if isinstance(getattr(module, "app", None), Jongo) else candidates[0]


def load_error_dict(exc: AppLoadError) -> dict:
    d = exc.to_dict()
    d["message"] = str(exc)
    return d


def load_app(target: str | None):
    """Import the app from ``app.py``, ``path/to/app.py``, ``module`` or ``module:attr``."""
    try:
        return _import_app(target)
    except AppLoadError as exc:
        cause = getattr(exc, "cause", None)
        if cause is not None and os.environ.get("JONGO_TRACEBACK") != "0":
            traceback.print_exception(cause)  # the full story first, as before; the summary below
        where = f" ({exc.file}:{exc.line})" if getattr(exc, "file", None) else ""
        die(f"{exc}{where}", error=exc.code, hint=exc.hint)


# ---------------------------------------------------------------------------


def cmd_new(args):
    target = Path(args.name).resolve()
    if target.exists() and any(target.iterdir()):
        die(f"{target} already exists and isn't empty")
    target.mkdir(parents=True, exist_ok=True)
    name = target.name
    for source in sorted(SCAFFOLD_DIR.rglob("*")):
        if source.is_dir() or "__pycache__" in source.parts:
            continue
        relative = source.relative_to(SCAFFOLD_DIR)
        dest = target / str(relative).replace("dot-", ".").replace(".tmpl", "")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(source.read_text().replace("{{name}}", name))
    print(f"\n  {_c('Created', '32;1')} {name}/\n")
    print(f"    cd {args.name}")
    print("    jongo dev\n")


def cmd_dev(args):
    if os.environ.get("JONGO_RUN_MAIN") != "1" and not args.no_reload:
        from .server import run_with_reloader

        target = Path(args.app or os.environ.get("JONGO_APP") or "app.py")
        root = (target.parent if target.suffix == ".py" else Path.cwd()).resolve()
        argv = [sys.executable, "-m", "jongo", "dev", *(sys.argv[2:])]
        raise SystemExit(run_with_reloader(argv, root))
    os.environ["JONGO_DEV"] = "1"
    app = load_app(args.app)
    app.dev = True
    app.run(host=args.host, port=args.port)


def cmd_run(args):
    app = load_app(args.app)
    app.run(host=args.host, port=args.port, migrate=args.migrate)


def cmd_migrate(args):
    load_app(args.app)
    from . import db
    from .server import database_failure

    try:
        _migrate(args, db)
    except Exception as exc:
        raise database_failure(exc, "plan the migrations" if args.plan else "migrate the database") from None


def _migrate(args, db):
    plan = db.plan_migrations()
    if not plan:
        print(_c("✓", "32;1"), "Database is up to date.")
        return
    if args.plan:
        for op in plan:
            flag = _c(" (destructive)", "31") if op.destructive else ""
            print(f"  • {op.describe()}{flag}")
            for sql in op.sql:
                print(_c(f"      {sql}", "2"))
        return
    for op, applied in db.migrate(allow_destructive=args.allow_destructive):
        if applied:
            print(_c("  ✓ ", "32") + op.describe())
        else:
            print(_c("  ✗ skipped ", "33") + op.describe() + _c("  (pass --allow-destructive)", "2"))


def cmd_routes(args):
    app = load_app(args.app)
    if getattr(args, "json", False):
        import json

        from .introspect import describe

        d = describe(app)
        print(json.dumps({"pages": d["pages"], "routes": d["routes"]}, indent=2))
        return
    rows = [
        (",".join(sorted(r.methods - {"HEAD"})), r.path, r.kind, r.name)
        for r in app.router.routes
        if r.kind != "internal" or args.all
    ]
    widths = [max(len(row[i]) for row in rows + [("METHODS", "PATH", "KIND", "NAME")]) for i in range(4)]
    header = ("METHODS", "PATH", "KIND", "NAME")
    print(_c("  ".join(h.ljust(w) for h, w in zip(header, widths)), "1"))
    for row in rows:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths)))


def cmd_shell(args):
    import code

    app = load_app(args.app)
    from . import db

    namespace = {"app": app, "db": db, **db.models_registry}
    banner = f"Jongo shell. Available: app, db, {', '.join(sorted(db.models_registry)) or 'no models yet'}"
    code.interact(banner=banner, local=namespace)


def cmd_createadmin(args):
    load_app(args.app)
    from . import auth, db
    from .server import auto_migrate, database_failure

    try:
        auto_migrate()
    except Exception as exc:
        raise database_failure(exc, "migrate the database") from None
    username = args.username or input("Username: ").strip()
    if not username:
        die("a username is required")
    if auth.User.filter(username=username).exists():
        die(f"user {username!r} already exists")
    email = args.email if args.email is not None else input("Email (optional): ").strip()
    password = args.password or os.environ.get("JONGO_ADMIN_PASSWORD")
    while not password:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Password (again): "):
            print("Passwords didn't match.")
            password = None
    try:
        auth.User.create_user(username, password, email=email, is_admin=True)
    except db.ValidationError as exc:
        die(str(exc))
    print(_c("✓", "32;1"), f"Created admin {username!r}. Sign in at /admin")


def cmd_build(args):
    app = load_app(args.app)
    try:
        app.check()
    except CompileError as exc:
        print(_c("Compile error\n", "31;1") + str(exc), file=sys.stderr)
        print(_c(f"  more: jongo explain {exc.code}", "2"), file=sys.stderr)
        raise SystemExit(1)
    js, build_hash = app.bundle()
    out = Path(app.root) / ".jongo" / "build"
    out.mkdir(parents=True, exist_ok=True)
    (out / "app.js").write_text(js)
    from .vdom import COMPONENTS

    print(_c("✓", "32;1"), f"compiled {len(COMPONENTS)} components → .jongo/build/app.js ({len(js) / 1024:.1f} KB, build {build_hash})")


def cmd_check(args):
    """Compile, resolve, plan; report problems as data. The command an agent runs after every change."""
    import json

    from .introspect import check

    if args.json:
        try:
            app = _import_app(args.app)
        except AppLoadError as exc:
            from . import __version__

            problem = {"kind": "load", **load_error_dict(exc)}
            print(json.dumps({"jongo": __version__, "app": None, "ok": False, "problems": [problem],
                              "counts": {}, "pending_migrations": []}, indent=2))
            raise SystemExit(1)
    else:
        app = load_app(args.app)
    report = check(app)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        c = report["counts"]
        print(f"  {c['components']} components, {c['server_functions']} server functions, {c['pages']} pages, "
              f"{c['routes']} routes, {c['models']} models, {c['channels']} channels")
        for op in report["pending_migrations"]:
            flag = _c(" (destructive)", "31") if op["destructive"] else ""
            print(f"  • pending migration: {op['describe']}{flag}")
        for p in report["problems"]:
            where = f" ({p['file']}:{p['line']})" if p.get("file") else ""
            code = f" [{p['code']}]" if p.get("code") else ""
            print(_c(f"✗ {p['kind']}{code}", "31;1") + f"{where}: {p['message']}")
            if p.get("source"):
                print(_c(f"      {p['source'].strip()}", "2"))
            if p.get("hint"):
                print(_c(f"      hint: {p['hint']}", "2"))
        if not report["ok"]:
            print(_c("  `jongo explain CODE` explains a code; `jongo doctor` checks the whole setup.", "2"))
        if report["ok"]:
            print(_c("✓", "32;1"), "Everything compiles, every server function resolves, "
                  + ("the schema is up to date." if not report["pending_migrations"] else "migrations are pending."))
    raise SystemExit(0 if report["ok"] else 1)


def cmd_context(args):
    """The context pack: the framework's rules plus this app's map, generated from the running code."""
    import json

    from .introspect import context, describe

    app = load_app(args.app)
    text = json.dumps(describe(app), indent=2) if args.json else context(app)
    if args.output:
        Path(args.output).write_text(text.rstrip("\n") + "\n")
        print(_c("✓", "32;1"), f"wrote {args.output}")
    else:
        print(text, end="" if text.endswith("\n") else "\n")


def cmd_doctor(args):
    """One report: is this app, on this machine, healthy? Exit 1 on any failure."""
    import json

    from .diagnostics import doctor, format_report

    try:
        app, load_error = _import_app(args.app), None
    except AppLoadError as exc:
        app, load_error = None, load_error_dict(exc)
    report = doctor(app, production=True if args.production else None, port=args.port, load_error=load_error)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        mode = "production" if report["production"] else "dev"
        print(f"  jongo doctor · {report['app'] or 'no app'} · checked as {mode}\n")
        print(format_report(report, color=sys.stdout.isatty()))
    raise SystemExit(0 if report["ok"] else 1)


def cmd_explain(args):
    """What an error code means and how to fix it."""
    import json

    from .errors import CATALOGUE, CATEGORIES, explain, format_explain

    if args.list or not args.code:
        entries = [explain(code) for code in sorted(CATALOGUE)]
        if args.json:
            print(json.dumps(entries, indent=2))
            return
        category = None
        for e in entries:
            if e["category"] != category:
                category = e["category"]
                print(_c(f"\n  {category}", "1"))
            print(f"    {e['code']}  {e['title']}")
        print(_c("\n  jongo explain CODE for the explanation and the fix.", "2"))
        return
    entry = explain(args.code)
    if entry is None:
        die(f"unknown error code {args.code!r}", hint="`jongo explain --list` lists every code")
    if args.json:
        print(json.dumps(entry, indent=2))
    else:
        print(format_explain(args.code))


def cmd_status(args):
    """Read /_jongo/status from a running server: recent requests, errors, routes, channels."""
    import json
    import urllib.error
    import urllib.request

    base = args.url.strip().rstrip("/")
    if "://" not in base:  # `--url localhost:8771` or `--url :8771` means http://
        base = "http://" + (base if not base.startswith(":") else "127.0.0.1" + base)
    url = base + "/_jongo/status"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    token = args.token or os.environ.get("JONGO_STATUS_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            data = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read().decode()).get("error", {})
        except Exception:
            error = {}
        code = error.get("code") or ("J508" if exc.code in (401, 403) else "J512")
        if exc.code == 404:
            code, message = "J512", f"{url} returned 404: not a Jongo 0.5+ server, or JONGO_STATUS is off"
        else:
            message = f"{url} refused: {exc.code} {error.get('message') or exc.reason}"
        _status_fail(args, code, message)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        _status_fail(args, "J512", f"can't reach {url}: {reason}")
    if args.json:
        print(json.dumps(data, indent=2))
        return
    print(format_status(data, color=sys.stdout.isatty()))


def _status_fail(args, code, message):
    import json

    if args.json:
        print(json.dumps({"ok": False, "error": {"code": code, "message": message, "hint": hint_for(code)}}, indent=2))
        raise SystemExit(1)
    die(message, error=code, hint=hint_for(code))


def format_status(data: dict, color: bool = False) -> str:
    def c(text, style):
        return f"\033[{style}m{text}\033[0m" if color else text

    r = data["requests"]
    by_status = "  ".join(f"{k} {v}" for k, v in r["by_status"].items()) or "none yet"
    lines = [
        f"  {c(data['app'], '1')} · jongo {data['jongo']} · {data['mode']} · pid {data['pid']} · "
        f"up {data['uptime_s']:.0f}s · {data['database']['backend'] or 'no database'}",
        f"  requests: {r['total']}  ({by_status})   channels: {data['channels']['subscribers']} subscriber(s)",
    ]
    if data["routes"]:
        lines += ["", c("  routes", "1")]
        for route in data["routes"][:15]:
            errors = c(f"  {route['errors']} errors", "31") if route["errors"] else ""
            lines.append(f"    {route['count']:>6}×  {route['route']:<32} avg {route['avg_ms']}ms  "
                         f"max {route['max_ms']}ms{errors}")
    if data["errors"]:
        lines += ["", c("  recent errors", "1")]
        for e in data["errors"][:10]:
            where = f" at {e['file']}:{e['line']}" if e.get("file") else ""
            lines.append(c(f"    [{e['code']}] {e['type']}: {e['message']}", "31")
                         + f"  ({e['method']} {e['path']}, request {e['request_id']}){where}")
            if e.get("hint"):
                lines.append(c(f"        hint: {e['hint']}", "2"))
    if data["sql"]["n_plus_one"]:
        lines += ["", c("  likely N+1 queries", "1")]
        for n in data["sql"]["n_plus_one"][:5]:
            lines.append(f"    {n['method']} {n['path']}: ran {n['repeated']}× {n['statement'][:90]}")
    if r["recent"]:
        lines += ["", c("  recent requests", "1")]
        for q in r["recent"][:10]:
            style = "31" if q["status"] >= 500 else ("33" if q["status"] >= 400 else "32")
            sql = f"  sql={q['sql']}" if q["sql"] else ""
            lines.append(f"    {q['time'][11:]}  {q['request_id'] or '':<8}  {q['method']:<6} {q['path']:<34} "
                         + c(str(q["status"]), style) + f"  {q['ms']}ms{sql}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="jongo", description="Full-stack Python web framework")
    from . import __version__

    parser.add_argument("--version", action="version", version=f"jongo {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    def add(name, fn, help_text, app_arg=True):
        p = sub.add_parser(name, help=help_text, description=help_text)
        if app_arg:
            p.add_argument("app", nargs="?", help="app file or module (default: app.py)")
        p.set_defaults(fn=fn)
        return p

    p = add("new", cmd_new, "create a new project", app_arg=False)
    p.add_argument("name")

    p = add("dev", cmd_dev, "run the dev server with auto-reload")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-reload", action="store_true")

    p = add("run", cmd_run, "run a production server")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    p.add_argument("--migrate", action="store_true", help="apply safe migrations on start")

    p = add("migrate", cmd_migrate, "sync the database schema with your models")
    p.add_argument("--plan", action="store_true", help="show changes without applying them")
    p.add_argument("--allow-destructive", action="store_true", help="also drop columns")

    p = add("routes", cmd_routes, "list routes")
    p.add_argument("--json", action="store_true", help="pages and routes as JSON, with parameters and guards")
    p.add_argument("--all", action="store_true", help="include Jongo's internal routes")

    add("shell", cmd_shell, "interactive Python shell with your models loaded")

    p = add("createadmin", cmd_createadmin, "create an admin user")
    p.add_argument("--username")
    p.add_argument("--email")
    p.add_argument("--password")

    add("build", cmd_build, "compile components and report errors")
    p = add("check", cmd_check, "compile, resolve type hints and plan migrations; report problems as data")
    p.add_argument("--json", action="store_true", help="the report as JSON (for a coding agent or CI)")
    p = add("context", cmd_context, "describe this app for an AI coding tool: the rules, then the app's map")
    p.add_argument("--json", action="store_true", help="the map as JSON instead of Markdown")
    p.add_argument("-o", "--output", metavar="FILE", help="write to FILE (e.g. AGENTS.md) instead of stdout")

    p = add("doctor", cmd_doctor, "check the app, the database, the settings and the environment; exit 1 on failure")
    p.add_argument("--json", action="store_true", help="the report as JSON")
    p.add_argument("--production", action="store_true", help="judge the settings as a production deploy")
    p.add_argument("--port", type=int, default=None, help="also check that this port is free")

    p = add("explain", cmd_explain, "explain an error code (J302) and how to fix it", app_arg=False)
    p.add_argument("code", nargs="?", help="an error code, e.g. J302")
    p.add_argument("--list", action="store_true", help="list every code")
    p.add_argument("--json", action="store_true", help="as JSON")

    p = add("status", cmd_status, "show what a running server is doing: requests, errors, routes, channels",
            app_arg=False)
    p.add_argument("--url", default=os.environ.get("JONGO_URL", "http://127.0.0.1:8000"),
                   help="the server's address (default http://127.0.0.1:8000)")
    p.add_argument("--token", help="the status token for a production server (default: $JONGO_STATUS_TOKEN)")
    p.add_argument("--json", action="store_true", help="the raw status JSON")
    p.add_argument("--timeout", type=float, default=5.0)

    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return
    args.fn(args)


if __name__ == "__main__":
    main()
