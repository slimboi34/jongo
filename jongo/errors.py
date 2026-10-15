"""Exception types shared across Jongo, and the catalogue of stable error codes.

Every error Jongo raises or reports carries a **stable code** — ``J101``, ``J302`` — that
names the problem independently of its wording, a one-line **hint** that says what to do,
and ``to_dict()`` for tools. ``jongo explain J302`` prints the full entry from
:data:`CATALOGUE`; ``jongo explain --list`` lists them all.

Code ranges:

* ``J0xx`` — general (an exception in the app's own code)
* ``J1xx`` — components and the compiler
* ``J2xx`` — routing and HTTP
* ``J3xx`` — the database, models and migrations
* ``J4xx`` — server functions and channels
* ``J5xx`` — configuration, the CLI and deployment

Codes are never reused or renumbered; a retired code stays in the catalogue.
"""
from __future__ import annotations

import re
import traceback as _traceback
from pathlib import Path

_CODE = re.compile(r"^J\d{3}$")


class Diagnostic:
    """Mixin: a ``code``, a ``hint`` and ``to_dict()`` on an exception.

    Used by Jongo's own exceptions (and the ORM's, which keep their historical bases).
    ``code`` and ``hint`` are class-level defaults that an instance may override.
    """

    code: str | None = None
    hint: str | None = None

    def to_dict(self) -> dict:
        return error_dict(self)


class JongoError(Diagnostic, Exception):
    """Base class for framework errors.

    Accepts optional ``code=``, ``hint=``, ``file=`` and ``line=`` keywords, so a raise
    site can say which catalogue entry it is and where the problem is.
    """

    def __init__(self, *args, code: str | None = None, hint: str | None = None,
                 file: str | None = None, line: int | None = None):
        super().__init__(*args)
        if code is not None:
            self.code = code
        if hint is not None:
            self.hint = hint
        if file is not None:
            self.file = file
        if line is not None:
            self.line = line


class CompileError(JongoError):
    """Python code that can't be turned into browser JavaScript."""

    code = "J100"

    def __init__(self, message, *, filename=None, lineno=None, source_line=None, hint=None, code=None):
        super().__init__(message, code=code, hint=hint)
        self.message = message
        self.filename = filename
        self.lineno = lineno
        self.source_line = source_line
        if hint is None:
            hint = CATALOGUE.get(self.code, {}).get("fix")
        self.hint = hint

    @property
    def file(self):
        return self.filename

    @property
    def line(self):
        return self.lineno

    def __str__(self):
        parts = [f"[{self.code}] {self.message}" if self.code else self.message]
        if self.filename:
            where = f"{self.filename}:{self.lineno}" if self.lineno else self.filename
            parts.append(f"  at {where}")
        if self.source_line:
            parts.append(f"    {self.source_line.strip()}")
        if self.hint:
            parts.append(f"  hint: {self.hint}")
        return "\n".join(parts)


class HTTPError(JongoError):
    """Raise from a page, route or server function to send an error response."""

    code = "J200"

    def __init__(self, status: int = 500, message: str | None = None, *, code: str | None = None,
                 hint: str | None = None):
        from http import HTTPStatus

        self.status = status
        try:
            default = HTTPStatus(status).phrase
        except ValueError:
            default = "Error"
        self.message = message or default
        super().__init__(f"{status} {self.message}", code=code, hint=hint)


class ServerError(JongoError):
    """Raised in browser code when a @server function call fails.

    Has ``.type`` (the server exception's class name), ``.status``, ``.errors``
    (field errors from a ValidationError, if any), and ``.code``/``.hint`` from the
    error catalogue.
    """


class NotFound(HTTPError):
    code = "J205"

    def __init__(self, message: str | None = None, *, code: str | None = None, hint: str | None = None):
        super().__init__(404, message, code=code, hint=hint)


class Forbidden(HTTPError):
    code = "J210"

    def __init__(self, message: str | None = None, *, code: str | None = None, hint: str | None = None):
        super().__init__(403, message, code=code, hint=hint)


