"""What an app can say about itself, for the person or the coding agent working on it.

    from jongo import describe, context
    describe(app)   # -> dict: pages, routes, server functions, models, components, channels
    context(app)    # -> str: a Markdown context pack — the framework's rules + this app's map

    jongo context [--json] [-o FILE]   # the same from the command line
    jongo check [--json]               # compile, resolve hints, plan migrations; problems as data

Everything here is read from the live objects — the router, the registries, the type
hints — so it cannot drift from the code. It exists because the author of a Jongo app is
increasingly a model: a model needs the map a new colleague would need, in one document
it can load at once, and it needs its mistakes reported as data it can act on.
"""
from __future__ import annotations

import ast
import inspect
import json
import sys
import textwrap
import typing
from pathlib import Path
from typing import Any

from .errors import CompileError, hint_for

__all__ = ["describe", "context", "check", "RULES"]

_FRAMEWORK = ("jongo", "jongo.")


def _belongs_to_app(obj) -> bool:
    """True for objects the app defined, False for the framework's own (admin, auth…)."""
    module = getattr(obj, "__module__", "") or ""
    return module != "jongo" and not module.startswith("jongo.")


def _where(obj, root: Path) -> tuple[str | None, int | None]:
    try:
        fn = inspect.unwrap(obj)
        file = inspect.getsourcefile(fn)
        line = inspect.getsourcelines(fn)[1]
    except (OSError, TypeError):
        return None, None
    if not file:
        return None, None
    try:
        rel = str(Path(file).resolve().relative_to(root))
    except ValueError:
        rel = file
    return rel, line


def _type_name(hint) -> str | None:
    if hint is None or hint is inspect.Parameter.empty:
        return None
    if getattr(hint, "__jongo_schema__", False):  # a forms.Schema: its name, not its contents
        return hint.__name__
    if hint is type(None):
        return "None"
    if isinstance(hint, type):
        return hint.__name__
    return str(hint).replace("typing.", "")


def _jsonable(value) -> Any:
    if value is inspect.Parameter.empty:
        return None
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def _signature(fn, hints: dict, name: str | None = None) -> str:
    parts = []
    for pname, p in inspect.signature(fn).parameters.items():
        if p.kind is p.VAR_POSITIONAL:
            parts.append(f"*{pname}")
            continue
        if p.kind is p.VAR_KEYWORD:
            parts.append(f"**{pname}")
            continue
        text = pname
        hint = _type_name(hints.get(pname))
        if hint:
            text += f": {hint}"
        if p.default is not p.empty:
            text += f" = {p.default!r}"
        parts.append(text)
    out = f"{name or fn.__name__}({', '.join(parts)})"
    ret = _type_name(hints.get("return"))
    return f"{out} -> {ret}" if ret else out


def _doc(obj) -> str | None:
    """The first line of the object's *own* docstring (a model must not inherit Model's)."""
    doc = vars(obj).get("__doc__") if isinstance(obj, type) else inspect.getdoc(obj)
    return doc.strip().splitlines()[0] if doc and doc.strip() else None


# ---------------------------------------------------------------------------- describe --


