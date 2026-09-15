"""The ``jongo`` command."""
from __future__ import annotations

import argparse
import getpass
import importlib
import os
import sys
from pathlib import Path

from .errors import CompileError

SCAFFOLD_DIR = Path(__file__).parent / "scaffold"


def _c(text, code):
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def die(message: str, code: int = 1):
    print(_c("error: ", "31;1") + message, file=sys.stderr)
    raise SystemExit(code)


def load_app(target: str | None):
    """Import the app from ``app.py``, ``path/to/app.py``, ``module`` or ``module:attr``."""
    from .app import Jongo

    target = target or os.environ.get("JONGO_APP") or "app.py"
    target, _, attr = target.partition(":")
    if target.endswith(".py") or os.sep in target:
        path = Path(target).resolve()
        if not path.exists():
            die(f"can't find {target}. Run this inside your project, or create one with `jongo new mysite`.")
        sys.path.insert(0, str(path.parent))
        os.chdir(path.parent)
        module_name = path.stem
    else:
        sys.path.insert(0, os.getcwd())
        module_name = target
    module = importlib.import_module(module_name)
    if attr:
        return getattr(module, attr)
    candidates = [v for v in vars(module).values() if isinstance(v, Jongo)]
    if not candidates:
        die(f"{module_name} doesn't define a Jongo app (app = Jongo(__name__))")
    return getattr(module, "app", None) if isinstance(getattr(module, "app", None), Jongo) else candidates[0]


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
    from .server import auto_migrate

    auto_migrate()
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
        raise SystemExit(1)
    js, build_hash = app.bundle()
    out = Path(app.root) / ".jongo" / "build"
    out.mkdir(parents=True, exist_ok=True)
    (out / "app.js").write_text(js)
    from .vdom import COMPONENTS

    print(_c("✓", "32;1"), f"compiled {len(COMPONENTS)} components → .jongo/build/app.js ({len(js) / 1024:.1f} KB, build {build_hash})")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="jongo", description="Full-stack Python web framework")
    parser.add_argument("--version", action="version", version="jongo 0.1.0")
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
    p.add_argument("--all", action="store_true", help="include Jongo's internal routes")

    add("shell", cmd_shell, "interactive Python shell with your models loaded")

    p = add("createadmin", cmd_createadmin, "create an admin user")
    p.add_argument("--username")
    p.add_argument("--email")
    p.add_argument("--password")

    add("build", cmd_build, "compile components and report errors")

    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return
    args.fn(args)


if __name__ == "__main__":
    main()
