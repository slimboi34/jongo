# Jongo

[![PyPI](https://img.shields.io/pypi/v/jongo.svg)](https://pypi.org/project/jongo/)
[![Python](https://img.shields.io/pypi/pyversions/jongo.svg)](https://pypi.org/project/jongo/)
[![License](https://img.shields.io/pypi/l/jongo.svg)](LICENSE)

**One language for the whole web app.** Jongo is a full-stack Python web framework in the spirit of Django: ORM, migrations, auth, sessions, CSRF protection and an admin site. The difference is that your frontend is Python too. Components render on the server for a fast first paint, then compile to JavaScript and come alive in the browser. They call your server code with a plain `await`.

There's no template language, no separate JS project and no build step. It has zero dependencies.

```python
from jongo import Jongo, component, db, server, state
from jongo.html import *

app = Jongo(__name__, title="Todos", database="db.sqlite3")


class Todo(db.Model):                       # a database table
    title = db.Text(max_length=200)
    done = db.Bool(default=False)


@server                                     # a server function, callable from the browser
def add_todo(title: str) -> dict:
    return Todo.create(title=title).to_dict()


@component                                  # UI: Python here, JavaScript in the browser
def TodoList(todos):
    items = state(todos)
    draft = state("")

    async def add(event):
        todo = await add_todo(draft.value)  # runs on the server
        items.value = items.value + [todo]
        draft.value = ""

    return div(
        form(
            input_(value=draft.value, on_input=lambda e: draft.set(e.target.value)),
            button("Add"),
            on_submit=add,
        ),
        ul([li(t["title"], key=t["id"]) for t in items.value]),
    )


@app.page("/")                              # route + view + UI, together
def home():
    return TodoList(todos=Todo.all())
```

```bash
jongo dev    # → http://localhost:8000, reloads when you save
```

## Documentation

- **[The guide](docs/guide.md)** — the long version: the request model, routing, components,
  every hook, server functions, the ORM, migrations, PostgreSQL, channels, auth, security,
  testing, deployment, exactly what compiles to the browser, a troubleshooting table, form
  schemas, rate limits and sessions, OpenAPI and the MCP server.
- **[Patterns](examples/patterns/)** — sixteen complete, runnable apps: CRUD, auth, search
  as you type, pagination, optimistic UI, live chat, a live dashboard, modals with server
  validation, master-detail, uploads, background work, testing, deployment, shared
  validation, agent tools, sessions and rate limits.
- **[Benchmarks](benchmarks/)** — against Django and FastAPI, with the harness and the
  caveats.
- The docs site is itself a Jongo app: `jongo dev docs/docs_site.py --port 8790`.

## Why

In Django, one feature is spread across `models.py`, `urls.py`, `views.py`, a template, a form class and usually some JavaScript. In Jongo it's one idea in one place:

| Concern | Django | Jongo |
|---|---|---|
| URL | `urls.py` | `@app.page("/todos/<id>")` |
| View | `views.py` | the decorated function |
| Template | `.html` + template language | Python functions: `div(h1(title))` |
| Interactivity | separate JavaScript | the same component, compiled |
| AJAX endpoint | view + URL + `fetch` + JSON | `@server` function, called with `await` |
| Input validation | form classes | type hints (`title: str`, `todo: Todo`) |
| Migrations | `makemigrations` + files | `jongo migrate` diffs models against the DB |

## Quick start

```bash
pip install jongo                 # Python 3.10+
jongo new mysite
cd mysite
jongo dev
```

The example app lives in [`examples/todo/app.py`](examples/todo/app.py). It's a polished todo list with a detail page and a focus timer, all in one file.

---

## Pages and routing

```python
@app.page("/posts/<id>", title="Post")
def post(request, id: int, preview: bool = False):   # `id: int` makes the route only match numbers
    ...
```

- **Path parameters** use `<name>` or `<converter:name>`, with the converters `str int float slug path uuid`. An annotation like `id: int` picks the converter for you.
- **Arguments** are filled by name. `request` is the current request, path parameters come from the URL, and anything else is read from the query string and type-checked: a `bool` accepts `true`/`false`, `1`/`0`, `on`/`off`, `yes`/`no`, and `list[int]` collects a repeated key (`?ids=1&ids=2`). A value that doesn't fit is a 400 (`J211`).
- **A page returns** UI, `Page(ui, title=..., status=..., head=[...])`, or a `Response`, such as `redirect("/login")`.
- **Plain handlers:** `@app.get`, `@app.post` and `@app.route(path, methods=[...])` return a `Response`, HTML `str`, JSON-able `dict`/`list`, or UI.
- **Layouts:** `@app.layout` wraps every page in a component. When a link switches pages, the layout's state survives.
- **Errors:** raise `NotFound("...")`, `Forbidden()` or `HTTPError(status, message)`, and customise the page with `@app.errorhandler(404)`.
- **Access control:** `login_required=True` and `admin_required=True` work on pages and routes. An anonymous visitor is redirected to `login_url` (`/login` by default) with `?next=`; that page is yours to write (see [Auth and admin](#auth-and-admin)).

Same-origin links are handled by the client-side router. It fetches the next page as JSON and patches the DOM, with no full reload. Opt out with `a(..., data_reload=True)`.

## Components

```python
@component
def Card(title, children, tone="plain"):
    open_ = state(True)
    return section(
        h2(title, on_click=lambda e: open_.set(not open_.value)),
        open_.value and div(children),
        class_=["card", {"card-warning": tone == "warning"}],
        style={"padding": 16, "border_radius": 12},
    )
```

**Elements** come from `from jongo.html import *`.

- **Children:** positional arguments are children, which can be strings, elements, lists or `None`.
- **Attributes:** keyword arguments become attributes.
  - `class_` accepts a string, list or `{name: condition}` dict.
  - `style` takes a dict. Snake_case becomes kebab-case, and numbers get `px`.
  - `on_click`, `on_input`, `on_submit`… attach event handlers. Submit handlers call `preventDefault()` for you.
  - `aria_label`, `data_id` → `aria-label`, `data-id`.
- **Name clashes:** Python builtins get a trailing underscore: `input_`, `del_`, `map_`.
- **Other helpers:** `key=` for list items, `raw(html)` for trusted markup, `h("my-element")` for custom tags.

**Hooks and browser helpers:**

| | |
|---|---|
| `state(initial)` | reactive value. Set `.value`, or call `.set(v)` / `.update(fn)`, to re-render |
| `effect(fn, deps=None)` | runs in the browser after render; `deps=[]` runs it once; return a cleanup function |
| `ref()` | pass as `ref=` to get the DOM node in `.current` |
| `navigate(url)` / `refresh()` | client-side navigation / re-run the current page |
| `form_values(event)` | dict of a form's fields |
| `js.window`, `js.localStorage`, `js.fetch`… | browser globals; `e.prevent_default()` maps to `preventDefault()` |

### Python that runs in the browser

The compiler supports most everyday Python:
- functions (default, `*args`, keyword-only and `**kwargs` parameters), `lambda`, closures and `nonlocal`
- `if`/`elif`/`else`, `for`/`while` loops with `else`, `try`/`except`/`finally`, `raise`
- list/dict/set comprehensions, generator expressions, the walrus operator, unpacking
- f-strings with format specs, `%` formatting and `str.format`
- `async`/`await`

It keeps Python semantics:
- Empty lists are falsy.
- `[1] + [2]` concatenates.
- `xs[-1]` indexes from the end.
- `-7 // 2 == -4`.
- `==` compares structures.
- A missing dict key raises `KeyError`.

The common methods of `str`, `list`, `dict` and `set` work, as do the `math`, `random`, `json` and `time` modules. A test suite runs the same functions in Python and in Node and requires identical results.

Anything that can't run in a browser is a **compile error with a hint**, reported at startup:
- classes
- `with`
- imports inside functions
- server-only modules
- touching a database model directly

One deliberate improvement: closures created in a `for` loop capture each item, so `button(on_click=lambda e: remove(todo))` in a loop does what you mean.

## Server functions

```python
@server
def rename(request, todo: Todo, title: str) -> dict:   # `todo: Todo` loads the row, 404 if missing
    if request.user is None:
        raise HTTPError(401, "Log in first")
    todo.title = title
    todo.save()
    return todo.to_dict()
```

- **In a component**, call it with `await rename(todo_id, "New title")`. Keyword arguments work too.
- **Arguments are validated** against the type hints before your code runs. Supported hints: `str`, `int`, `float`, `bool`, `list[...]`, `tuple[...]`, `set[...]`, `dict[...]`, `Optional` and unions, `Literal`, dataclasses, models, a `forms.Schema`, and (sent as strings) `date`, `datetime`, `time`, `UUID`, `Decimal`, `bytes` (base64) and `Enum`. Any other annotation is refused when the function is defined (`J405`); use `Any` to take any JSON.
- **Return values** can be JSON-able data or UI elements (rendered in the browser), or a `redirect(url)` that navigates: a path on this site or an `http(s)` URL (a `javascript:` target is refused).
- **Failures** raise `ServerError` in the browser, with `.status`, `.type` and field `.errors`.
- **Options:** `@server(login_required=True)`, `@server(admin_required=True)`, `@server(refresh=True)` to re-run the page loader after each call, and `@server(rate_limit="5/minute")` to cap one caller (429 over the limit).
- **Security:** every call is CSRF-protected. Only functions you decorate are exposed.

## Forms: one set of rules, enforced twice

Validation usually exists twice, in JavaScript for speed and on the server for safety, and
the copies drift. Here there is one definition, because the browser runs compiled Python:

```python
from jongo import forms

Signup = forms.Schema(
    "Signup",
    email=forms.Email(),
    seats=forms.Int(min=1, max=50, default=1),
    password=forms.Text(min_length=8, secret=True),
    confirm=forms.Text(same_as="password", secret=True),
)

@server
def sign_up(request, data: Signup) -> dict:        # validated first; `data` is the cleaned dict
    return Account.create(email=data["email"], seats=data["seats"]).to_dict()

@component
def SignupForm():
    errors = state({})

    async def submit(event):
        values = form_values(event)
        found = forms.validate(Signup, values)     # in the browser: the same function, compiled
        errors.set(found)
        if not found:
            try:
                await sign_up(values)              # on the server: the same rules again
            except ServerError as exc:
                errors.set(exc.errors or {})

    return form([forms.Field(schema=Signup, name=n, error=errors.value.get(n), key=n) for n in Signup],
                button("Create account"), on_submit=submit)
```

- **One definition.** The schema is data, inlined into the bundle; `forms.validate` is Python the compiler turns into JavaScript. Messages are rendered once, in Python, and patterns are rewritten to syntax both regex engines read the same way, or refused when the schema is defined.
- **The server never trusts the browser.** A `@server` parameter annotated with a schema is validated again and arrives cleaned: typed, stripped, and holding only the fields the schema declares.
- **From a model:** `forms.Schema.from_model(Invite)` reads `max_length`, `choices` and types from the table. Pick fields with `only=[...]` or `exclude=[...]`; `is_admin`, `is_staff`, `is_superuser` and `password` are left out unless `only=` names them.
- **Checked, not asserted:** a test runs every rule against a table of awkward values in CPython and in the compiled bundle under Node, and requires identical answers.

## Real-time

Push from a server function straight into a mounted component. Declare the channel, and
the declaring function authorises each subscription — an undeclared channel cannot be
subscribed to at all, the same rule that keeps undecorated functions off the RPC boundary.

```python
@app.channel("room:<int:id>")
def room(request, id):
    return id in request.session.get("rooms", [])

@server
def post(request, room: int, text: str) -> dict:
    message = Message.create(room_id=room, text=text).to_dict()
    broadcast(f"room:{room}", message)          # -> every subscribed browser
    return message

@component
def Chat(room):
    messages = state([])
    live(f"room:{room}", lambda message: messages.set([*messages.value, message]))
    return ul([li(m["text"], key=m["id"]) for m in messages.value])
```

`live()` is a hook like `state()` and `effect()`: the subscription opens when the component
mounts, moves when the channel changes, and closes when it unmounts. One SSE connection per
tab carries every channel the page asked for, and reconnects with backoff if it drops.

- It is a **live feed, not a queue**: a browser that reconnects sees what happens next, not
  what it missed, and a connection that falls more than 100 messages behind sheds its oldest.
- The hub is **per process**. With one worker and threads (the default `jongo run`) a
  broadcast reaches every connection. Across multiple worker processes, each worker only
  reaches its own connections — put a shared bus in front if you need that.
- Each streaming connection holds a worker thread, so raise `--threads` for a chatty app.
- A tab may listen to at most `live.MAX_CHANNELS` (20) channels, with names of at most
  `live.MAX_CHANNEL_LENGTH` (200) characters. The one stream counts every `live()` on the
  page together, so give a list one channel and put the row id in the message, rather than
  a channel per row; past the limit the stream is refused (403, `J210`).

## Styles

```python
from jongo import css, global_css

s = css(
    card={"padding": 16, "border_radius": 12, ":hover": {"background": "#fafafa"},
          "& h2": {"margin": 0}, "@media (max-width: 600px)": {"padding": 8}},
)
div(h2("Hi"), class_=s.card)        # class="card-3f9a1c"
```

Class names are scoped. All stylesheets are served together from `/_jongo/app.css`.

## Database

```python
class Author(db.Model):
    name = db.Text(max_length=100, unique=True)

class Book(db.Model):
    title = db.Text(max_length=200)
    author = db.ForeignKey(Author, related_name="books")
    published = db.Date(null=True)
    tags = db.JSON(default=list)

    class Meta:
        ordering = ["-published"]

Book.filter(author__name__icontains="le guin", published__gte=date(1970, 1, 1)).exclude(tags=[])[:10]
Book.filter(db.Q(title__startswith="The") | db.Q(tags__contains="classic")).count()
author.books.create(title="The Dispossessed")
with db.transaction():
    ...
```

- **Fields:** `Text Int Float Bool DateTime Date JSON ForeignKey`.
- **Lookups:** `exact iexact contains icontains startswith endswith gt gte lt lte in isnull ne`, plus relation traversal with `__`.
- **Migrations have no files.** `jongo migrate` compares your models to the live schema.
  - It creates tables, adds columns and indexes, and changes a column when its type changes.
  - It only drops columns when you pass `--allow-destructive`.
  - `jongo migrate --plan` shows the SQL first.
  - `jongo dev` applies the safe changes automatically.

### SQLite or PostgreSQL

SQLite is the default and needs no setup. Point the same models at PostgreSQL with a URL:

```python
app = Jongo(__name__, database="postgres://user:pw@localhost/app")
# or: JONGO_DATABASE=postgres://user:pw@localhost/app
```

```bash
pip install "jongo[postgres]"    # adds the psycopg driver; the core stays dependency-free
```

Nothing else changes — the same models, queries, migrations and tests run on both, and the
test suite is run against both. The backends differ only where the database does:

| | SQLite | PostgreSQL |
|---|---|---|
| Changing a column | rebuilds the table | `ALTER TABLE … ALTER COLUMN` |
| Dropping a column | rebuilds when the column is referenced | drops in place |
| Case-sensitive `contains` | `GLOB` | `LIKE` |
| Case-insensitive `icontains` | `LIKE` | `ILIKE` |
| New row ids | `AUTOINCREMENT` | identity column, kept in step with explicit ids |

Both store the same representations — datetimes and dates as ISO-8601 text, booleans as
integers, JSON as text — so a model behaves identically on either one. That is a deliberate
trade for consistency: it means no value changes meaning when you move an app from SQLite to
PostgreSQL, at the cost of not using `timestamptz`/`jsonb` natively.

## Auth and admin

```python
from jongo.auth import User, authenticate, login, logout

app.admin()                                  # generated admin at /admin
```

- **Users:** `User.create_user(username, password, **fields)` for sign-up (it hashes the password; never `User.create(password=...)`, which `save()` refuses with `J304`), then `authenticate(username, password, request)`, `login(request, user)` and `logout(request)`. `request.user` is available in pages, routes and server functions.
- **The sign-in page is yours.** `login_required` redirects to `login_url` (`/login` by default) with `?next=`; define that page, and after signing in redirect to `next` only if `is_local_url(next)` (`from jongo import is_local_url`), so a link can't send your users to another site. [Pattern 02](examples/patterns/02_auth.py) is a complete one.
- **Passwords** use PBKDF2-SHA256. Changing a password signs out the user's other sessions.
- **Password guessing is throttled** with no configuration: after 10 wrong passwords for a username in 5 minutes, `authenticate()` stops checking and answers 429.
- **Sign-ins are revocable.** Each `login()` is a row on the server: `auth.sessions(user)` lists them, `auth.revoke_session(id, user=user)` signs one browser out, `auth.revoke_sessions(user)` signs out everywhere, and `logout()` kills the cookie on the server, not just in the browser. `jongo sessions` does the same from the command line.
- **The admin site** lists, searches, sorts, creates, edits and deletes rows for every model, with forms built from your field types. Create the first account with `jongo createadmin`.

## Sessions, CSRF, security

- **Sessions** are HMAC-signed cookies: `request.session["cart"] = [...]`. Set `JONGO_SECRET_KEY` in production. In dev, a key is generated into `.jongo/secret`.
- **Rate limits:** `rate_limit="5/minute"` on `@server`, `@app.page` or `@app.route` caps one caller: every request counts against the client address and, when someone is signed in, against that user too, and either being over refuses it. `ratelimit.check(request, "3/hour", "reset", key=email)` for your own key. Counters are per process.
- **Channel guards fail closed:** a guard must return `True` to allow a subscription; returning nothing refuses it.
- **CSRF:** unsafe requests need a token. Browser code sends it automatically. Classic HTML forms need `input_(type="hidden", name="csrf_token", value=request.csrf_token)`. Cross-origin `Origin` headers are rejected.
- **Host header:** `jongo dev` answers requests addressed to `localhost` or an IP address and refuses other host names (400, `J214`), which stops DNS rebinding; `Jongo(allowed_hosts=["myapp.example.com"])` lists more names (`".example.com"` takes subdomains too), and turns the check on in production.
- **Framing:** pages carry `X-Frame-Options: SAMEORIGIN` (and `frame-ancestors 'self'` when there's no CSP); `Jongo(frame_options="DENY")` is stricter and `frame_options=None` opts out.
- **Request limits:** bodies up to `Request.max_body_size` (16 MB) and forms up to `Request.max_form_fields` / `Request.max_form_parts` (1000 each); more is a 413 (`J212`) before anything is parsed.
- **Redirects:** `is_local_url(url)` is true only for a path on this site; check user-supplied redirect targets with it.
- **Escaping:** text is always HTML-escaped. `raw()` is the explicit escape hatch.
- **Data sent to the browser:** props are converted to JSON. `User.to_dict()` never includes password hashes.

## Testing

`TestClient` uses the database the app is configured with, so the `app` fixture gives every
test a fresh in-memory one instead of `db.sqlite3`. Put it in `conftest.py` next to `app.py`
(`jongo new` writes the same fixture into `tests/conftest.py`):

```python
import pytest
from jongo import db

import app as todo_module


@pytest.fixture
def app():
    db.configure(":memory:")    # a new, empty database for each test
    db.migrate()                # with every model's table
    yield todo_module.app
    db.close_connections()
```

Then, in a test file that does `from app import add_todo`:

```python
def test_add(app):
    client = app.test_client()
    assert client.get("/").status == 200           # the status code, as an int
    todo = client.rpc(add_todo, "Write tests")     # full HTTP round trip, CSRF included
    assert todo["title"] == "Write tests"          # a refused call raises jongo.ServerError
    assert client.navigate("/")["title"] == "Todos"
```

## AI coding agents

The author of a web app is increasingly a model, and a model needs what a new colleague
needs: the map of the app in one document it can load at once, and a feedback loop whose
output it can act on. Jongo generates both from the running code, so neither can go stale:

```bash
jongo context               # the rules of the framework, then this app's map: models, server
                            # functions with their signatures, pages, components, channels
jongo context -o AGENTS.md  # write it for the agent            jongo context --json   # as data
jongo check --json          # compile every component, resolve every server function's type
                            # hints, plan migrations; problems as {kind, message, file, line, hint}
```

When something goes wrong, every error says which one it is: a stable code (`J302`), a hint,
and the file and line, on the dev error page, in server-call errors, in `jongo check` and in
every CLI message.

```bash
jongo explain J302          # what the code means, why it happens, how to fix it (--list for all)
jongo doctor --json         # one health report: the app loads, components compile, database
                            # reachable, pending migrations, production settings; exit 1 on failure
jongo status --json         # a running server: recent requests and errors (code, traceback,
                            # request id), per-route counters, channels, likely N+1 queries
```

Across 19 scripted broken apps, 0.5.0 attaches a stable code and a fix hint to every failure
(0.4.0: no codes, hints for 3) — the method and the full table are in
[benchmarks/diagnostics/RESULTS.md](benchmarks/diagnostics/RESULTS.md).

**The app is a set of tools.** `jongo mcp` serves the app to an agent over the Model Context
Protocol: the reports above as tools, plus one tool per `@server` function, with input schemas
generated from the type hints. A tool call passes through the same checks as a call from the
browser, and a failure comes back with its Jongo code and hint.

```bash
claude mcp add myapp -- jongo mcp     # stdio: nothing to add to the app
jongo openapi -o openapi.json         # the same contract as an OpenAPI 3.1 document
```

```python
app.mcp(tools=[add_todo, list_todos])   # or serve it from the running app, at POST /_jongo/mcp
```

The HTTP endpoint exists only if you mount it, answers this machine only under `jongo dev`,
and needs `Authorization: Bearer $JONGO_MCP_TOKEN` in production.

`jongo new` writes an `AGENTS.md` for the project (the convention Codex, Cursor and most
agents read) and a `CLAUDE.md` that imports it, so Claude Code knows the rules the moment it
opens the folder. The reasons this shape suits agents are the reasons it suits people: one
language, one file, type-checked boundaries, and compile errors at start-up instead of
surprises in a browser. From Python: `jongo.describe(app)`, `jongo.context(app)`,
`jongo.doctor(app)`, `jongo.status(app)`, `jongo.explain(code)`, `jongo.openapi(app)` and
`jongo.json_schema(hint)`.

## CLI

| Command | |
|---|---|
| `jongo new NAME` | create a project |
| `jongo dev [app.py] [--port]` | dev server: auto-reload, live browser reload, error overlay, debug pages |
| `jongo run [--host --port --migrate]` | production server (threaded) |
| `jongo migrate [--plan] [--allow-destructive]` | sync the schema |
| `jongo createadmin` | create an admin user |
| `jongo routes` | list routes |
| `jongo shell` | Python shell with your models |
| `jongo build` | compile components and report errors (good for CI) |
| `jongo check [--json]` | compile, resolve server-function hints, plan migrations; problems as data |
| `jongo context [--json] [-o FILE]` | describe the app for an AI coding tool |
| `jongo doctor [--json] [--production]` | health report on the app, database, settings and environment |
| `jongo explain CODE` | explain an error code and its fix (`--list` for all) |
| `jongo status [--url URL] [--json]` | what a running server is doing: requests, errors, routes, channels |
| `jongo openapi [-o FILE]` | the OpenAPI 3.1 document, generated from the type hints |
| `jongo mcp [--list] [--read-only] [--user NAME]` | serve the app to a coding agent over MCP (stdio) |
| `jongo sessions [--user NAME] [--revoke ID] [--revoke-user NAME]` | list or revoke sign-ins |

With `--json`, a command prints JSON even when it fails: `{"ok": false, "error": {"code", "message", "hint", "file", "line"}}`, exit 1.

## Deployment

`app` is a standard WSGI application:

```bash
JONGO_SECRET_KEY=... gunicorn app:app        # or: jongo run --port 8000 --migrate
```

Run `jongo doctor --production` first: it fails on a weak secret key or dev mode left on, and
warns when the key only lives in `.jongo/secret` (set `JONGO_SECRET_KEY` instead). `/_jongo/status` is never open in production; set `JONGO_STATUS_TOKEN` to read it
with `jongo status --url ... --token ...`, or sign in as an admin.

## How it works

```
 request ─▶ route ─▶ page function ─▶ UI tree ─┬─▶ rendered to HTML on the server ─▶ fast first paint
                                               └─▶ serialised as JSON ──────────────▶ browser hydrates it
                                                                                      with components compiled
 @component (Python source) ──ast──▶ JavaScript ──▶ /_jongo/app.js                  from the same Python
 @server call in browser ──POST /_jongo/rpc/<id> (CSRF, JSON, type-checked)──▶ your function
```

- `jongo/compiler/` turns Python ASTs into JavaScript. Free names resolve against the live Python objects, so the compiler knows whether `add_todo` is a server function, a component, a helper or a constant.
- `jongo/compiler/pyrt.js` provides Python semantics in the browser.
- `jongo/compiler/dom.js` is the virtual DOM, hooks, keyed diffing, hydration and router, in about 700 lines with no dependencies.
- `jongo/vdom.py` is the same tree model on the server.

## Developing Jongo

```bash
python3 -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/python -m pytest              # needs `node` for the compiler parity tests
cd examples/todo && ../../.venv/bin/jongo dev
```

## Status

Version 0.6 (see `CHANGELOG.md`). Known limits:
- Rate limits and the login throttle count in each process's memory: exact with one worker, per worker with several, cleared by a restart.
- Form schemas validate flat fields (no nested objects or lists).
- The MCP server offers tools and resources over stdio and plain JSON-over-HTTP; it doesn't push (no server-sent stream, no prompts, no sampling).
- No WebSockets: real-time channels run over server-sent events, and the hub is per process.
- No classes in browser code.
- Components re-render their subtree without memoisation.
- `x__ne=v` / `.exclude(field=v)` also match rows where the column is `NULL` (matching Python's `None != v`, not SQL's three-valued logic).
- Browser (post-hydration) code inherits JavaScript's value model, so a few things differ from CPython (the server is always correct): integers past 2⁵³ lose precision and `str(2.0)` shows `"2"` (one number type — format explicitly, e.g. `f"{x:.2f}"`); a dict keyed by ints iterates string keys, and integer-like keys iterate in ascending order rather than insertion order; `isinstance(5, float)` is true; generator expressions are evaluated eagerly, so `any()`/`all()` don't stop early. Do exact numeric/big-integer work in a `@server` function.
- The first argument to `Jongo()` should be `__name__` (or pass `root=`): the database file, `.jongo/secret` and `static/` are found next to that module.

Bug reports and ideas are welcome.

## License

Jongo is Copyright © 2026 Joshua Harty, and is licensed under the **GNU Affero General
Public License v3.0 or later** (AGPL-3.0-or-later) — see [LICENSE](LICENSE). You are free
to use, study, share and modify it, but any modified version you distribute or make
available to users over a network must also be released, in full source form, under the
AGPL. This keeps Jongo open and prevents it from being folded into closed, proprietary
products. Versions 0.1.0–0.2.2 were released under the MIT License and remain available
under those terms.