def describe(app) -> dict:
    """The app as data: every page, route, server function, model, component, channel and form schema.

    Read from the live registries, so it describes the code that is actually loaded.

    Returns:
        A JSON-able dict with the keys ``jongo`` (the version), ``app`` (name, title,
        database, admin path, layout, MCP path), ``models``, ``server_functions`` (each with
        its ``signature``, ``params``, ``rate_limit`` and a JSON Schema ``input_schema``),
        ``pages``, ``routes``, ``components``, ``channels`` and ``schemas`` (the
        ``forms.Schema`` definitions, with the functions that use each).

    Example::

        from jongo import describe

        for fn in describe(app)["server_functions"]:
            print(fn["signature"], fn["input_schema"]["required"])
    """
    from . import __version__
    from .db import connection
    from .db.fields import NOTHING, ForeignKey
    from .db.models import Model, models_registry
    from .rpc import SERVER_FUNCTIONS
    from .routing import _ANNOTATION_CONVERTERS, _PARAM
    from .schema import function_schema
    from .vdom import COMPONENTS

    root = app.root

    # -- pages and routes ------------------------------------------------------------------
    pages, routes, admin_paths = [], [], []
    for r in app.router.routes:
        handler_module = getattr(r.handler, "__module__", "") or ""
        if handler_module == "jongo.admin":
            admin_paths.append(r.path)
        if r.kind == "internal" or not _belongs_to_app(r.handler):
            continue
        try:
            hints = typing.get_type_hints(r.handler)
        except Exception:
            hints = {}
        params = {}
        for m in _PARAM.finditer(r.path):
            converter, pname = m.group(1), m.group(2)
            params[pname] = converter or _ANNOTATION_CONVERTERS.get(hints.get(pname), "str")
        file, line = _where(r.handler, root)
        entry = {
            "path": r.path,
            "methods": sorted(r.methods - {"HEAD"}),
            "name": r.name,
            "params": params,
            "function": f"{handler_module}.{getattr(r.handler, '__qualname__', r.name)}",
            "file": file,
            "line": line,
            "doc": _doc(r.handler),
            "login_required": bool(r.options.get("login_required")),
            "admin_required": bool(r.options.get("admin_required")),
            "rate_limit": str(r.options["rate_limit"]) if r.options.get("rate_limit") is not None else None,
        }
        if r.kind == "page":
            entry["title"] = r.options.get("title")
            entry["layout"] = bool(r.options.get("layout", True))
            pages.append(entry)
        else:
            entry["csrf"] = bool(r.options.get("csrf", True))
            routes.append(entry)

    # -- server functions -------------------------------------------------------------------
    server_functions = []
    schema_users: dict[int, list[str]] = {}
    for sf in SERVER_FUNCTIONS.values():
        if not _belongs_to_app(sf.fn):
            continue
        file, line = _where(sf.fn, root)
        entry = {
            "id": sf.id,
            "name": sf.fn.__name__,
            "file": file,
            "line": line,
            "doc": _doc(sf.fn),
            "login_required": bool(sf.login_required),
            "admin_required": bool(sf.admin_required),
            "refresh": bool(sf.refresh),
            "takes_request": bool(sf.wants_request),
            "rate_limit": str(sf.rate_limit) if getattr(sf, "rate_limit", None) is not None else None,
        }
        try:
            hints = sf.hints
        except Exception as exc:  # unresolvable annotations: reported, never hidden
            entry["error"] = str(exc)
            hints = {}
        params = []
        for pname, p in sf.signature.parameters.items():
            if pname == "request" and sf.wants_request:
                continue
            hint = hints.get(pname)
            param = {"name": pname, "type": _type_name(hint)}
            if p.default is not p.empty:
                param["default"] = _jsonable(p.default)
            if isinstance(hint, type) and issubclass(hint, Model):
                param["model"] = hint.__name__
            if getattr(hint, "__jongo_schema__", False):
                param["schema"] = hint.__name__
                schema_users.setdefault(id(hint), []).append(sf.fn.__name__)
            params.append(param)
        entry["params"] = params
        entry["returns"] = _type_name(hints.get("return"))
        entry["signature"] = _signature(sf.fn, hints)
        entry["input_schema"] = function_schema(sf)
        server_functions.append(entry)
    server_functions.sort(key=lambda e: (e["file"] or "", e["line"] or 0))

    # -- models ----------------------------------------------------------------------------
    models = []
    for model in models_registry.values():
        if not _belongs_to_app(model) or model._meta.abstract:
            continue
        fields = []
        for f in model._meta.fields:
            field = {"name": f.name, "type": type(f).__name__, "kind": f.kind, "null": bool(f.null),
                     "unique": bool(f.unique), "index": bool(f.index)}
            if f.default is not NOTHING:
                field["default"] = "<callable>" if callable(f.default) else _jsonable(f.default)
            if getattr(f, "max_length", None):
                field["max_length"] = f.max_length
            if f.choices:
                field["choices"] = [_jsonable(c[0] if isinstance(c, (list, tuple)) else c) for c in f.choices]
            if isinstance(f, ForeignKey):
                field["to"] = f.to if isinstance(f.to, str) else f.to.__name__
                field["on_delete"] = f.on_delete
                field["column"] = f.column
            fields.append(field)
        file, line = _where(model, root)
        models.append({
            "name": model.__name__,
            "table": model._meta.table,
            "ordering": list(model._meta.ordering),
            "fields": fields,
            "file": file,
            "line": line,
            "doc": _doc(model),
        })

    # -- components -------------------------------------------------------------------------
    components = []
    for comp in COMPONENTS.values():
        if not _belongs_to_app(comp.fn):
            continue
        sig = inspect.signature(comp.fn)
        props = []
        for pname, p in sig.parameters.items():
            if pname == "children" or p.kind is p.VAR_KEYWORD:
                continue
            prop = {"name": pname}
            if p.default is not p.empty:
                prop["default"] = _jsonable(p.default)
            props.append(prop)
        file, line = _where(comp.fn, root)
        components.append({
            "name": comp.name,
            "id": comp.id,
            "props": props,
            "takes_children": bool(comp.takes_children),
            "layout": comp is app.layout_component,
            "file": file,
            "line": line,
            "doc": _doc(comp.fn),
        })
    components.sort(key=lambda e: (e["file"] or "", e["line"] or 0))

    # -- form schemas ------------------------------------------------------------------------
    schemas = []
    forms_module = sys.modules.get("jongo.forms")  # no schema can exist unless it was imported
    for schema in (forms_module.SCHEMAS.values() if forms_module is not None else ()):
        if not _belongs_to_app(schema):
            continue
        file = schema.file
        if file:
            try:
                file = str(Path(file).resolve().relative_to(root))
            except (ValueError, OSError):
                pass
        fields = []
        for fname, rule in schema.items():
            field = {"name": fname, "type": rule["type"], "required": bool(rule["required"]), "label": rule["label"]}
            for key in ("min_length", "max_length", "min", "max", "pattern", "choices", "same_as", "default"):
                if key in rule:
                    field[key] = rule[key]
            fields.append(field)
        schemas.append({"name": schema.__name__, "fields": fields, "file": file, "line": schema.line,
                        "used_by": sorted(schema_users.get(id(schema), []))})
    schemas.sort(key=lambda e: (e["file"] or "", e["line"] or 0))

    # -- channels ---------------------------------------------------------------------------
    channels = [
        {"pattern": ch.pattern, "guard": ch.name, "params": sorted(ch.params)}
        for ch in app.channels
    ]

    # -- database and admin ------------------------------------------------------------------
    database: dict | None
    try:
        path = connection.database_path()
        if connection.backend() == "postgres":
            path = connection._redact(path)  # never put a password into a context pack
        elif path != ":memory:":
            try:
                path = str(Path(path).resolve().relative_to(root))
            except (ValueError, OSError):
                pass
        database = {"backend": connection.backend(), "database": path}
    except Exception:
        database = None

    app_file, _ = _where(app.router.routes[0].handler, root) if pages or routes else (None, None)
    return {
        "jongo": __version__,
        "app": {
            "name": app.name,
            "title": app.title,
            "root": str(root),
            "file": next((p["file"] for p in pages + routes if p["file"]), None),
            "database": database,
            "admin": min(admin_paths, key=len) if admin_paths else None,
            "layout": app.layout_component.name if app.layout_component is not None else None,
            "login_url": app.login_url,
            "mcp": getattr(app, "mcp_path", None),
        },
        "models": models,
        "server_functions": server_functions,
        "pages": pages,
        "routes": routes,
        "components": components,
        "channels": channels,
        "schemas": schemas,
    }