class RouteError(JongoError, ValueError):
    """A route that can't be registered (bad converter, bad path). Still a ``ValueError``."""


class URLBuildError(JongoError, KeyError):
    """``url_for`` can't build a URL. Still a ``KeyError``, for code that catches that."""

    def __str__(self):
        return str(self.args[0]) if self.args else ""


# ------------------------------------------------------------------------------ catalogue --

CATEGORIES = {
    "J0": "General",
    "J1": "Components and the compiler",
    "J2": "Routing and HTTP",
    "J3": "Database, models and migrations",
    "J4": "Server functions and channels",
    "J5": "Configuration, CLI and deployment",
}


def _e(title, explanation, fix, example=None):
    return {"title": title, "explanation": explanation, "fix": fix, "example": example}


#: code -> {title, explanation, fix, example}. ``fix`` doubles as the one-line hint.
CATALOGUE: dict[str, dict] = {
    # -- J0xx general ----------------------------------------------------------------------
    "J000": _e(
        "Unhandled exception in the app's code",
        "Code the app wrote raised an exception Jongo has no specific entry for. The traceback "
        "points at the line; the dev error page highlights the frames in your files.",
        "Read the innermost frame in your own file (it is highlighted) and fix the code there.",
    ),
    # -- J1xx components and the compiler --------------------------------------------------
    "J100": _e(
        "Code can't be compiled for the browser",
        "A @component (or a function it calls) uses Python the compiler can't turn into "
        "JavaScript. Components run twice: on the server for the first paint and in the browser.",
        "Move the unsupported code into the page function or a @server function and pass the result in as props.",
    ),
    "J101": _e(
        "Statement not supported in browser code",
        "`import`, `class`, `with`, `global`, `match` and decorators on nested functions have no "
        "browser equivalent inside a component.",
        "Import at the top of the module (the compiler resolves imported names); use dicts instead of "
        "classes; keep changing values in state().",
        "@component\ndef Clock():\n    import time          # J101\n    ...",
    ),
    "J102": _e(
        "Server-only object used in browser code",
        "A component refers to something that only exists on the server: a database model, a module "
        "that isn't available in the browser, a class, or a function from a library.",
        "Load rows in the page (or a @server function) and pass them as props; call server-only code "
        "through a @server function.",
        "@component\ndef Todos():\n    rows = Todo.all()    # J102: Todo is a database model\n",
    ),
    "J103": _e(
        "Undefined name in browser code",
        "A component uses a name that isn't defined where the compiler looks (the function, its "
        "closure, the module), or a builtin that doesn't exist in the browser.",
        "Define or import the name at module level, or check the spelling.",
    ),
    "J104": _e(
        "Expression or syntax not supported in browser code",
        "An operator, literal, assignment target or expression form has no browser translation "
        "(e.g. slice assignment, super(), async comprehensions, bytes literals).",
        "Rewrite it with a supported form (plain assignment, a loop, a helper function), or move it to the server.",
    ),
    "J105": _e(
        "Component source can't be read",
        "The compiler needs a component's source code: it must be a `def` in a .py file, not a "
        "lambda, an exec() string or an interactive session.",
        "Define the component with `def` in a module file.",
    ),
    "J106": _e(
        "Component can't be async",
        "A component renders synchronously on the server; an `async def` component has nothing to await there.",
        "Load the data in the page function or a @server function; async event handlers inside the component are fine.",
    ),
    "J107": _e(
        "Hook called outside a component render",
        "state(), effect(), ref() and live() belong to the component that is rendering. Called from a "
        "page function, a route or at import time there is no component to attach them to.",
        "Call hooks at the top level of a @component function.",
        "@app.page('/')\ndef home():\n    count = state(0)     # J107\n",
    ),
    "J108": _e(
        "Browser-only API called on the server",
        "navigate(), refresh(), form_values() and js.* exist only in the browser; calling them while the "
        "server renders has no meaning.",
        "Call it from an event handler or inside effect().",
    ),
    "J110": _e(
        "Component returned a list",
        "A component must return exactly one element (it becomes one node in the tree).",
        "Wrap the elements in div(...) or fragment(...).",
    ),
    "J111": _e(
        "Value can't be sent to the browser",
        "Props and data are sent to the browser as JSON. Functions, arbitrary objects and dicts whose "
        "keys collide as strings can't be.",
        "Convert it to dict/list/str/int/float/bool (Model rows: .to_dict()).",
    ),
    "J112": _e(
        "Invalid UI element",
        "The tree contains something that isn't UI: an object that isn't an element, an invalid tag "
        "name, or an event handler/ref outside any component.",
        "Return elements (div(...), components) from pages and components; put event handlers inside a @component.",
    ),
    "J113": _e(
        "Unknown component",
        "The tree references a component id that isn't registered — usually a stale page after the "
        "code changed, or a component defined conditionally.",
        "Reload the page; define components at module level so they register at import.",
    ),
    "J114": _e(
        "Event handler passed under the wrong name",
        "Event handlers are passed as on_<event>= (on_click, on_input, on_submit). A function passed as "
        "onclick= or onClick= is an HTML attribute name, which Jongo drops (inline handlers are never "
        "emitted), so the handler would silently never run.",
        "Rename the keyword to on_<event>: onClick= becomes on_click=, onInput= becomes on_input=.",
        "button('Save', onClick=save)   # J114: use on_click=save",
    ),
    # -- J2xx routing and HTTP --------------------------------------------------------------
    "J200": _e(
        "HTTP error raised by the app",
        "A page, route or server function raised HTTPError(status, message) on purpose.",
        "Nothing to fix in Jongo: this is the response the app chose. Check the condition that raised it.",
    ),
    "J201": _e(
        "Unknown route converter",
        "A route path uses `<name:param>` with a converter Jongo doesn't have. Converters: str, int, "
        "float, slug, path, uuid.",
        "Use one of str, int, float, slug, path, uuid — or annotate the parameter (id: int) and write <id>.",
        "@app.page('/posts/<integer:id>')   # J201: use <int:id>",
    ),
    "J202": _e(
        "Invalid route path",
        "Route paths must start with '/'.",
        "Write the path as '/things', not 'things'.",
    ),
    "J203": _e(
        "No route with that name",
        "url_for(name) was asked for a route that doesn't exist. A route's name defaults to its "
        "function's name.",
        "Check `jongo routes` for the names, or pass name= when registering the route.",
    ),
    "J204": _e(
        "Missing URL parameter",
        "url_for() was called without a value for one of the route's <parameters>.",
        "Pass every parameter in the route path as a keyword: url_for('post', id=3).",
    ),
    "J205": _e(
        "Not found",
        "No page or route matches the path, or the app raised NotFound.",
        "Check the URL against `jongo routes`; a typed segment (<int:id>) only matches values of that type.",
    ),
    "J206": _e(
        "Method not allowed",
        "A route matches the path but not the HTTP method (e.g. POST to a page).",
        "Register the method (methods=('GET', 'POST')) or send the request with an allowed method (see the Allow header).",
    ),
    "J207": _e(
        "CSRF check failed",
        "Unsafe requests (POST, PUT, PATCH, DELETE) must carry the CSRF token: browser server calls do "
        "this automatically; classic HTML forms need the hidden csrf_token input.",
        "Add input(type='hidden', name='csrf_token', value=request.csrf_token) to the form, or pass csrf=False "
        "for a webhook that authenticates another way.",
    ),
    "J208": _e(
        "Login required",
        "The page, route or server function was declared login_required=True and there is no signed-in user.",
        "Sign in first (client.login(user) in tests), or remove login_required if the endpoint is public.",
    ),
    "J209": _e(
        "Admins only",
        "The endpoint was declared admin_required=True and the signed-in user isn't an admin.",
        "Sign in as a user with is_admin=True (`jongo createadmin` makes one).",
    ),
    "J210": _e(
        "Forbidden",
        "The app (or a guard) raised Forbidden for this request.",
        "Check the permission condition that raised it; this is usually the app working as intended.",
    ),
    "J211": _e(
        "Query parameter has the wrong type",
        "A page or route parameter that isn't in the path is read from the query string and checked "
        "against its annotation (page: int); the value in the URL didn't match.",
        "Fix the link or form that builds the URL, or give the parameter a type that accepts the value "
        "(str, Optional[int]) and handle it in the function.",
        "@app.page('/list')\ndef listing(page: int = 1): ...\n# GET /list?page=abc  -> J211",
    ),
    "J212": _e(
        "Malformed request body",
        "The request body couldn't be read: it isn't valid JSON, the Content-Length header is invalid, "
        "or it is larger than the app's limit (max_body_size).",
        "Send a valid JSON body with Content-Type: application/json and a correct Content-Length; for "
        "larger uploads raise jongo.Request.max_body_size (16 MB by default).",
    ),
    # -- J3xx database ------------------------------------------------------------------------
    "J300": _e(
        "Database error",
        "The database driver reported an error Jongo has no specific entry for.",
        "Read the driver's message; run `jongo doctor` to check the connection and schema.",
    ),
    "J301": _e(
        "Database unreachable",
        "Jongo can't open or connect to the configured database: the SQLite file's directory doesn't "
        "exist or isn't writable, or the PostgreSQL server refused the connection.",
        "Check Jongo(database=...) / JONGO_DATABASE: the directory must exist and be writable, or the server must be running.",
    ),
    "J302": _e(
        "Schema out of date (pending migrations)",
        "The models don't match the database: a table or column the code uses doesn't exist yet.",
        "Run `jongo migrate` (`jongo migrate --plan` shows the changes first); `jongo dev` and `jongo run --migrate` apply them on start.",
        "class Todo(db.Model):\n    title = db.Text()\n    due = db.DateTime(null=True)   # new field -> J302 until migrated",
    ),
    "J303": _e(
        "Migration failed or blocked",
        "A schema change couldn't be applied: a destructive change without --allow-destructive, two "
        "models sharing a table, a required foreign key added to a table with rows, or the database "
        "rejected the statement.",
        "Read the message: make the new field null=True or give it a default, rename the table, or run "
        "`jongo migrate --plan` to see what would change.",
    ),
    "J304": _e(
        "Validation failed",
        "A model's field validation (or the app's own db.ValidationError) rejected the data. `.errors` "
        "maps field names to messages; server calls return them to the browser with status 400.",
        "Show err.errors to the user, or fix the data being saved.",
    ),
    "J305": _e(
        "Integrity constraint violated",
        "The database rejected a write: a unique value already exists, a required column is NULL, or a "
        "foreign key points at a missing row.",
        "Check for an existing row before creating (.exists()), or catch the error and report it to the user.",
    ),
    "J306": _e(
        "PostgreSQL driver missing",
        "The database URL is postgres:// but the psycopg driver isn't installed.",
        "pip install \"jongo[postgres]\"",
    ),
    "J307": _e(
        "Row does not exist",
        "Model.get(...) matched no rows.",
        "Use .filter(...).first() when the row may be missing, or catch Model.DoesNotExist (raise NotFound for a 404).",
    ),
    "J308": _e(
        "More than one row matched",
        "Model.get(...) matched several rows; get() expects exactly one.",
        "Filter on a unique field (id), or use .filter(...).first().",
    ),
    "J309": _e(
        "Invalid database URL",
        "The database setting is empty or uses a scheme Jongo doesn't support.",
        "Use a SQLite path ('app.sqlite3', 'sqlite:///path', ':memory:') or a postgres:// URL.",
    ),
    "J310": _e(
        "Unknown model or field",
        "A query or relation names a model or field that doesn't exist (a typo, or a model defined after "
        "the ForeignKey that points at it).",
        "Check the spelling against `jongo context`; define (import) the referenced model first.",
    ),
    # -- J4xx server functions and channels ---------------------------------------------------
    "J401": _e(
        "No such server function",
        "The browser called a server function id that isn't registered: the function isn't decorated "
        "with @server, was renamed, or the page is from an older build.",
        "Decorate the function with @server at module level, and reload the page after renaming it.",
    ),
    "J402": _e(
        "Malformed server function call",
        "The request body isn't the {\"args\": [...], \"kwargs\": {...}} object a server call sends.",
        "Call server functions from browser code with await fn(...), or from tests with client.rpc(fn, ...).",
    ),
    "J403": _e(
        "Server function argument has the wrong type",
        "Arguments from the browser are validated against the function's type hints before it runs, "
        "and this one didn't match (or was out of range / not one of the allowed values).",
        "Send the annotated type (convert in the browser: int(draft.value)), or change the annotation.",
        "@server\ndef add(n: int): ...\n# browser: await add('three')   -> J403",
    ),
    "J404": _e(
        "Server function arguments don't match its signature",
        "Too many, too few or unknown keyword arguments for the function's parameters.",
        "Call it with the parameters in its signature (`jongo context` lists every signature).",
    ),
    "J405": _e(
        "Server function type hint can't be resolved",
        "A parameter annotation names a type Jongo can't find. Without it the argument can't be "
        "validated, so the function refuses to run.",
        "Define the annotated type (e.g. the model) at module level where the function can see it, or drop the annotation.",
    ),
    "J406": _e(
        "Two server functions share an id",
        "Server functions are addressed as {module}.{qualname}; two built inside the same factory "
        "function get the same id, and the browser couldn't tell them apart.",
        "Move them to module level, or give each a distinct name.",
    ),
    "J407": _e(
        "Server function raised an exception",
        "The server function's own code raised. In production the browser only sees \"Server error\"; "
        "the traceback is in the server log (and in `jongo status`).",
        "Read the traceback in the log or `jongo status`; raise NotFound/Forbidden/ValidationError for errors the browser may read.",
    ),
    "J408": _e(
        "Server function returned an unsupported response",
        "A server function may return data or redirect(); other Response objects can't be sent back to a server call.",
        "Return JSON-able data, or redirect(url).",
    ),
    "J410": _e(
        "Channel subscription refused",
        "The page asked to subscribe to a channel that isn't declared with @app.channel, has an invalid "
        "name, or whose guard returned False.",
        "Declare the channel with @app.channel('pattern') and check its guard.",
    ),
    "J411": _e(
        "Model argument names a row that doesn't exist",
        "A server function parameter annotated with a model class receives the row for the id the browser "
        "sent; there is no row with that id.",
        "Check the id the browser sends (the row may have been deleted); handle the 404 in the browser.",
    ),
    # -- J5xx configuration, CLI and deployment -----------------------------------------------
    "J501": _e(
        "Secret key not set (or too weak) for production",
        "Sessions and CSRF tokens are signed with the secret key. Without JONGO_SECRET_KEY, a production "
        "server falls back to a key file in .jongo/ that a fresh container won't have (every deploy signs "
        "everyone out) — and a short key can be guessed.",
        "Set JONGO_SECRET_KEY to a long random value: python -c \"import secrets; print(secrets.token_urlsafe(48))\"",
    ),
    "J502": _e(
        "Dev mode is on",
        "Dev mode shows tracebacks and source code on error pages and opens /_jongo/status to anyone who can reach the "
        "server (unless JONGO_ENV=production is set, which keeps the status endpoint locked).",
        "Don't set JONGO_DEV=1 or dev=True in production; run with `jongo run`.",
    ),
    "J503": _e(
        "App file not found",
        "The CLI looks for app.py (or the file/module given as an argument, or JONGO_APP).",
        "Run the command inside the project, pass the file (jongo check path/to/app.py), or create one with `jongo new`.",
    ),
    "J504": _e(
        "No Jongo app in the module",
        "The module imported but defines no Jongo instance.",
        "Add app = Jongo(__name__) to the module, or name it: jongo check module:attr.",
    ),
    "J505": _e(
        "Port already in use",
        "Another process is listening on the port.",
        "Use another port (--port 8001) or stop the other process.",
    ),
    "J506": _e(
        "Static directory missing",
        "The app references /static/ files but there's no static/ directory next to the app, so they will 404.",
        "Create static/ next to app.py and put the files there.",
    ),
    "J507": _e(
        "Cookies won't be marked Secure behind a proxy",
        "Session cookies get the Secure flag when the request is HTTPS. Behind a TLS-terminating proxy the "
        "app sees plain HTTP unless it trusts X-Forwarded-Proto.",
        "Behind a proxy you control, create the app with Jongo(..., trust_proxy=True).",
    ),
    "J508": _e(
        "Status endpoint refused",
        "/_jongo/status is open in dev mode only. In production it needs a signed-in admin or the "
        "JONGO_STATUS_TOKEN bearer token, so it is never exposed unauthenticated.",
        "Set JONGO_STATUS_TOKEN on the server and run `jongo status --url ... --token $JONGO_STATUS_TOKEN`.",
    ),
    "J509": _e(
        "App failed to import",
        "Importing the app module raised an exception, so nothing else can run.",
        "Fix the error at the file and line shown (it's in your code or something it imports).",
    ),
    "J510": _e(
        "Admin mounted but no admin user",
        "app.admin() is mounted but no user has is_admin=True, so nobody can sign in to it.",
        "Run `jongo createadmin`.",
    ),
    "J511": _e(
        "Unsupported Python version",
        "Jongo needs Python 3.10 or newer.",
        "Install a newer Python and recreate the virtualenv.",
    ),
    "J512": _e(
        "Can't reach the running server",
        "`jongo status` couldn't connect to the URL — the server isn't running there, or it isn't a Jongo app.",
        "Start the app (`jongo dev`) and pass its address: jongo status --url http://127.0.0.1:8000",
    ),
}


