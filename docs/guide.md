# The Jongo guide

Jongo is a full-stack Python web framework. Routes, database models, server logic and
reactive UI are written in one language, usually in one file. A component renders on the
server for the first paint, then compiles to JavaScript and hydrates in the browser, where
it holds state like any single-page app. From the browser you call a server function with a
plain `await`.

This guide is the long version. The [README](../README.md) is the short one, and
[examples/patterns](../examples/patterns/) has sixteen runnable apps.

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
- [20. Working with AI coding agents](#20-working-with-ai-coding-agents)
- [21. Diagnostics: doctor, explain, status](#21-diagnostics-doctor-explain-status)
- [22. Error codes](#22-error-codes)
- [23. Form schemas: one set of rules for the browser and the server](#23-form-schemas-one-set-of-rules-for-the-browser-and-the-server)
- [24. Rate limits and revocable sessions](#24-rate-limits-and-revocable-sessions)
- [25. OpenAPI and JSON Schema](#25-openapi-and-json-schema)
- [26. MCP: the app as tools for a coding agent](#26-mcp-the-app-as-tools-for-a-coding-agent)

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

Pass `__name__` as the first argument to `Jongo()`: it tells Jongo which file the app lives
in, and the database path, the `.jongo/` key file and `static/` are found next to that file.
An app built somewhere else (a factory, a test) can pass `root=` instead.

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
- Any other parameter is read from the query string and checked against its annotation:
  `page: int = 1` reads `?page=2`; a `bool` accepts `true`/`false`, `1`/`0`, `on`/`off` and
  `yes`/`no` in any case; `list[int]` collects a repeated key (`?ids=1&ids=2`); `date`,
  `datetime`, `UUID`, `Decimal` and `Enum` arrive converted. A value that doesn't fit is a
  400 (`J211`), not an exception in your function.
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

To validate a form, declare its rules once with `forms.Schema` and run them in the browser
and on the server: see [§23](#23-form-schemas-one-set-of-rules-for-the-browser-and-the-server).

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
  a clean 400 (`J403`), not a crash inside your function. Supported: `str`, `int`, `float`,
  `bool`, `list[...]`, `tuple[...]`, `set[...]`, `dict[K, V]`, `None`-able types and unions,
  `Literal[...]`, dataclasses, `TypedDict`, a `forms.Schema` ([§23](#23-form-schemas-one-set-of-rules-for-the-browser-and-the-server)),
  model classes, and, sent from the browser as strings, `date`, `datetime` and `time`
  (ISO 8601; a trailing `Z` is accepted), `UUID`, `Decimal` (finite values only), `bytes`
  (base64) and `Enum` members (by value). Any other annotation is refused when the function
  is defined (`J405`), so no argument reaches your code unchecked; annotate a parameter
  `Any` to take any JSON and check it yourself.
- **A model parameter loads the row** by id, or returns 404 if it does not exist.
- **`request` is always the first parameter**, with `request.user`, `request.session` and
  the rest.
- **Return** anything JSON-able, or `redirect(...)` to a path on this site or an `http(s)` URL.
  Any other target (`javascript:`, `data:`) is refused with a 500 (`J408`) instead of being
  handed to the browser. A target that came from the user must pass `is_local_url()` first
  ([§14](#14-security)).
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
`help` (the help text forms and the admin show; `help_text` is accepted as an alias);
`DateTime` adds `auto_now` and `auto_now_add`; `ForeignKey` adds `related_name` and
`on_delete` (`CASCADE`, `SET NULL`, `RESTRICT`, `NO ACTION`).

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
    return id in request.session.get("rooms", [])     # True allows; anything else refuses


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
- The guard has to say yes. Only a truthy return value allows the subscription; `False`
  refuses it (`J410`) and so does returning nothing (`J412`), so a guard with a forgotten
  `return` fails closed. `jongo check` reports a guard that never returns a value. (Before
  0.6.0, returning `None` allowed.)
- `live()` follows the component: it subscribes on mount, moves when the channel changes,
  and unsubscribes on unmount. One SSE connection per tab carries every channel the page
  asked for, and reconnects with backoff.
- That connection may carry at most `MAX_CHANNELS` (20) channels, and a channel name is at
  most `MAX_CHANNEL_LENGTH` (200) characters. Both live in the `jongo.live` module (import
  them with `from jongo.live import MAX_CHANNELS`; `from jongo import live` is the hook).
  Because the tab has one stream, the limit counts every `live()` on the page together:
  thirty rows that each call `live(f"row:{id}")` ask for thirty channels, and the stream is
  refused (403, `J210`, with a message naming the limit). Under `jongo dev` the browser
  reports it in the console and stops retrying. Use one channel for the list and put the row
  id in the message.
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

user = User.create_user(username="ann", password="s3cret")   # hashes the password
user = authenticate("ann", "s3cret", request)   # None if wrong; `request` adds the per-address throttle
login(request, user)
logout(request)
request.user                                # None when signed out
```

Make accounts with `User.create_user(username, password, **fields)`, and change a password
with `user.set_password(raw)` then `user.save()`. Never `User.create(password=...)`: that
would store the password as typed, so `save()` refuses a `password` that isn't a hash
(`J304`).

`@app.page("/x", login_required=True)` redirects anonymous visitors to `login_url` with a
`?next=` parameter; `admin_required=True` also requires a staff account. `login_url` is
`/login` unless you pass `Jongo(login_url=...)`, and Jongo doesn't provide that page: define
it yourself ([02_auth.py](../examples/patterns/02_auth.py) is a complete one).

`?next=` comes from the URL, so anyone can send a link whose `next` is their own site. After
signing in, redirect to it only when `is_local_url()` says it is a path on this site; it
refuses `https://…`, `//host` and `/\host`:

```python
from jongo import is_local_url             # also jongo.http.is_local_url

@app.route("/login", methods=("POST",))
def do_login(request):
    user = authenticate(request.form.get("username", ""), request.form.get("password", ""), request)
    if user is None:
        return redirect("/login?error=1")
    login(request, user)
    target = request.form.get("next") or "/"
    return redirect(target if is_local_url(target) else "/")
```

Sessions are HMAC-signed cookies: `request.session["cart"] = [...]`. Passwords are
PBKDF2-SHA256, salted and compared in constant time; changing one signs out that user's
other sessions.

Every sign-in is also recorded on the server, so it can be listed and revoked, and
`authenticate()` throttles wrong passwords on its own:
see [§24](#24-rate-limits-and-revocable-sessions).

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
  the origin. Cross-origin `Origin` headers are rejected (scheme included), and the token is
  replaced at sign-in and sign-out.
- **The Host header.** `jongo dev` answers requests addressed to `localhost`, a
  `*.localhost` name or an IP address; any other host name gets a 400 (`J214`), so a page you
  visit can't reach your dev server through a domain it controls (DNS rebinding) and read
  the debug pages or call your server functions. To reach the dev server by a name (a
  tunnel, `mymac.local`), list it: `Jongo(__name__, allowed_hosts=["mymac.local"])`;
  `".example.com"` or `"*.example.com"` also allows subdomains. In production the check
  applies only when `allowed_hosts` is set; set it unless a proxy in front already checks
  `Host`.
- **Framing.** Pages are sent with `X-Frame-Options: SAMEORIGIN` and, when the response has
  no `Content-Security-Policy` of its own, `frame-ancestors 'self'`, so another site can't
  load them in an invisible frame (clickjacking). `Jongo(frame_options="DENY")` forbids
  framing entirely; `frame_options=None` sends neither header. The admin always sends `DENY`.
- **Redirects.** `is_local_url(url)` (`jongo.is_local_url`) is true only for a path on this
  site. Check any redirect target that came from a request with it, such as a sign-in
  form's `?next=` ([§13](#13-auth-sessions-and-the-admin)).
- **Sessions** are signed and revocable: each sign-in is a row on the server, and a cookie
  whose row is gone is refused. **Passwords** are PBKDF2-SHA256.
- **Password guessing is throttled.** `authenticate()` stops checking after 10 wrong
  passwords for one username in 5 minutes and answers 429. `rate_limit=` puts a cap on any
  server function, page or route, counted per address and, for a signed-in caller, per
  user as well ([§24](#24-rate-limits-and-revocable-sessions)).
- **Channel guards fail closed.** A guard that returns nothing refuses the subscription.
- **Form input is whitelisted.** A `forms.Schema` returns only the fields it declares.
- **Limits.** Request bodies are capped at `Request.max_body_size` (16 MB), and forms at
  `Request.max_form_fields` and `Request.max_form_parts` (1000 each); over a limit the answer
  is a 413 (`J212`) before the body is parsed. A bogus `Content-Length` is rejected before
  the body is read. Raise them on the class for an app that needs more:
  `Request.max_form_fields = 5000`.
- **`to_dict()`** never includes password hashes.

What you still have to do: set `JONGO_SECRET_KEY` in production, authorise your own
channels and server functions (`request.user` is there, Jongo will not guess your rules),
validate uploads, check redirect targets that come from the user with `is_local_url()`,
set `allowed_hosts` in production when nothing in front of the app checks `Host`, and keep
secrets out of components.

---

## 15. Testing

`TestClient(app)` runs requests against the database the app is configured with, so tests
would read and write your development data. Give each test a fresh in-memory database
instead, with an autouse fixture in `tests/conftest.py` (`jongo new` writes this file):

```python
import pytest
from jongo import db

@pytest.fixture(autouse=True)
def fresh_database():
    db.configure(":memory:")   # a new, empty database for this test
    db.migrate()               # create the table for every model
    yield
    db.close_connections()
```

```python
from jongo import ServerError
from jongo.testing import TestClient

client = TestClient(app)

response = client.get("/")            # a real WSGI round trip
response.status, response.text        # 200 (an int), the body; response.json() for JSON
client.post("/login", data={...})     # CSRF handled like a browser
client.rpc(add_todo, "Write tests")   # call a @server function over HTTP; returns its result
client.navigate("/")                  # the JSON tree the in-browser router fetches
client.login(user)                    # sign in (records the session, as auth.login does)
response, chunks = client.stream("/_jongo/live?channels=prices")   # SSE, unconsumed

with pytest.raises(ServerError) as refused:   # a refused call raises, as it does in the browser
    client.rpc(add_todo, 42)
assert refused.value.status == 400 and refused.value.code == "J403"
```

See [12_testing.py](../examples/patterns/12_testing.py) for the whole set in one file.

---

## 16. The CLI

| Command | |
|---|---|
| `jongo new NAME` | create a project: `app.py`, `AGENTS.md`, and `tests/` with a fresh-database fixture |
| `jongo dev [app.py] [--port]` | dev server: auto-reload, live browser reload, error overlay |
| `jongo run [--host --port --migrate]` | production server (threaded) |
| `jongo migrate [--plan] [--allow-destructive]` | sync the schema |
| `jongo createadmin` | create an admin user |
| `jongo routes [--json]` | list routes |
| `jongo shell` | a Python shell with your models loaded |
| `jongo build` | compile components and write the bundle |
| `jongo check [--json]` | compile, resolve server-function hints, plan migrations; problems as data, exit 1 (use this in CI) |
| `jongo context [--json] [-o FILE]` | describe the app for an AI coding tool — see [§20](#20-working-with-ai-coding-agents) |
| `jongo doctor [--json] [--production] [--port N]` | one health report on the app, the database and the settings; exit 1 on a failure — see [§21](#21-diagnostics-doctor-explain-status) |
| `jongo explain CODE` / `--list [--json]` | what an error code means and how to fix it — see [§22](#22-error-codes) |
| `jongo status [--url URL] [--json]` | what a running server is doing: recent requests, errors, routes, channels, N+1 queries |
| `jongo openapi [-o FILE] [--api-version V] [--server URL]` | the OpenAPI 3.1 document, generated from the type hints — see [§25](#25-openapi-and-json-schema) |
| `jongo mcp [--list] [--read-only] [--user NAME]` | serve the app to a coding agent over MCP on stdin/stdout — see [§26](#26-mcp-the-app-as-tools-for-a-coding-agent) |
| `jongo sessions [--user NAME] [--revoke ID] [--revoke-user NAME] [--json]` | list the active sign-ins, or revoke them — see [§24](#24-rate-limits-and-revocable-sessions) |

A command given `--json` answers in JSON even when it fails: `{"ok": false, "error": {"code",
"message", "hint", "file", "line"}}` on stdout, exit status 1 (`code` is `null` for a plain
usage mistake). `--port` must be a number from 0 to 65535. A failure is a message with its code,
never a Python traceback, except that an app that fails to import prints its own traceback
first (`JONGO_TRACEBACK=0` turns that off).

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
- Set `Jongo(allowed_hosts=["myapp.example.com"])` unless a proxy in front already refuses
  other `Host` names; requests for any other host get a 400 (`J214`). Production doesn't
  check `Host` until you set it.
- Serve behind TLS. Cookies are marked `Secure` when the request is HTTPS.
- Console-script shebangs are sometimes broken by build packs: `python -m gunicorn` is the
  reliable spelling.
- Run `jongo doctor --production` before the first deploy (and in CI): it fails on a weak
  `JONGO_SECRET_KEY`, on no usable key at all, and on dev mode; it warns when the key only lives in
  `.jongo/secret` (a fresh container gets a new one), and about pending migrations and cookie settings.
- To read `/_jongo/status` from a deployed server, set `JONGO_STATUS_TOKEN` (16+ characters)
  and run `jongo status --url https://… --token …`; otherwise only a signed-in admin can.

- Rate limits and the login throttle count in the memory of each process. With one worker
  they are exact; with several, each worker counts separately.
- If the app calls `app.mcp()`, set `JONGO_MCP_TOKEN` (16+ characters) or the endpoint
  refuses everyone; `jongo doctor --production` warns about it (`J514`).
- Upgrading from 0.5: users sign in once more (their old cookies carry no server-side
  session), and the `jongo_sessions` table is created on the first sign-in if
  `jongo migrate` hasn't already made it.

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
- Integers past 2⁵³ (9007199254740992) lose precision: there is one number type. Do exact
  or big-integer arithmetic in a `@server` function.
- A dict whose keys look like non-negative integers (`{2: "b", 1: "a"}`, or `{"10": …,
  "9": …}`) iterates those keys in ascending numeric order, ahead of the other keys, instead
  of insertion order. When order matters, keep a list (of pairs, or of keys) beside the dict.
- A generator expression builds its whole list before the consumer sees it, so
  `any(check(x) for x in xs)` and `all(...)` call `check` for every item instead of stopping
  at the first answer. The result is the same; write a loop with `break` when the calls are
  expensive or have side effects.

Tuples are real tuples — `(1, 2) != [1, 2]`, immutable, and `enumerate`/`zip`/`.items()`
yield them — and strings are measured and indexed in code points, so `len("😀")` is 1.
Errors are Python's too: `/`, `//`, `%` and `**` by zero raise `ZeroDivisionError`, ordering
`None` or mixed types (`None < 1`, `"a" < 1`) raises `TypeError`, and `float("")` raises
`ValueError`. All of this is checked by a parity suite that runs every sample in CPython and
in Node and compares the results.

**One limit that isn't about the browser:** the first argument to `Jongo()` should be
`__name__`, or pass `root=`. The app finds its database file, `.jongo/secret` and `static/`
next to the module it names; given a name that isn't an imported module, it uses the file
that created it, and logs a warning if it has to fall back to the current directory.

---

## 19. When something goes wrong

Every error Jongo reports has a stable code — `J101`, `J302`, `J403` — and a hint. The code is
on the dev error page, in server-call error responses (`err.code` in the browser), in
`jongo check`, `jongo doctor` and every CLI error. `jongo explain J302` prints the full entry;
[§22](#22-error-codes) lists them all. Three commands cover most situations:

- the code is wrong → `jongo check` (compile, resolve, plan);
- the app won't start, or it's not the code → `jongo doctor` (the setup, the database, the settings);
- it fails at runtime → `jongo status` (the recent errors, with traceback and request id).

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
code is 1 when there is a problem, so the same command gates CI. Every `--json` command
answers in JSON even when it fails, as `{"ok": false, "error": {...}}`:

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

**The app is a set of tools.** `jongo mcp` serves all of the above to an agent as MCP tools,
together with one tool per `@server` function, so the agent can call the app it is working on
instead of guessing at it ([§26](#26-mcp-the-app-as-tools-for-a-coding-agent)). The same type
hints produce an OpenAPI document for everything else that speaks HTTP
([§25](#25-openapi-and-json-schema)).

---

## 21. Diagnostics: doctor, explain, status

**`jongo doctor`** runs every check that can be made without a browser and prints one line each:

```text
  ✓ python           Python 3.12.13
  ✓ app              loaded 'app' from /srv/todo
  ! secret_key       secret key read from .jongo/secret; a fresh container gets a different one [J501]
      hint: Set JONGO_SECRET_KEY to a long random value: python -c "import secrets; print(secrets.token_urlsafe(48))"
  ✓ components       4 components compile
  ✓ database         sqlite /srv/todo/todos.sqlite3 is reachable
  ! migrations       1 pending migration(s): Add column "todo"."due" [J302]
```

It checks Python and Jongo, that the app imports, that every component compiles and every
server function's hints resolve, that the database is reachable (without creating it) and
whether migrations are pending, the static directory, an admin user when the admin is mounted,
optionally a port (`--port`), and — when judged as production — the secret key, dev mode,
cookie security and the status endpoint. Production is judged when you pass `--production`,
set `JONGO_ENV=production` (which wins even over dev mode, so a stray `JONGO_DEV=1` on a server
fails as `J502`), or a hosting platform's variables are present (Railway, Heroku,
Fly, Cloud Run, Render, Vercel) and dev mode is off. `--json` gives each check as
`{"id", "status": "ok"|"warn"|"fail", "message", "hint", "code"}`; the exit code is 1 when
any check fails, so it gates CI. From Python: `jongo.doctor(app)`.

**`jongo explain CODE`** prints what a code means, why it happens, the fix and an example;
`jongo explain --list` lists every code, `--json` for tools. From Python: `jongo.explain("J302")`.

**`jongo status`** shows what a running server has been doing, read from `/_jongo/status`:

```text
  app · jongo 0.5.0 · dev · pid 66509 · up 42s · sqlite
  requests: 12  (2xx 10  4xx 1  5xx 1)   channels: 0 subscriber(s)

  routes
         7×  /                                avg 2.2ms  max 4.0ms
         3×  @server app.add_todo             avg 1.1ms  max 1.4ms  1 errors

  recent errors
    [J305] IntegrityError: UNIQUE constraint failed: todo.title  (POST /_jongo/rpc/app.add_todo, request 4d42) at app.py:31
        hint: Check for an existing row before creating (.exists()), or catch the error and report it to the user.

  likely N+1 queries
    GET /: ran 20× SELECT "id", "title" FROM "tag" WHERE ("todo_id" = ?)
```

The app keeps the last 200 requests (method, path, status, duration, request id, route, query
count while SQL timing is on), the last 50 unhandled errors (code, message, a short traceback, the app file and line,
the request id that ties it to the log line), a counter per route, the channels with
subscribers, and — while SQL timing is on (dev, or `JONGO_LOG_LEVEL=DEBUG`) — requests that ran
the same statement five or more times. It is in-process, standard library only, and costs one
short locked append per request. From Python: `jongo.status(app)` returns the same dict.

Its `mode` is how the app is running: `dev` under `jongo dev`, otherwise `production`, which
includes `jongo run` on your own machine and the app inside `jongo mcp`. That is not the
doctor's verdict: `jongo doctor` reports `"production": true` only when it judges the settings
as a deploy (`--production`, `JONGO_ENV=production`, or a hosting platform's variables), so the
two can disagree on a laptop without contradicting each other.

It never records query strings, headers, cookies, bodies, form values or SQL parameters, never
shows the database URL, and strips `user:password@` from error messages.

**Access.** In dev mode `/_jongo/status` is open (the dev server listens on 127.0.0.1), unless
`JONGO_ENV=production` is set: that keeps it locked even if dev mode was switched on by mistake
(the server logs a `J502` warning at start, and `jongo doctor` fails). In
production it answers only a signed-in admin, or a request with
`Authorization: Bearer $JONGO_STATUS_TOKEN` when that variable is set to 16 or more characters;
everyone else gets a 403 (`J508`). `JONGO_STATUS=off` removes the endpoint entirely.

```bash
jongo status                                     # http://127.0.0.1:8000
jongo status --url http://127.0.0.1:8770 --json
JONGO_STATUS_TOKEN=... jongo status --url https://myapp.example.com
```

Server-call errors carry the same data to the browser: `err.code`, `err.hint` and, in dev,
the file, line and traceback. In production a crashing server function answers
`{"type", "message": "Server error", "code": "J407", "request_id"}` — nothing more — and the
request id finds the full error in `jongo status`. The dev error page shows the code, the hint
and the request id; the production error page shows only the request id, as a reference.

---

## 22. Error codes

Codes are stable: the wording of a message may improve, the code doesn't change, and a retired
code is never reused. `jongo explain CODE` has the explanation and an example for each.

**General**

| Code | Problem | Fix |
|---|---|---|
| `J000` | Unhandled exception in the app's code | Read the innermost frame in your own file (it is highlighted) and fix the code there. |

**Components and the compiler**

| Code | Problem | Fix |
|---|---|---|
| `J100` | Code can't be compiled for the browser | Move the unsupported code into the page function or a @server function and pass the result in as props. |
| `J101` | Statement not supported in browser code | Import at the top of the module (the compiler resolves imported names); use dicts instead of classes; keep changing values in state(). |
| `J102` | Server-only object used in browser code | Load rows in the page (or a @server function) and pass them as props; call server-only code through a @server function. |
| `J103` | Undefined name in browser code | Define or import the name at module level, or check the spelling. |
| `J104` | Expression or syntax not supported in browser code | Rewrite it with a supported form (plain assignment, a loop, a helper function), or move it to the server. |
| `J105` | Component source can't be read | Define the component with `def` in a module file. |
| `J106` | Component can't be async | Load the data in the page function or a @server function; async event handlers inside the component are fine. |
| `J107` | Hook called outside a component render | Call hooks at the top level of a @component function. |
| `J108` | Browser-only API called on the server | Call it from an event handler or inside effect(). |
| `J110` | Component returned a list | Wrap the elements in div(...) or fragment(...). |
| `J111` | Value can't be sent to the browser | Convert it to dict/list/str/int/float/bool (Model rows: .to_dict()). |
| `J112` | Invalid UI element | Return elements (div(...), components) from pages and components; put event handlers inside a @component. |
| `J113` | Unknown component | Reload the page; define components at module level so they register at import. |
| `J114` | Event handler passed under the wrong name | Rename the keyword to on_<event>: onClick= becomes on_click=, onInput= becomes on_input=. |
| `J115` | Form schema can't be built | Fix the rule named in the message; write patterns with plain groups and character classes ([A-Za-z] instead of (?i)). |

**Routing and HTTP**

| Code | Problem | Fix |
|---|---|---|
| `J200` | HTTP error raised by the app | Nothing to fix in Jongo: this is the response the app chose. Check the condition that raised it. |
| `J201` | Unknown route converter | Use one of str, int, float, slug, path, uuid — or annotate the parameter (id: int) and write <id>. |
| `J202` | Invalid route path | Write the path as '/things', not 'things'. |
| `J203` | No route with that name | Check `jongo routes` for the names, or pass name= when registering the route. |
| `J204` | Missing URL parameter | Pass every parameter in the route path as a keyword: url_for('post', id=3). |
| `J205` | Not found | Check the URL against `jongo routes`; a typed segment (<int:id>) only matches values of that type. |
| `J206` | Method not allowed | Register the method (methods=('GET', 'POST')) or send the request with an allowed method (see the Allow header). |
| `J207` | CSRF check failed | Add input(type='hidden', name='csrf_token', value=request.csrf_token) to the form, or pass csrf=False for a webhook that authenticates another way. |
| `J208` | Login required | Sign in first (client.login(user) in tests), or remove login_required if the endpoint is public. |
| `J209` | Admins only | Sign in as a user with is_admin=True (`jongo createadmin` makes one). |
| `J210` | Forbidden | Check the permission condition that raised it; this is usually the app working as intended. |
| `J211` | Query parameter has the wrong type | Fix the link or form that builds the URL, or give the parameter a type that accepts the value (str, Optional[int]) and handle it in the function. |
| `J212` | Malformed request body | Send a valid JSON body with Content-Type: application/json and a correct Content-Length; for larger uploads raise jongo.Request.max_body_size (16 MB by default), for bigger forms Request.max_form_fields / max_form_parts. |
| `J213` | Too many requests (rate limit) | Wait for the Retry-After seconds. If the limit is too tight, raise it where it is set (rate_limit="30/minute", or jongo.auth.LOGIN_ATTEMPTS / LOGIN_WINDOW for sign-in). |
| `J214` | Host not allowed | Open the app as localhost or 127.0.0.1, or list the host: Jongo(__name__, allowed_hosts=["myapp.test"]) (a leading dot, ".example.com", also allows subdomains). |

**Database, models and migrations**

| Code | Problem | Fix |
|---|---|---|
| `J300` | Database error | Read the driver's message; run `jongo doctor` to check the connection and schema. |
| `J301` | Database unreachable | Check Jongo(database=...) / JONGO_DATABASE: the directory must exist and be writable, or the server must be running. |
| `J302` | Schema out of date (pending migrations) | Run `jongo migrate` (`jongo migrate --plan` shows the changes first); `jongo dev` and `jongo run --migrate` apply them on start. |
| `J303` | Migration failed or blocked | Read the message: make the new field null=True or give it a default, rename the table, or run `jongo migrate --plan` to see what would change. |
| `J304` | Validation failed | Show err.errors to the user, or fix the data being saved. |
| `J305` | Integrity constraint violated | Check for an existing row before creating (.exists()), or catch the error and report it to the user. |
| `J306` | PostgreSQL driver missing | pip install "jongo[postgres]" |
| `J307` | Row does not exist | Use .filter(...).first() when the row may be missing, or catch Model.DoesNotExist (raise NotFound for a 404). |
| `J308` | More than one row matched | Filter on a unique field (id), or use .filter(...).first(). |
| `J309` | Invalid database URL | Use a SQLite path ('app.sqlite3', 'sqlite:///path', ':memory:') or a postgres:// URL. |
| `J310` | Unknown model or field | Check the spelling against `jongo context`; define (import) the referenced model first. |

**Server functions and channels**

| Code | Problem | Fix |
|---|---|---|
| `J401` | No such server function | Decorate the function with @server at module level, and reload the page after renaming it. |
| `J402` | Malformed server function call | Call server functions from browser code with await fn(...), or from tests with client.rpc(fn, ...). |
| `J403` | Server function argument has the wrong type | Send the annotated type (convert in the browser: int(draft.value)), or change the annotation. |
| `J404` | Server function arguments don't match its signature | Call it with the parameters in its signature (`jongo context` lists every signature). |
| `J405` | Server function type hint can't be resolved | Define the annotated type (e.g. the model) at module level where the function can see it, annotate with one of the checked types, or use Any to take any JSON and check it yourself. |
| `J406` | Two server functions share an id | Move them to module level, or give each a distinct name. |
| `J407` | Server function raised an exception | Read the traceback in the log or `jongo status`; raise NotFound/Forbidden/ValidationError for errors the browser may read. |
| `J408` | Server function returned an unsupported response | Return JSON-able data, or redirect(url) to a path on this site (check user-supplied targets with jongo.is_local_url) or an http(s) URL. |
| `J410` | Channel subscription refused | Declare the channel with @app.channel('pattern') and check its guard. |
| `J411` | Model argument names a row that doesn't exist | Check the id the browser sends (the row may have been deleted); handle the 404 in the browser. |
| `J412` | Channel guard gave no answer | Return True to allow the subscription and False to refuse it, on every path through the guard. |
| `J420` | MCP endpoint refused the request | Set JONGO_MCP_TOKEN on the server and give the same token to the MCP client as a bearer token; locally, use `jongo mcp` (stdio) or connect to 127.0.0.1. |
| `J421` | Unknown MCP tool | Call tools/list to see the tools; expose a function with app.mcp(tools=[fn]) or tools="all". |
| `J422` | Malformed MCP message | Send one JSON-RPC object per request (or per line over stdio) with "jsonrpc": "2.0", a method and object params. |

**Configuration, CLI and deployment**

| Code | Problem | Fix |
|---|---|---|
| `J501` | Secret key not set (or too weak) for production | Set JONGO_SECRET_KEY to a long random value: python -c "import secrets; print(secrets.token_urlsafe(48))" |
| `J502` | Dev mode is on | Don't set JONGO_DEV=1 or dev=True in production; run with `jongo run`. |
| `J503` | App file not found | Run the command inside the project, pass the file (jongo check path/to/app.py), or create one with `jongo new`. |
| `J504` | No Jongo app in the module | Add app = Jongo(__name__) to the module, or name it: jongo check module:attr. |
| `J505` | Port in use, or the address can't be listened on | Use another port (--port 8001) or stop the other process; check --host (127.0.0.1 for this machine only, 0.0.0.0 for every interface). |
| `J506` | Static directory missing | Create static/ next to app.py and put the files there. |
| `J507` | Cookies won't be marked Secure behind a proxy | Behind a proxy you control, create the app with Jongo(..., trust_proxy=True). |
| `J508` | Status endpoint refused | Set JONGO_STATUS_TOKEN on the server and run `jongo status --url ... --token $JONGO_STATUS_TOKEN`. |
| `J509` | App failed to import | Fix the error at the file and line shown (it's in your code or something it imports). |
| `J510` | Admin mounted but no admin user | Run `jongo createadmin`. |
| `J511` | Unsupported Python version | Install a newer Python and recreate the virtualenv. |
| `J512` | Can't reach the running server | Start the app (`jongo dev`) and pass its address: jongo status --url http://127.0.0.1:8000 |
| `J513` | Rate limit can't be read | Write it as "5/minute", "100/hour", "10 per 30 seconds", or a (count, seconds) pair. |
| `J514` | MCP endpoint mounted but nobody can use it | Set JONGO_MCP_TOKEN to a long random value (python -c "import secrets; print(secrets.token_urlsafe(32))"), or remove app.mcp() from the production app. |
| `J515` | Can't write the file or directory | Pass a path inside a directory that exists and that you can write to (create it first with mkdir -p), with a shorter name if the system says it is too long. |

---

## 23. Form schemas: one set of rules for the browser and the server

Validation usually exists twice: once in JavaScript so the user gets an answer straight
away, once on the server because the browser can't be trusted. The two copies drift. In
Jongo there is one copy, because the browser runs compiled Python.

```python
from jongo import forms

Signup = forms.Schema(
    "Signup",
    email=forms.Email(max_length=120),
    team=forms.Text(min_length=2, max_length=30, pattern=r"[a-z][a-z0-9-]*"),
    plan=forms.Choice([("free", "Free"), ("team", "Team")], default="free"),
    seats=forms.Int(min=1, max=50, default=1),
    password=forms.Text(min_length=8, secret=True),
    confirm=forms.Text(same_as="password", secret=True, label="Password again"),
    terms=forms.Bool(required=True, label="I accept the terms"),
)


@server
def sign_up(request, data: Signup) -> dict:      # validated before this line runs
    if Account.filter(email__iexact=data["email"]).exists():
        raise db.ValidationError({"email": "That address already has an account."})
    return Account.create(email=data["email"], team=data["team"], seats=data["seats"]).to_dict()


@component
def SignupForm():
    errors = state({})

    async def submit(event):
        values = form_values(event)
        found = forms.validate(Signup, values)   # in the browser: no request made
        errors.set(found)
        if found:
            return
        try:
            await sign_up(values)                # on the server: the same rules again
        except ServerError as exc:
            errors.set(exc.errors or {})         # plus what only the server knows

    return form([forms.Field(schema=Signup, name=name, error=errors.value.get(name), key=name)
                 for name in Signup],
                button("Create account"), on_submit=submit)
```

**Rules.** Each takes `required` (default `True`), `label`, `default`, `help` and `messages`.

| Rule | Checks | Cleaned value |
|---|---|---|
| `forms.Text(min_length, max_length, pattern, same_as, strip, multiline, secret)` | length in characters, a whole-value regular expression, equality with another field | `str`, stripped |
| `forms.Email(max_length=254)` | looks like `name@host.tld` | `str` |
| `forms.Int(min, max)` | a whole number, as a number or as text (`"42"`) | `int` |
| `forms.Float(min, max)` | a number (`"3.5"`, `"1e3"`) | `float` |
| `forms.Bool()` | yes/no; `required=True` means it must be ticked | `bool` |
| `forms.Choice(choices)` | one of the values, or of `(value, label)` pairs | the declared value |

`forms.Schema.from_model(Invite, only=..., exclude=...)` builds a schema from a model's
fields (`max_length`, `choices`, `blank`, the type), so the form can't drift from the table.
`only=[...]` keeps just the fields named and `exclude=[...]` drops some. Fields that should
never come from a form (`is_admin`, `is_staff`, `is_superuser`, `password`) are left out
unless `only=` names them, with a warning outside production; name one in `only=` if a form
really should set it.

**Using a schema.**

| Where | Call | Result |
|---|---|---|
| a component | `forms.validate(Signup, values)` | `{field: message}`; empty when valid |
| a component, one field | `forms.check(Signup["email"], value, values)` | the message, or `""` |
| a `@server` parameter | `data: Signup` | the cleaned dict; a failure is a 400 with `errors` (`J304`) |
| anywhere on the server | `Signup.clean(values)` or `Signup(values)` | the cleaned dict, or raises `db.ValidationError` |
| rendering | `forms.Field(schema=Signup, name="email", value=..., error=..., on_input=...)` | a labelled control with its message |
| rendering your own input | `input_(**forms.attrs(Signup, "email"))` | `name`, `type`, `required`, `maxlength`, `min`, `max`… |

`clean()` returns **only the fields the schema declares**, typed and stripped, with defaults
applied. A browser that adds `"is_admin": true` to the payload gets it dropped, so
`Model.create(**data)` is safe to write. A field with a `default` is never "required":
left empty, it becomes the default.

`forms.Field` renders `<label class="jongo-field">` with the control, `help` text and, when
`error` is set, `<span class="jongo-field-error" role="alert">`. It is unstyled; style the
classes with `global_css`. Pass `novalidate=True` on the `<form>` if you want your own
messages instead of the browser's native bubbles.

**How the two sides are kept in step.**

- A schema is a plain dict of rules, so the compiler inlines it into the bundle as JSON.
  `forms.validate` and `forms.check` are ordinary Python functions that the compiler turns
  into JavaScript: the browser runs the code the server runs.
- Every message is rendered in Python when the schema is built. The browser formats nothing,
  so `5.0` never prints differently on the two sides.
- Patterns are rewritten to syntax Python's `re` and JavaScript's `RegExp` read identically:
  `\d` becomes `[0-9]`, `\w` becomes `[A-Za-z0-9_]`, `.` stops at the same line endings. A
  pattern that can't be made portable (named groups, `(?i)`, `\A`, possessive quantifiers) is
  an error when the schema is defined (`J115`), not a surprise in production.
- Whole numbers beyond ±9007199254740991 are refused on both sides, rather than silently
  rounded by JavaScript.
- `tests/test_forms.py` runs every rule against a table of awkward values (non-ASCII
  digits, astral characters, `"1e400"`, bools that are ints) in CPython and in the compiled
  bundle under Node, and requires identical answers.

Limits: a schema validates flat fields (no nested objects or lists), `DateTime`/`Date`/`JSON`
model fields are skipped by `from_model`, and HTML's `maxlength` counts UTF-16 units while
the schema counts characters, so for emoji the browser's native limit is the stricter one.

[14_shared_validation.py](../examples/patterns/14_shared_validation.py) is this section as a
running app, with as-you-leave-the-field feedback.

---

## 24. Rate limits and revocable sessions

### Rate limits

```python
@server(rate_limit="5/minute")
def send_invite(request, email: str) -> dict: ...

@app.route("/webhook", methods=("POST",), csrf=False, rate_limit="30/minute")
def webhook(request): ...

@app.page("/report", rate_limit="10 per 30 seconds")
def report(): ...
```

Each request counts against the client address and, when someone is signed in, against that
user as well; it is refused when either count is over the limit (and then neither is charged).
So signing up a fresh account for every request, or switching accounts, doesn't reset the
address's count. Many signed-in users behind one address (an office, a proxy without
`trust_proxy=True`) share its count: raise `ratelimit.ADDRESS_FACTOR` (default `1`) to give the
address a multiple of the limit, or set it to `None` to count signed-in users per user only. Over the limit
the function doesn't run: the response is `429` with a `Retry-After` header and the code
`J213`. Rates are written `"5/minute"`, `"100/hour"`, `"3/s"`, `"10 per 30 seconds"`
or as a `(count, seconds)` pair; a rate that can't be read fails when the function is
registered (`J513`), not on the first request. The window slides, so a burst can't straddle a
boundary and get through twice.

For a limit keyed by something else, call it yourself:

```python
from jongo import ratelimit

@server
def reset_password(request, email: str) -> dict:
    ratelimit.check(request, "3/hour", "reset-password", key=email.lower())
    ...
```

Behind a proxy, addresses are only meaningful with `Jongo(trust_proxy=True)`: then
`request.client_ip` is the entry the proxy appended to `X-Forwarded-For`. Without it the
header is ignored, so a client can't choose its own rate-limit key.

**Counters live in the memory of the process.** `jongo run` is one process, so its limits
are exact. Behind several worker processes each worker counts on its own (the effective
limit is workers × limit), and a restart clears them. A limit that must hold across
processes needs a shared store, which Jongo doesn't provide.

### Login throttling

`authenticate(username, password, request)` counts wrong passwords with no configuration:

```python
try:
    user = authenticate(username, password, request)
except TooManyRequests as exc:        # from jongo.ratelimit; a 429, code J213
    return {"error": exc.message, "retry_after": exc.retry_after}
```

After 10 wrong passwords for one username in 5 minutes it stops checking passwords for that
username and raises instead, so a guess gains nothing and costs the server no hashing. A
correct password clears the count. When you pass the request, wrong passwords are also
counted per client address (50 in 5 minutes), which stops one machine trying a few passwords
on many accounts. If the exception isn't caught it becomes the 429 response. Unknown
usernames are throttled the same way, so the limit doesn't reveal which accounts exist.
Tune with `auth.LOGIN_ATTEMPTS`, `auth.LOGIN_ATTEMPTS_PER_ADDRESS` and `auth.LOGIN_WINDOW`;
`None` turns one off. The trade-off is the usual one: someone who knows a username can lock
its login for five minutes.

### Sessions you can revoke

A signed cookie can't be taken back: before 0.6.0, signing out cleared the cookie in that
browser, and a copy of it kept working until it expired. Now every `login()` is a row in
`jongo_sessions`, and a cookie is honoured only while its row exists.

```python
from jongo import auth

auth.sessions(user)                          # [LoginSession, ...] newest first
auth.revoke_session(session_id, user=user)   # sign one browser out -> True/False
auth.revoke_sessions(user)                   # sign out everywhere -> how many
auth.revoke_sessions(user, keep=request)     # everywhere but this browser
request.login_session                        # this request's own record
```

A session has `id`, `created`, `last_seen`, `ip` and `user_agent`; `to_dict()` gives them
JSON-ready, enough for a "where you're signed in" list. Pass `user=request.user` to
`revoke_session` whenever the id came from the browser, so nobody can end someone else's
session by counting upwards. From the command line:

```bash
jongo sessions [--user ann] [--json]
jongo sessions --revoke 12
jongo sessions --revoke-user ann
```

What to know:

- `logout()` deletes the row, so a copied cookie dies with it. Changing a password still
  signs out that user's other sessions, as before.
- The table stores a SHA-256 of the session id, never the id, and `to_dict()` leaves it out.
- A row expires with `session_max_age` (14 days by default), counted from the sign-in.
- It costs one extra query per request from a signed-in user. `last_seen` is written at
  most once every five minutes.
- **Upgrading:** cookies issued by 0.5 carry no session id, so those users are signed out
  once and sign in again. The table is created on the first sign-in if `jongo migrate`
  hasn't run. Code that built a session cookie by hand from `{"_uid": ..., "_pw": ...}`
  should call `auth.session_data_for(user)`; `TestClient.login(user)` already does.

[16_sessions_and_limits.py](../examples/patterns/16_sessions_and_limits.py) is a working
"where you're signed in" page.

---

## 25. OpenAPI and JSON Schema

There is no API description to write: the type hints on `@server` functions and the fields on
models already are one. `jongo openapi` writes them out as an OpenAPI 3.1 document.

```bash
jongo openapi                                   # to stdout
jongo openapi -o openapi.json --api-version 1.2.0 --server https://example.com
curl http://127.0.0.1:8000/_jongo/openapi.json  # from the running app
```

The document has:

- every `@server` function as `POST /_jongo/rpc/<id>`, with the body browser code sends
  (`{"args": [...], "kwargs": {...}}`), the result under `ok`, and the error envelope for
  each status it can answer (400 for bad arguments, 401/403 for the guards, 404 for a model
  argument, 429 for a rate limit);
- every page and route with its path parameters (from `<int:id>`) and query parameters
  (from the handler's signature), and the response schema when the handler declares a
  return type;
- a schema under `components` for each model a function returns, in the shape `to_dict()`
  produces (so `User` has no `password`), and one `Error` schema;
- `x-jongo` on each operation: `login_required`, `admin_required`, `rate_limit`.

The framework's own routes and the admin are left out. `/_jongo/openapi.json` is open under
`jongo dev`; in production it follows the rule of `/_jongo/status` (an admin session or
`JONGO_STATUS_TOKEN`).

From Python:

```python
from jongo import json_schema, openapi
from jongo.schema import form_schema, function_schema, model_schema

openapi(app, version="1.2.0")        # the document as a dict
json_schema(list[int] | None)        # {"anyOf": [{"type": "array", "items": {"type": "integer"}}, {"type": "null"}]}
json_schema(Todo)                    # an argument: {"type": "integer", "description": "id of a Todo"}
json_schema(Todo, output=True)       # a return value: the to_dict() object
function_schema(add_todo)            # a server function's arguments as one object
form_schema(Signup)                  # a forms.Schema
```

How hints map: `int`, `float`, `str`, `bool`, `None`; `list[T]`, `set[T]`, `tuple[A, B]`,
`dict[str, T]`; `Optional[T]` and unions as `anyOf`; `Literal[...]` as `enum`; dataclasses as
objects; `datetime`, `date`, `UUID`, `Decimal`; a model class as its id going in and its
`to_dict()` object coming out; a `forms.Schema` as an object with its rules as keywords
(`minLength`, `maximum`, `pattern`, `enum`, `format: email`). An unannotated parameter is
`{}`: anything. `jongo.describe(app)` carries the same `input_schema` for each server function.

A return type is documentation, not enforcement: Jongo validates arguments, and reports what
you declared for results. Annotate what a function really returns.

---

## 26. MCP: the app as tools for a coding agent

The [Model Context Protocol](https://modelcontextprotocol.io) is how agents such as Claude
Code discover tools and call them. A Jongo app already has what a tool list needs: named
functions with docstrings and type hints, and reports that answer in JSON.

**In the project, over stdio.** Nothing to add to the app:

```bash
jongo mcp                              # speaks MCP on stdin/stdout
claude mcp add myapp -- jongo mcp      # register it with Claude Code
jongo mcp --list                       # print the tools as JSON and exit
jongo mcp --read-only                  # only the jongo_* tools
jongo mcp --user ann                   # run calls as this user
```

**From the running server, over HTTP.** Opt in, in `app.py`:

```python
app.mcp()                                              # POST /_jongo/mcp: the jongo_* tools
app.mcp(tools=[add_task, list_tasks, complete_task])   # ...plus these functions
app.mcp(tools="all", act_as=lambda request: User.get(username="agent"))
```

```bash
claude mcp add --transport http myapp http://127.0.0.1:8000/_jongo/mcp
```

**The tools.**

| Tool | Returns |
|---|---|
| `jongo_context` | the framework's rules and this app's map, as Markdown (`jongo context`) |
| `jongo_describe` | the map as data; `section` picks one part (`jongo context --json`) |
| `jongo_check` | compile, resolve hints, check channel guards, plan migrations (`jongo check --json`) |
| `jongo_doctor` | the health report; `production=true` judges it as a deploy |
| `jongo_explain` | what an error code means and how to fix it; with no code, every code |
| `jongo_status` | recent requests, errors with tracebacks, per-route counters, N+1 queries; `mode` is `production` under `jongo mcp` because dev mode is off ([§21](#21-diagnostics-doctor-explain-status)) |
| `jongo_openapi` | the OpenAPI document |
| `jongo_get` | GET a path in-process: `{status, content_type, body}`, to confirm a page renders |
| *one per `@server` function* | the function's result; its input schema comes from the type hints |

The context pack and the OpenAPI document are also MCP resources (`jongo://context`,
`jongo://openapi`).

A tool call goes through the door a browser call goes through: arguments are checked against
the type hints (a model parameter takes the row's id, a `forms.Schema` parameter is
validated), `login_required` and `admin_required` are enforced, rate limits are counted. A
call that fails comes back as a tool error, not a traceback:

```json
{"isError": true,
 "content": [{"type": "text", "text": "[J403] argument 'title' must be str, got int\nhint: send title as str ..."}],
 "structuredContent": {"error": {"code": "J403", "type": "HTTPError", "status": 400,
                                 "message": "argument 'title' must be str, got int",
                                 "hint": "...", "explain": "jongo explain J403"}}}
```

so the agent can read the code, call `jongo_explain`, and try again.

**Which functions are exposed.** `jongo mcp` runs on your machine as you, and exposes every
`@server` function the app defines. `app.mcp()` exposes only the functions you pass;
`tools="all"` is there, and it is a decision: everything the browser can call, an agent
holding the token can call.

**Who a call runs as.** Anonymous by default, so `login_required` functions answer `J208`.
`act_as` (a user, or a function of the request) or `jongo mcp --user NAME` chooses a user.
Over HTTP a browser session cookie gives no identity: the endpoint is exempt from the CSRF
check, so cookies carry no authority there.

**Who may connect over HTTP.**

- There is no endpoint until `app.mcp()` is called.
- Under `jongo dev` it answers this machine only, addressed as `localhost` or `127.0.0.1`,
  even if the server is bound to `0.0.0.0`.
- In production it requires `Authorization: Bearer $JONGO_MCP_TOKEN`, 16 characters or more.
  With the variable unset it refuses everyone (`J420`), and `jongo doctor` says so (`J514`).
- A request with a browser `Origin` from another site is refused unless it carries the
  token, and so (in dev) is one whose `Host` isn't this machine. Together they stop a web
  page you visit from reaching a server on localhost, directly or by DNS rebinding.

In production, tool errors from your own code say `Server error` and the code, as a browser
would be told; the traceback goes to the log and `jongo status`.

**The transport.** Stdio is one JSON message per line; whatever the app prints goes to
stderr, so `print()` in app code can't corrupt the stream. HTTP is MCP's Streamable HTTP in
its simplest form: one JSON-RPC message per POST, one JSON response; the server pushes
nothing, so there are no sessions and no streams to hold open (a GET answers 405). The
server answers protocol revisions 2024-11-05 to 2025-11-25, and offers tools and resources
(no prompts, no sampling, no batches).

[15_agent_tools.py](../examples/patterns/15_agent_tools.py) mounts the endpoint and shows the
tool list an agent sees.