# ------------------------------------------------------------------------------- context --

RULES = """\
- **One language, one file.** Pages, `@server` functions, `@component` UI and `db.Model` tables live \
together in Python. There is no JavaScript project, no API schema and no build step to keep in sync.
- **A page runs on the server** and returns UI (elements and components), a `Page`, or a `Response`. \
Register it with `@app.page("/path")`; a path like `/todos/<int:id>` or a parameter annotated `id: int` \
converts and validates the segment. `@app.route(...)` is a plain handler that returns a `Response`, a \
string of HTML, or JSON-able data.
- **A component runs in two places**: on the server for the first paint, and in the browser, compiled \
to JavaScript. So it may only do what both can do: **no database calls, no file system, no `import` \
inside a function, no classes, no server-only modules** inside a `@component`. Load data in the page \
or in a `@server` function and pass it in as props (`Model.to_dict()` makes a row JSON-able). \
Unsupported code is a compile error at start-up; `jongo check` reports the file, line and a hint.
- **Hooks** are called at the top level of a component, in the same order every render: `state()`, \
`effect()`, `ref()`, `live()`. `count = state(0)`; read `count.value`, write `count.value += 1` or \
`count.set(x)`; a re-render is scheduled, not immediate. `state(prop)` seeds once on mount: render \
server data straight from props and keep in state only what the browser owns.
- **The browser calls the server with `await`.** Only `@server` functions are reachable: \
`todo = await add_todo(draft.value)` inside an event handler. Arguments are validated against the type \
hints before the function runs (`str`, `int`, `float`, `bool`, `list[...]`, `dict[...]`, `Optional`, \
`date`/`datetime`, `UUID`, `Decimal`, `Enum`, and model classes: a parameter annotated `todo: Todo` receives \
the row for the id the browser sent, or 404); any other annotation is refused at start-up (J405). \
A first parameter named `request` receives the request (`request.user`, `request.session`). \
`@server(login_required=True)` requires a signed-in user. Raise `Forbidden`, `NotFound` or \
`db.ValidationError({"field": "message"})` for errors the browser may read; any other exception is a bare \
"Server error". `refresh=True` re-runs the page loader after a successful call.
- **Forms validate once, in both places.** `from jongo import forms`; `Signup = forms.Schema("Signup", \
email=forms.Email(), age=forms.Int(min=18))` (also `Text`, `Float`, `Bool`, `Choice`, and \
`forms.Schema.from_model(Model)`). In a component, `forms.validate(Signup, form_values(event))` returns \
`{field: message}` (the same Python, compiled); on the server, annotate the parameter `data: Signup` and the \
function receives the cleaned dict or the browser gets a 400 with `errors`. `forms.Field(schema=Signup, \
name="email", error=...)` renders the control. Patterns must be plain regular expressions (no `(?i)`, no named groups).
- **Events** hand the handler an event: `on_click=lambda e: ...`, `on_input=lambda e: draft.set(e.target.value)`, \
`on_submit=add` (an `async def add(event)` may `await` server functions); always `on_<event>=`, never React's `onClick=` (a compile error, J114). `js.setTimeout`, `js.document` \
and friends reach the browser from Python.
- **Models** subclass `db.Model` with `db.Text`, `db.Int`, `db.Float`, `db.Bool`, `db.DateTime`, `db.JSON`, \
`db.ForeignKey(Other)`; an `id` primary key is implicit. The schema is synced automatically by `jongo dev` \
and `jongo migrate` (no migration files). Query with `Model.all()`, `.filter(field=value)`, `.get(...)`, \
`.create(...)`, `.first()`, `.exists()`; `.save()`, `.delete()`, `.to_dict()`. SQLite by default; \
`Jongo(database="postgres://...")` runs the same code on PostgreSQL.
- **Real time**: `@app.channel("room:<int:id>")` declares a channel and its function decides who may \
subscribe; `broadcast(channel, data)` pushes from anywhere on the server; `live(channel, handler)` in a \
component receives it. An undeclared channel cannot be subscribed to, and the guard must `return True` to \
allow one: returning nothing refuses (J412). A tab listens on one stream of at most 20 channels in all, so \
give a list one channel and put the row id in the message instead of a channel per row.
- **Styles** are Python: `s = css(card={"padding": 12, ":hover": {...}})` then `class_=s.card`; \
`global_css({...})` for page-wide rules.
- **Auth, sessions, CSRF and the admin are built in**: `app.admin()` mounts the admin; `jongo createadmin` \
makes a user; `login_required=True` on pages, routes and server functions. Sign-up is \
`User.create_user(username, password, **fields)`, which hashes the password; never `User.create(password=...)` \
(`save()` refuses a password that isn't a hash). A `login_required` page redirects to `login_url` (default \
`/login`) with `?next=`: define that page yourself, and after `login()` redirect to `next` only if \
`is_local_url(next)`. `authenticate(username, password, request)` throttles wrong passwords by itself (429, \
J213); `auth.revoke_sessions(user)` signs a user out everywhere. `rate_limit="5/minute"` on `@server`, \
`@app.page` or `@app.route` caps one caller (counted per address and per signed-in user).
- **Browser differences** (JavaScript's value model): `str(10 / 2)` is `"5"` (format explicitly), a dict \
keyed by ints iterates string keys in ascending order (not insertion order), `isinstance(5, float)` is `True`, \
integers past 2**53 lose precision, and generator expressions are eager (`any()`/`all()` don't stop early). \
Tuples and strings behave as in Python.
- **Test through the real stack**: `from jongo.testing import TestClient`; `client = TestClient(app)`; \
`client.get("/")`, `client.post(...)`, `client.rpc(add_todo, "title")`, `client.login(user)`. `TestClient` uses \
the app's configured database, so `tests/conftest.py` needs an autouse fixture that gives each test a fresh \
one: `db.configure(":memory:")`, `db.migrate()`, `yield`, `db.close_connections()` (`jongo new` writes it). \
`response.status` is an int; a refused `client.rpc(...)` raises `ServerError` with `.status`, `.code`, `.errors`.
- **Zero dependencies.** Don't add a package to solve something the framework already does."""