def explain(code: str) -> dict | None:
    """The catalogue entry for ``code`` (case-insensitive), with the code and its category."""
    code = (code or "").strip().upper()
    if code.isdigit():  # `jongo explain 302` means J302
        code = "J" + code.zfill(3)
    entry = CATALOGUE.get(code)
    if entry is None:
        return None
    return {"code": code, "category": CATEGORIES.get(code[:2], "General"), **entry}


def hint_for(code: str | None) -> str | None:
    entry = CATALOGUE.get(code or "")
    return entry["fix"] if entry else None


def format_explain(code: str) -> str | None:
    """``jongo explain CODE`` as text."""
    entry = explain(code)
    if entry is None:
        return None
    lines = [f"{entry['code']}  {entry['title']}", f"({entry['category']})", "", entry["explanation"], "",
             f"Fix: {entry['fix']}"]
    if entry.get("example"):
        lines += ["", "Example:", *("    " + line for line in entry["example"].splitlines())]
    return "\n".join(lines)


# ------------------------------------------------------------------------- classification --

_DB_SCHEMA = re.compile(
    r"no such table|no such column|has no column named|relation \S+ does not exist|"
    r"column \S+ (?:of relation \S+ )?does not exist|UndefinedTable|UndefinedColumn", re.I)
_DB_CONNECT = re.compile(
    r"unable to open database|could not connect|connection refused|connection to server|"
    r"no such host|could not translate host|database \S+ does not exist|authentication failed|"
    r"readonly database|disk I/O error", re.I)


