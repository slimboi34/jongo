# The Jongo guide

Jongo is a full-stack Python web framework. Routes, database models, server logic and
reactive UI are written in one language, usually in one file. A component renders on the
server for the first paint, then compiles to JavaScript and hydrates in the browser, where
it holds state like any single-page app. From the browser you call a server function with a
plain `await`.

This guide is the long version. The [README](../README.md) is the short one, and
[examples/patterns](../examples/patterns/) has thirteen runnable apps.

- [1. Install and first app](#1-install-and-first-app)
- [2. The mental model](#2-the-mental-model)
- [3. Pages and routing](#3-pages-and-routing)
- [4. Components](#4-components)
- [5. Hooks: state, effect, ref, live](#5-hooks-state-effect-ref-live)
- [6. Events and forms](#6-events-and-forms)
- [7. Server functions](#7-server-functions)
- [8. Styles](#8-styles)
- [9. The database](#9-the-database)
- [10. Migrations](#10-migrations)
- [11. PostgreSQL](#11-postgresql)
- [12. Real-time channels](#12-real-time-channels)
- [13. Auth, sessions and the admin](#13-auth-sessions-and-the-admin)
- [14. Security](#14-security)
- [15. Testing](#15-testing)
- [16. The CLI](#16-the-cli)
- [17. Deployment](#17-deployment)
- [18. What compiles to the browser](#18-what-compiles-to-the-browser)
- [19. When something goes wrong](#19-when-something-goes-wrong)

---

## 1. Install and first app

```bash
pip install jongo                 # zero dependencies
pip install "jongo[postgres]"     # add the psycopg driver if you want PostgreSQL
jongo new myapp && cd myapp
jongo dev
```

The whole of a working app:

```python
from jongo import Jongo, component, db, server, state
from jongo.html import button, div, form, h1, input_, li, ul

app = Jongo(__name__, title="Todos", database="app.sqlite3")


class Todo(db.Model):                      # a table
    title = db.Text(max_length=200)
    done = db.Bool(default=False)


@server                                    # callable from the browser with await
def add_todo(request, title: str) -> dict:
    return Todo.create(title=title).to_dict()


@component                                 # Python here, JavaScript in the browser
def TodoList(todos):
    items = state(todos)
    draft = state("")

    async def add(event):
        todo = await add_todo(draft.value)
        items.set([todo, *items.value])
        draft.set("")

    return form(
        input_(value=draft.value, on_input=lambda e: draft.set(e.target.value)),
        button("Add"),
        ul([li(t["title"], key=t["id"]) for t in items.value]),
        on_submit=add,
    )


@app.page("/")                             # a route and a page in one
def home():
    return div(h1("Todos"), TodoList(todos=[t.to_dict() for t in Todo.all()]))
```

Four building blocks: `@app.page`, `@component`, `@server`, `db.Model`.

---

## 2. The mental model

A request goes through four steps. Knowing them explains nearly every rule in this guide.

1. **A request arrives.** Your page function runs on the server and returns a UI tree —
   plain Python calling `div()`, `h1()` and your components.
2. **Server render.** That tree renders to HTML for an instant first paint, and is
   serialised to JSON so the browser can pick up where the server left off. Search engines
   and readers without JavaScript get real HTML.
3. **Hydrate.** The browser loads one bundle — the runtime plus your components, compiled
   from the same Python (AST → JavaScript) — adopts the server's DOM and makes it
   interactive. There is no build step you run; the compiler runs at start-up and reports
   browser-incompatible Python as an error before a request is served.
4. **Call the server.** An event handler does `await save(todo)`. Jongo turns that into a
   CSRF-protected, type-checked POST to your `@server` function. A model parameter loads
   the row. You never write an endpoint, a fetch or a serializer.

The one rule that follows from this: **a component runs in both places, so it can only do
what both places can do.** No database access, no file system, no secrets inside a
component. Load data in the page function or a `@server` function and pass it in as props.

```python
@component
def Bad():
    rows = Todo.all()          # compile error: there is no database in the browser
    return ul(...)

@app.page("/")
def good():
    return List(rows=[t.to_dict() for t in Todo.all()])    # load here, pass as props
```

---

## 3. Pages and routing

```python
@app.page("/")
def home():
    return div("hi")

@app.page("/posts/<int:id>")
def post(id: int):
    return div(Post.get(id=id).title)

@app.page("/admin/reports", login_required=True, admin_required=True, title="Reports")
def reports(request):
    return div(request.user.username)
```

- Converters: `<str:x>` (default), `<int:x>`, `<float:x>`, `<slug:x>`, `<uuid:x>`,
  `<path:x>`. A type annotation on the parameter works too: `/posts/<id>` with `id: int`.
- A page function may take `request` and/or its path parameters; take what you need.
- `@app.route(path, methods=("POST",))` registers a plain handler for forms, webhooks and
  files. Return a `Response`, a `str` of HTML, a `dict`/`list` (sent as JSON) or a UI tree.
- `@app.get` and `@app.post` are shorthands.
- More specific patterns win: `/o/special` beats `/o/<thing>`.

Navigation inside the app is intercepted, so links do not reload the page:

```python
a("Next", href="/page/2")          # intercepted; URL, history and back button all work
navigate("/page/2")                 # the same, from code in the browser
refresh()                           # re-fetch the current page from the server
redirect("/login")                  # a server-side redirect, from a route
```

Layouts wrap every page:

```python
@app.layout
def Shell(children):
    return div(Nav(), main(children))
```

---

## 4. Components

A component is a function decorated with `@component` that returns UI. It takes props by
keyword, and it must be pure with respect to its props: same props, same output.

```python
@component
def Card(title, body, tone="plain"):
    return div(h2(title), p(body), class_=f"card card--{tone}")

Card(title="Hi", body="There")                     # keyword props only
```

- **Keys.** When you render a list, give each item a stable `key` so the reconciler can
  move nodes instead of rebuilding them: `li(t["title"], key=t["id"])`.
- **Children.** Pass elements as positional arguments; a component that accepts children
  takes them as a `children` prop.
- **Conditional UI** is just Python: `p("...") if error else None`. `None` renders nothing.
- **Fragments**: `fragment(a, b)` groups siblings without a wrapper element.
- **Escaping.** Text is HTML-escaped. `raw("<b>x</b>")` is the explicit, visible opt-out.

Props cross the wire as JSON: they can be `None`, `bool`, `int`, `float`, `str`, `list`,
`dict`, `datetime`/`date` (as ISO strings), `Decimal` (as a float) and nested combinations.
Pass `todo.to_dict()`, not a model instance.

---

## 5. Hooks: state, effect, ref, live

Hooks are called at the top level of a component, in the same order on every render.

### state

```python
count = state(0)
count.value            # read
count.value += 1       # write, and schedule a re-render
count.set(5)           # the same
count.update(lambda n: n + 1)
```

A re-render is scheduled, not immediate: several updates in one handler produce one render.

> **Gotcha.** `state(prop)` seeds the state once, on mount. If the prop changes later, the
> state does not follow it. Render values that come from the server straight from props,
> and keep in state only what the browser owns.

### effect

```python
effect(lambda: js.document.title == title, [title])   # runs after render, in the browser
```

- `effect(fn)` runs after every render; `effect(fn, [a, b])` runs when `a` or `b` changes;
  `effect(fn, [])` runs once on mount.
- Return a function to clean up: it runs before the next run and on unmount.
- Effects never run during server rendering.

### ref

```python
box = ref(None)
input_(ref=box)             # box.current is the DOM node after mount
box.current.focus()
```

A ref is also a mutable box for values that must survive a render without causing one — a
timer handle, a previous value, a scroll position.

### live

```python
live("room:12", lambda message: messages.set([*messages.value, message]))
```

Subscribes this component to a channel; see [§12](#12-real-time-channels).

---

## 6. Events and forms

Handlers are ordinary functions passed as props. `on_click`, `on_input`, `on_change`,
`on_submit`, `on_key_down` — any DOM event, in `snake_case`.

```python
button("Save", on_click=save)
input_(value=name.value, on_input=lambda e: name.set(e.target.value))
form(..., on_submit=submit)        # Jongo calls preventDefault() for you
```

`async def` handlers work: that is how you `await` a server function.

`form_values(event)` collects a whole form as a dict:

```python
async def submit(event):
    values = form_values(event)
    await create_user(values["email"], values["password"])
```

> **Gotcha.** Pass `lambda v: total.set(v)`, not the bare method `total.set`, as a callback
> to another component — a bare method loses its receiver when it crosses into JavaScript.

A classic HTML form (no JavaScript) posts to a route and needs the CSRF token as a hidden
input:

```python
form(input_(type="hidden", name="csrf_token", value=request.csrf_token),
     input_(name="email"), button("Sign up"), method="post", action="/signup")
```

---

## 7. Server functions

```python
@server
def rename(request, todo: Todo, title: str) -> dict:
    todo.title = title
    todo.save()
    return todo.to_dict()
```

From the browser:

```python
updated = await rename(todo["id"], new_title)
```

- **Only decorated functions are reachable.** Nothing else is exposed.
- **Arguments are checked against your type hints before your code runs.** A wrong type is
  a clean 400, not a crash inside your function. Supported: `str`, `int`, `float`, `bool`,
  `list[...]`, `dict[K, V]`, `None`-able types, and model classes.
- **A model parameter loads the row** by id, or returns 404 if it does not exist.
- **`request` is always the first parameter**, with `request.user`, `request.session` and
  the rest.
- **Return** anything JSON-able, or `redirect(...)`.
- **Raise** `Forbidden` or `NotFound` to send a status and a message the browser can read,
  or `db.ValidationError({"email": "..."})` to send field errors:

```python
@server
def rename(request, todo: Todo, title: str) -> dict:
    if todo.owner_id != request.user.id:
        raise Forbidden("Not your todo.")          # 403, message included
    if not title.strip():
        raise db.ValidationError({"title": "Required."})   # 400, field errors included
    ...
```

In the browser, a failed call raises `ServerError`, which carries `.type`, `.status` and
`.errors`:

```python
try:
    await rename(todo["id"], title)
except ServerError as exc:
    if exc.errors:
        field_errors.set(exc.errors)
    else:
        error.set(str(exc))
```

> **Any other exception is reported to the browser as a bare "Server error"** — the real
> message and traceback appear in the server log, and in the browser only when `dev` is on.
> That is deliberate: an unexpected exception must not leak internals to a visitor. So a
> message you actually want a user to read has to be raised as one of the types above, or —
> better for expected problems like a taken email — simply *returned as data*:
> `{"errors": {"email": "already taken"}}` is not an exception at all, and renders next to
> the field.

---

## 8. Styles

Each keyword to `css()` becomes one scoped class, and its value is a dict of declarations
with snake_case property names. Nested keys give you pseudo-classes, child selectors and
media queries:

```python
from jongo import css, global_css

s = css(
    card={"padding": 16, "border_radius": 12,
          ":hover": {"transform": "translateY(-1px)"},
          "& h2": {"margin": 0},
          "@media (max-width: 600px)": {"padding": 8}},
    muted={"opacity": .7, "font_size": 14},
)

div(h2("Hi"), class_=s.card)          # class="card-3f9a1c" — scoped, no collisions
div("...", class_=[s.card, s.muted])  # several classes
```

`global_css()` takes either a raw CSS string or a dict of selectors, and is where resets,
`:root` variables and `@font-face` go:

```python
global_css(""":root { --ink: #14161a; } body { margin: 0; }""")
```

One-off styles go inline with the same snake_case keys:
`style={"font_weight": 600, "max_width": "60ch"}`. All stylesheets are served together from
`/_jongo/app.css`.

---

## 9. The database

```python
class Author(db.Model):
    name = db.Text(max_length=100, unique=True)
    joined = db.DateTime(auto_now_add=True)


class Book(db.Model):
    title = db.Text(max_length=200, index=True)
    author = db.ForeignKey(Author, related_name="books", on_delete="CASCADE")
    published = db.Date(null=True)
    tags = db.JSON(default=list)

    class Meta:
        table = "books"
        ordering = ["-published"]
```

**Fields:** `Text Int Float Bool DateTime Date JSON ForeignKey`. Common options:
`null`, `blank`, `default`, `unique`, `index`, `choices`, `max_length`, `label`,
`help_text`; `DateTime` adds `auto_now` and `auto_now_add`; `ForeignKey` adds
`related_name` and `on_delete` (`CASCADE`, `SET NULL`, `RESTRICT`, `NO ACTION`).

**Queries** are lazy and chainable; nothing runs until you iterate, slice or count.

```python
Book.all()
Book.filter(title__icontains="wind", published__gte=date(1970, 1, 1))
Book.exclude(tags=[]).order_by("-published")[:10]
Book.filter(db.Q(title__startswith="The") | db.Q(tags__contains="classic")).count()
Book.filter(author__name__iexact="le guin")          # follow a relation with __
Book.get(id=3)                                        # DoesNotExist / MultipleObjectsReturned
Book.filter(slug="x").first()                         # None when there is no match
Book.filter(draft=True).exists()
Book.filter(done=False).update(done=True)
Book.filter(draft=True).delete()
author.books.create(title="The Dispossessed")         # reverse accessor
```

**Lookups:** `exact iexact contains icontains startswith istartswith endswith iendswith
gt gte lt lte in isnull ne`.

**Transactions:**

```python
with db.transaction():
    author.save()
    book.save()          # both, or neither — nested blocks use savepoints
```

**Validation** runs on `save()`/`create()` and raises `db.ValidationError` with an `errors`
dict keyed by field name. Database-level races (a `UNIQUE` collision between the check and
the insert) surface as the same `ValidationError`, not a raw driver error.

---

## 10. Migrations

There are no migration files. `jongo migrate` compares your models to the live schema and
applies the difference.

```bash
jongo migrate --plan                 # show the SQL first
jongo migrate                        # apply the safe changes
jongo migrate --allow-destructive    # also drop columns
```

- It creates tables, adds columns, and adds or drops the indexes your fields describe.
- It changes a column whose type or nullability changed — with `ALTER` on PostgreSQL, by
  rebuilding the table on SQLite (which cannot alter much) while preserving the data,
  extra columns and children.
- It never drops a column unless you ask.
- Making a column `NOT NULL` fills existing NULLs from the field's default.
- `jongo dev` applies the safe changes automatically on reload.
- Adding a required foreign key to a table that already has rows is refused with an
  explanation: give it `null=True` or a default and fill it in yourself.

---

## 11. PostgreSQL

```python
app = Jongo(__name__, database="postgres://user:pw@localhost/app")
# or leave it out and set JONGO_DATABASE
```

```bash
pip install "jongo[postgres]"
```

The same models, queries, migrations and tests run on both backends, and the test suite is
run against both. Differences are handled for you:

| | SQLite | PostgreSQL |
|---|---|---|
| Changing a column | rebuilds the table | `ALTER TABLE … ALTER COLUMN` |
| Dropping a column | rebuilds when referenced | drops in place |
| `contains` (case-sensitive) | `GLOB` | `LIKE` |
| `icontains` | `LIKE` | `ILIKE` |
| `!=` including NULLs | `IS NOT ?` | `IS DISTINCT FROM ?` |
| New ids | `AUTOINCREMENT` | identity column, kept in step with explicit ids |

Both store the same representations — datetimes and dates as ISO-8601 text, booleans as
integers, JSON as text — so no value changes meaning when an app moves from one to the
other. The trade is that the PostgreSQL schema does not use `timestamptz`/`jsonb` natively.

Run the test suite against PostgreSQL with:

```bash
JONGO_TEST_DATABASE=postgres://localhost/jongo_test python -m pytest
```

---

## 12. Real-time channels

```python
@app.channel("room:<int:id>")
def room(request, id):
    return id in request.session.get("rooms", [])     # False or Forbidden refuses


@server
def post(request, room: int, text: str) -> dict:
    message = Message.create(room_id=room, text=text).to_dict()
    broadcast(f"room:{room}", message)
    return message


@component
def Chat(room):
    messages = state([])
    live(f"room:{room}", lambda message: messages.set([*messages.value, message]))
    return ul([li(m["text"], key=m["id"]) for m in messages.value])
```

- A channel that is not declared cannot be subscribed to — the rule that keeps undecorated
  functions off the RPC boundary, applied to data going the other way.
- `live()` follows the component: it subscribes on mount, moves when the channel changes,
  and unsubscribes on unmount. One SSE connection per tab carries every channel the page
  asked for, and reconnects with backoff.
- `broadcast()` returns how many connections it reached. Anything that can call a function
  can push: a request handler, a thread, a cron job, a queue worker.
- It is a **live feed, not a queue.** A browser that reconnects sees what happens next, not
  what it missed; a connection more than 100 messages behind sheds its oldest. Store
  anything that must survive a reconnection.
- The hub is **per process**. One worker with threads reaches every connection; several
  worker processes each reach only their own. Each open stream holds a thread, so raise
  `--threads` for a chatty app.

---

## 13. Auth, sessions and the admin

```python
from jongo.auth import User, authenticate, login, logout

user = User.create_user(username="ann", password="s3cret")
user = authenticate("ann", "s3cret")       # None if wrong
login(request, user)
logout(request)
request.user                                # None when signed out
```

`@app.page("/x", login_required=True)` redirects anonymous visitors to `login_url` with a
`?next=` parameter; `admin_required=True` also requires a staff account.

Sessions are HMAC-signed cookies: `request.session["cart"] = [...]`. Passwords are
PBKDF2-SHA256, salted and compared in constant time; changing one signs out that user's
other sessions.

`app.admin()` generates an admin site at `/admin` — list, search, sort, create, edit and
delete for every model, with forms built from your field types. Create the first account
with `jongo createadmin`.

---

## 14. Security

What Jongo does by default:

- **Escaping.** Every value rendered to the page is HTML-escaped. Attribute names, tag
  names and URL schemes are validated, so untrusted data cannot smuggle in a handler or a
  `javascript:` link. `raw()` is the visible opt-out.
- **A type-checked border.** `@server` arguments are validated against your hints before
  your code runs. Only decorated functions are exposed. The same applies to channels.
- **CSRF.** Every state-changing call carries a token signed with your secret and bound to
  the origin. Cross-origin `Origin` headers are rejected.
- **Sessions** are signed; **passwords** are PBKDF2-SHA256.
- **Limits.** Request bodies are capped; a bogus `Content-Length` is rejected before the
  body is read.
- **`to_dict()`** never includes password hashes.

What you still have to do: set `JONGO_SECRET_KEY` in production, authorise your own
channels and server functions (`request.user` is there, Jongo will not guess your rules),
validate uploads, and keep secrets out of components.

---

## 15. Testing

```python
from jongo.testing import TestClient

client = TestClient(app)

client.get("/")                       # a real WSGI round trip
client.post("/login", data={...})     # CSRF handled like a browser
client.rpc(add_todo, "Write tests")   # call a @server function over HTTP
client.navigate("/")                  # the JSON tree the in-browser router fetches
response, chunks = client.stream("/_jongo/live?channels=prices")   # SSE, unconsumed
```

See [12_testing.py](../examples/patterns/12_testing.py) for the whole set in one file.

---

## 16. The CLI

| Command | |
|---|---|
| `jongo new NAME` | create a project |
| `jongo dev [app.py] [--port]` | dev server: auto-reload, live browser reload, error overlay |
| `jongo run [--host --port --migrate]` | production server (threaded) |
| `jongo migrate [--plan] [--allow-destructive]` | sync the schema |
| `jongo createadmin` | create an admin user |
| `jongo routes [--json]` | list routes |
| `jongo shell` | a Python shell with your models loaded |
| `jongo build` | compile components and write the bundle |
| `jongo check [--json]` | compile, resolve server-function hints, plan migrations; problems as data, exit 1 (use this in CI) |
| `jongo context [--json] [-o FILE]` | describe the app for an AI coding tool — see [§20](#20-working-with-ai-coding-agents) |

---

## 17. Deployment

`app` is a standard WSGI application.

```bash
JONGO_SECRET_KEY=... python -m gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8
```

- **`JONGO_SECRET_KEY` is required in production** — sessions and CSRF are signed with it.
- Run `db.migrate()` at import, or start with `jongo run --migrate`, so a fresh container
  has its schema before the first request.
- Use one worker with threads if you use channels (the hub is per process).
- Set `trust_proxy=True` only behind a proxy you control; it makes Jongo believe
  `X-Forwarded-Proto`.
- Serve behind TLS. Cookies are marked `Secure` when the request is HTTPS.
- Console-script shebangs are sometimes broken by build packs: `python -m gunicorn` is the
  reliable spelling.

[13_deployment.py](../examples/patterns/13_deployment.py) is this section as a file.

---

## 18. What compiles to the browser

Component bodies and event handlers are compiled. Supported: functions, `async`/`await`,
closures, comprehensions, f-strings, `if`/`for`/`while`/`try`, unpacking, default and
keyword arguments, `*args`/`**kwargs`, and the builtins you would expect (`len`, `range`,
`enumerate`, `zip`, `sorted`, `min`, `max`, `sum`, `any`, `all`, `abs`, `round`, `str`,
`int`, `float`, `bool`, `list`, `dict`, `set`, `tuple`, `map`, `filter`, `isinstance`,
`print`). `math` and `json` are available. Anything unsupported is a **compile error at
start-up**, with the file, the line and a hint — not a surprise in someone's browser.

Not compiled, by design: `import` inside a function, classes, the database, the file
system, and modules that only exist on the server.

**Known differences in the browser**, all consequences of JavaScript's value model:

- `str()` of an integral float drops the `.0` (`str(10 / 2)` → `"5"`). Format explicitly:
  `f"{x:.1f}"`.
- A dict keyed by ints iterates *string* keys (`list({1: 'a'}.keys())` → `["1"]`); lookups
  still work.
- `isinstance` cannot tell an int from a float (`isinstance(5, float)` is True).
  `isinstance(True, int)` is correct.

Tuples are real tuples — `(1, 2) != [1, 2]`, immutable, and `enumerate`/`zip`/`.items()`
yield them — and strings are measured and indexed in code points, so `len("😀")` is 1.
Both are checked by a parity suite that runs every sample in CPython and in Node and
compares the results.

---

## 19. When something goes wrong

| What you see | What it means |
|---|---|
| `CompileError: ... isn't supported in browser code` | A component or handler uses something that cannot cross to the browser. The message names the file, the line and usually the fix. |
| `there is no database in the browser` | A component touched a model. Load rows in the page function and pass props. |
| A page renders but nothing is interactive | The bundle failed to load, or hydration threw. Check the console; `jongo dev` shows an error overlay. |
| `403` on a server call | CSRF: the token is missing or the `Origin` is cross-site. Classic forms need the hidden `csrf_token` input. |
| `403` on a channel | The channel is not declared, or its guard returned `False`. |
| State does not update after `refresh()` | `state(prop)` keeps its first value. Render server data from props. |
| A callback does nothing | Pass `lambda v: x.set(v)`, not a bare `x.set`. |
| Cookies behave oddly across ports on localhost | Browsers share cookies between ports on `localhost`. Use one port per app while developing. |

---

## 20. Working with AI coding agents

Jongo was built, tested and shipped largely by AI agents, and it is designed to be extended by
them. Three things make a framework agent-friendly, and each has a command.

**The whole feature fits in context.** One language, one file: the table, the server function
and the UI are one idea an agent can hold at once. `jongo context` prints that map for the app
you are in — every model with its fields, every `@server` function with its signature and
where it lives, pages and routes with their parameters and guards, components with their
props, channels — generated from the live registries and type hints, so it is never stale,
after a short statement of the framework's rules. Paste it into the agent, or write it down:

```bash
jongo context                 # Markdown
jongo context --json          # the map as data
jongo context -o AGENTS.md    # write it
```

**Mistakes fail loud, early, and as data.** A component that touches the database, an
`import` inside a function, a server function whose annotation cannot be resolved:
`jongo check` finds them without starting a server and reports each with the file, the line
and a hint. With `--json` the report is something an agent can act on in a loop, and the exit
code is 1 when there is a problem, so the same command gates CI:

```bash
jongo check --json
# {"ok": false, "problems": [{"kind": "compile", "message": "...", "file": "app.py",
#                              "line": 12, "hint": "..."}], "counts": {...}, "pending_migrations": [...]}
```

**The rules travel with the project.** `jongo new` writes `AGENTS.md` — the convention Codex,
Cursor and most agents read — and a `CLAUDE.md` containing `@AGENTS.md`, Claude Code's import
syntax. Both say the same three things: run `jongo context` before you start, run
`jongo check --json` after every change, and keep it in one language.

From Python, `jongo.describe(app)` returns the map as a dict and `jongo.context(app)` the
Markdown; both are what the commands print.