def _fmt_default(value) -> str:
    return "" if value is None else f" = {value!r}"


def context(app) -> str:
    """A Markdown context pack: the rules of the framework, then this app's own map."""
    d = describe(app)
    a = d["app"]
    lines = [
        f"# {a['title']} — a Jongo app, described for a coding agent",
        "",
        f"Generated by `jongo context` from the running code (jongo {d['jongo']}). Regenerate after "
        "changes rather than editing it.",
        "",
        "## How to work here",
        "",
        f"- The app is `{a['file'] or 'app.py'}`: pages, server functions, components and models in one file "
        "(a bigger app may split into modules; every registry is global).",
        "- `jongo dev` runs it with auto-reload and an error overlay. `jongo check --json` compiles every "
        "component, resolves every server function's type hints and plans the migrations, and reports "
        "problems as data (exit 1 if any). `jongo routes --json` lists the URL map. `pytest` runs the tests.",
        "- **When something goes wrong:** every Jongo error carries a stable code (`J302`) and a hint. "
        "`jongo explain J302` says what it means and how to fix it. `jongo doctor --json` checks the whole "
        "setup (Python, database reachable, pending migrations, compile errors, production settings) and "
        "exits 1 on a failure. With the app running, `jongo status --json` shows the recent requests, errors "
        "(code, traceback, request id), per-route counters and likely N+1 queries.",
        "- **Tools instead of commands:** `jongo mcp` serves all of the above, plus every `@server` function, "
        "as MCP tools over stdio (`claude mcp add app -- jongo mcp`)."
        + (f" The running server also answers MCP at `{a['mcp']}`." if a.get("mcp") else "")
        + " `jongo openapi` prints the OpenAPI document generated from the type hints.",
        f"- Database: {a['database']['backend']} ({a['database']['database']})." if a["database"] else
        "- Database: not configured (pass `database=` to `Jongo(...)`).",
        f"- Admin: mounted at `{a['admin']}`." if a["admin"] else "- Admin: not mounted (`app.admin()` would).",
        f"- Layout: `{a['layout']}` wraps every page." if a["layout"] else "- Layout: none (`@app.layout` sets one).",
        "",
        "## The rules of the framework",
        "",
        RULES,
        "",
        f"## Models ({len(d['models'])})",
        "",
    ]
    if not d["models"]:
        lines.append("_None yet. Subclass `db.Model`; the table appears on the next `jongo dev`._")
    for m in d["models"]:
        where = f" · `{m['file']}:{m['line']}`" if m["file"] else ""
        order = f", ordered by `{', '.join(m['ordering'])}`" if m["ordering"] else ""
        lines.append(f"### `{m['name']}` — table `{m['table']}`{order}{where}")
        if m["doc"]:
            lines.append(f"_{m['doc']}_")
        lines += ["", "| field | type | |", "|---|---|---|", "| `id` | Int | primary key |"]
        for f in m["fields"]:
            notes = []
            if f.get("to"):
                notes.append(f"→ `{f['to']}` (on delete {f['on_delete'].lower()}; column `{f['column']}`)")
            if f.get("max_length"):
                notes.append(f"max {f['max_length']}")
            if f["null"]:
                notes.append("nullable")
            if f["unique"]:
                notes.append("unique")
            if "default" in f:
                notes.append(f"default {f['default']!r}")
            if f.get("choices"):
                notes.append("choices " + ", ".join(repr(c) for c in f["choices"]))
            lines.append(f"| `{f['name']}` | {f['type']} | {'; '.join(notes)} |")
        lines.append("")
    lines += [f"## Server functions ({len(d['server_functions'])})", "",
              "Callable from browser code as `await name(args)`; arguments are validated against these hints.", ""]
    if not d["server_functions"]:
        lines.append("_None yet. Decorate a function with `@server`._")
    for s in d["server_functions"]:
        flags = []
        if s["takes_request"]:
            flags.append("takes `request`")
        if s["admin_required"]:
            flags.append("admin only")
        elif s["login_required"]:
            flags.append("login required")
        if s["refresh"]:
            flags.append("refreshes the page after")
        if s.get("rate_limit"):
            flags.append(f"limited to {s['rate_limit']}")
        where = f" · `{s['file']}:{s['line']}`" if s["file"] else ""
        lines.append(f"- `{s['signature']}`{where}" + (f" · {', '.join(flags)}" if flags else ""))
        if s.get("doc"):
            lines.append(f"  _{s['doc']}_")
        if s.get("error"):
            lines.append(f"  **problem:** {s['error']}")
    lines += ["", f"## Pages ({len(d['pages'])})", ""]
    for p in d["pages"]:
        lines.append(_route_line(p))
    if d["routes"]:
        lines += ["", f"## Routes ({len(d['routes'])})", ""]
        for r in d["routes"]:
            lines.append(_route_line(r))
    lines += ["", f"## Components ({len(d['components'])})", ""]
    if not d["components"]:
        lines.append("_None yet. Decorate a function with `@component`._")
    for c in d["components"]:
        props = ", ".join(f"{p['name']}{_fmt_default(p.get('default'))}" for p in c["props"])
        if c["takes_children"]:
            props = (props + ", " if props else "") + "children"
        where = f" · `{c['file']}:{c['line']}`" if c["file"] else ""
        tag = " · the layout" if c["layout"] else ""
        lines.append(f"- `{c['name']}({props})`{where}{tag}")
        if c.get("doc"):
            lines.append(f"  _{c['doc']}_")
    if d["schemas"]:
        lines += ["", f"## Form schemas ({len(d['schemas'])})", "",
                  "Validated by the same code in the browser (`forms.validate`) and on the server "
                  "(a `@server` parameter annotated with the schema).", ""]
        for sc in d["schemas"]:
            where = f" · `{sc['file']}:{sc['line']}`" if sc["file"] else ""
            used = f" · used by {', '.join(f'`{u}`' for u in sc['used_by'])}" if sc["used_by"] else ""
            lines.append(f"### `{sc['name']}`{where}{used}")
            lines += ["", "| field | type | rules |", "|---|---|---|"]
            for f in sc["fields"]:
                notes = ["required" if f["required"] else "optional"]
                for key, label in (("min_length", "min length"), ("max_length", "max length"), ("min", "min"),
                                   ("max", "max"), ("same_as", "must match"), ("default", "default")):
                    if key in f:
                        notes.append(f"{label} {f[key]!r}" if key in ("same_as", "default") else f"{label} {f[key]}")
                if "choices" in f:
                    notes.append("one of " + ", ".join(repr(c) for c in f["choices"]))
                if "pattern" in f and f["type"] != "email":
                    notes.append(f"pattern `{f['pattern']}`")
                lines.append(f"| `{f['name']}` | {f['type']} | {'; '.join(notes)} |")
            lines.append("")
    if d["channels"]:
        lines += ["", f"## Channels ({len(d['channels'])})", ""]
        for ch in d["channels"]:
            params = f" · params {', '.join(ch['params'])}" if ch["params"] else ""
            lines.append(f"- `{ch['pattern']}` — guarded by `{ch['guard']}`{params}")
    return "\n".join(lines).rstrip() + "\n"