def classify(exc: BaseException) -> tuple[str, str | None]:
    """``(code, hint)`` for any exception — Jongo's own carry theirs; others are recognised."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and _CODE.match(code) and code in CATALOGUE:
        return code, getattr(exc, "hint", None) or hint_for(code)
    names = {cls.__name__ for cls in type(exc).__mro__}
    module = type(exc).__module__ or ""
    message = str(exc)
    if "IntegrityError" in names or "UniqueViolation" in names or "NotNullViolation" in names:
        return "J305", hint_for("J305")
    if isinstance(exc, ModuleNotFoundError) and "psycopg" in message:
        return "J306", hint_for("J306")
    is_db = module.startswith(("sqlite3", "psycopg")) or "OperationalError" in names or "DatabaseError" in names
    if is_db or "ProgrammingError" in names:
        if _DB_SCHEMA.search(message) or {"UndefinedTable", "UndefinedColumn"} & names:
            return "J302", hint_for("J302")
        if _DB_CONNECT.search(message):
            return "J301", hint_for("J301")
        if is_db:
            return "J300", hint_for("J300")
    return "J000", None


def app_location(exc: BaseException, root: str | Path | None = None) -> tuple[str | None, int | None]:
    """The innermost traceback frame in the app's own files (not Jongo, not site-packages)."""
    file = getattr(exc, "file", None) or getattr(exc, "filename", None)
    line = getattr(exc, "line", None) or getattr(exc, "lineno", None)
    if file and isinstance(file, str) and not isinstance(exc, (OSError,)):
        return _relative(file, root), line
    framework = str(Path(__file__).resolve().parent)
    found = (None, None)
    for frame in _traceback.extract_tb(exc.__traceback__):
        name = frame.filename
        if name.startswith("<") or "site-packages" in name or name.startswith(framework):
            continue
        if root is not None:
            try:
                if not Path(name).resolve().is_relative_to(Path(root).resolve()):
                    continue
            except OSError:
                continue
        found = (name, frame.lineno)
    return (_relative(found[0], root), found[1]) if found[0] else found