def _route_line(r: dict) -> str:
    methods = ",".join(r["methods"])
    params = f" · params {', '.join(f'{k}:{v}' for k, v in r['params'].items())}" if r["params"] else ""
    guard = " · admin only" if r["admin_required"] else (" · login required" if r["login_required"] else "")
    if r.get("rate_limit"):
        guard += f" · limited to {r['rate_limit']}"
    where = f" · `{r['file']}:{r['line']}`" if r["file"] else ""
    title = f" · title {r['title']!r}" if r.get("title") else ""
    return f"- {methods} `{r['path']}` → `{r['name']}`{params}{title}{guard}{where}"


# --------------------------------------------------------------------------------- check --


def _plan_without_writing(db) -> list:
    """``db.plan_migrations()``, except that a SQLite file that doesn't exist yet isn't created.

    Connecting to a missing SQLite file creates it (and its directory), which a read-only
    check must not do; against no database every model is a pending "create table", which
    is exactly what planning against an empty in-memory database reports.
    """
    from .db import connection

    path = connection.database_path()
    if connection.backend() != "sqlite" or path == connection.MEMORY or Path(path).exists():
        return db.plan_migrations()
    from .diagnostics import sqlite_location_problem
    from .errors import JongoError

    problem = sqlite_location_problem(Path(path))
    if problem:
        raise JongoError(f"SQLite {path}: {problem}", code="J301")
    connection.configure(connection.MEMORY)
    try:
        return db.plan_migrations()
    finally:
        connection.configure(path)