def _relative(file: str, root) -> str:
    if root is None:
        return file
    try:
        return str(Path(file).resolve().relative_to(Path(root).resolve()))
    except (ValueError, OSError):
        return file


def short_traceback(exc: BaseException, limit: int = 6, root=None) -> list[str]:
    """The last ``limit`` frames as ``file:line in function`` strings (no locals, no source).

    App files are shown relative to ``root``, Jongo's own as ``jongo/...``, others by name.
    """
    framework = Path(__file__).resolve().parent
    out = []
    for f in _traceback.extract_tb(exc.__traceback__)[-limit:]:
        path = Path(f.filename)
        try:
            resolved = path.resolve()
            if resolved.is_relative_to(framework):
                name = "jongo/" + str(resolved.relative_to(framework))
            elif root is not None and resolved.is_relative_to(Path(root).resolve()):
                name = str(resolved.relative_to(Path(root).resolve()))
            else:
                name = path.name
        except (OSError, ValueError):
            name = path.name
        out.append(f"{name}:{f.lineno} in {f.name}")
    return out


def error_dict(exc: BaseException, root=None, *, message: str | None = None) -> dict:
    """``{code, type, message, hint, file, line, explain}`` for any exception."""
    code, hint = classify(exc)
    file, line = app_location(exc, root)
    if message is None:
        message = getattr(exc, "message", None)
        if not isinstance(message, str):
            message = str(exc)
    out = {"code": code, "type": type(exc).__name__, "message": message, "hint": hint}
    if file:
        out["file"] = file
        out["line"] = line
    out["explain"] = f"jongo explain {code}"
    return out


def codes() -> list[str]:
    return sorted(CATALOGUE)


def coded(exc: BaseException, code: str, hint: str | None = None) -> BaseException:
    """Attach a catalogue ``code`` (and ``hint``) to an exception of a standard type.

    For raise sites that keep their historical type (``ValueError``, ``LookupError``…)
    so existing ``except`` clauses still work.
    """
    exc.code = code
    exc.hint = hint or hint_for(code)
    return exc