def _guard_never_answers(guard) -> bool:
    """True when a channel guard's source has no ``return <value>`` and no ``raise`` at all.

    Such a guard returns None for everyone, which refuses every subscription (J412). It is
    a static read of the function's own body; a guard whose source can't be read, or that
    answers on only some paths, is left to the run-time check.
    """
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(inspect.unwrap(guard))))
    except (OSError, TypeError, SyntaxError, IndentationError):
        return False
    function = tree.body[0] if tree.body else None
    if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    pending = list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue  # a nested function's return isn't the guard's
        if isinstance(node, ast.Raise):
            return False
        if isinstance(node, ast.Return) and node.value is not None and not (
                isinstance(node.value, ast.Constant) and node.value.value is None):
            return False
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return False
        pending.extend(ast.iter_child_nodes(node))
    return True


def check(app) -> dict:
    """Compile, resolve, plan — and report every problem as data.

    The report is what a coding agent (or CI) needs to decide what to do next: ``ok``,
    a ``problems`` list with ``kind``, ``message`` and, where known, ``file``, ``line``
    and ``hint``, the counts of what was found, and the migrations ``jongo dev`` would
    apply. Nothing is written.

    Problem kinds: ``compile`` (a component can't be compiled), ``server_function`` (a
    type hint can't be resolved), ``channel`` (a guard that never returns a value, so it
    refuses everyone: ``J412``) and ``migrations`` (the schema couldn't be planned).
    """
    from . import __version__

    problems: list[dict] = []

    try:
        app.check()
    except CompileError as exc:
        file = exc.filename
        if file:
            try:
                file = str(Path(file).resolve().relative_to(app.root))
            except (ValueError, OSError):
                pass
        problems.append({
            "kind": "compile", "code": exc.code, "message": exc.message, "file": file, "line": exc.lineno,
            "source": exc.source_line, "hint": exc.hint,
        })

    d = describe(app)
    for s in d["server_functions"]:
        if s.get("error"):
            problems.append({"kind": "server_function", "code": "J405", "name": s["name"], "message": s["error"],
                             "file": s["file"], "line": s["line"],
                             "hint": "Every parameter of a @server function needs an annotation Jongo can "
                                     "resolve; define the model at module level or annotate with a real class."})

    for channel in app.channels:
        if _guard_never_answers(channel.guard):
            file, line = _where(channel.guard, app.root)
            problems.append({
                "kind": "channel", "code": "J412", "name": channel.name, "file": file, "line": line,
                "message": f"the guard {channel.name}() for channel {channel.pattern!r} never returns a value, "
                           "so every subscription is refused",
                "hint": "return True to allow the subscription and False to refuse it",
            })

    pending: list[dict] = []
    if d["app"]["database"] is not None:
        from . import db

        try:
            for op in _plan_without_writing(db):
                pending.append({"describe": op.describe(), "destructive": bool(op.destructive), "sql": list(op.sql),
                                "code": "J302"})
        except Exception as exc:
            from .errors import classify
            from .monitor import redact

            code, hint = classify(exc)
            if code == "J000":
                code, hint = "J303", hint_for("J303")
            problems.append({"kind": "migrations", "code": code,
                             "message": f"could not plan migrations: {redact(str(exc), 2000)}",
                             "hint": hint})

    return {
        "jongo": __version__,
        "app": d["app"]["name"],
        "ok": not problems,
        "problems": problems,
        "counts": {k: len(d[k]) for k in ("models", "server_functions", "pages", "routes", "components", "channels",
                                          "schemas")},
        "pending_migrations": pending,
    }
