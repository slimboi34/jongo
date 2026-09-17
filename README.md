# Jongo

**One language for the whole web app.** Jongo is a full-stack Python web framework in the spirit of Django: ORM, migrations, auth, sessions, CSRF protection and an admin site. The difference is that your frontend is Python too. Components render on the server for a fast first paint, then compile to JavaScript and come alive in the browser. They call your server code with a plain `await`.

There's no template language, no separate JS project and no build step. It has zero dependencies.

```python
from jongo import Jongo, component, db, server, state
from jongo.html import *

app = Jongo(__name__, database="db.sqlite3")


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
- **Arguments** are filled by name. `request` is the current request, path parameters come from the URL, and anything else is read from the query string and type-checked.
- **A page returns** UI, `Page(ui, title=..., status=..., head=[...])`, or a `Response`, such as `redirect("/login")`.
- **Plain handlers:** `@app.get`, `@app.post` and `@app.route(path, methods=[...])` return a `Response`, HTML `str`, JSON-able `dict`/`list`, or UI.
- **Layouts:** `@app.layout` wraps every page in a component. When a link switches pages, the layout's state survives.
- **Errors:** raise `NotFound("...")`, `Forbidden()` or `HTTPError(status, message)`, and customise the page with `@app.errorhandler(404)`.
- **Access control:** `login_required=True` and `admin_required=True` work on pages and routes.

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
- **Arguments are validated** against the type hints before your code runs. Supported hints: `str`, `int`, `float`, `bool`, `list[...]`, `dict[...]`, `Optional`, `Literal`, dataclasses and models.
- **Return values** can be JSON-able data or UI elements (rendered in the browser), or a `redirect(url)` that navigates.
- **Failures** raise `ServerError` in the browser, with `.status`, `.type` and field `.errors`.
- **Options:** `@server(login_required=True)`, `@server(admin_required=True)`, and `@server(refresh=True)` to re-run the page loader after each call.
- **Security:** every call is CSRF-protected. Only functions you decorate are exposed.

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
- **Migrations have no files.** `jongo migrate` compares your models to the live SQLite schema.
  - It creates tables, adds columns and indexes, and rebuilds a table when a column's type changes.
  - It only drops columns when you pass `--allow-destructive`.
  - `jongo migrate --plan` shows the SQL first.
  - `jongo dev` applies the safe changes automatically.

## Auth and admin

```python
from jongo.auth import User, authenticate, login, logout

app.admin()                                  # generated admin at /admin
```

- **Users:** `User.create_user(...)`, then `authenticate`, `login(request, user)` and `logout(request)`. `request.user` is available in pages, routes and server functions.
- **Passwords** use PBKDF2-SHA256. Changing a password signs out the user's other sessions.
- **The admin site** lists, searches, sorts, creates, edits and deletes rows for every model, with forms built from your field types. Create the first account with `jongo createadmin`.

## Sessions, CSRF, security

- **Sessions** are HMAC-signed cookies: `request.session["cart"] = [...]`. Set `JONGO_SECRET_KEY` in production. In dev, a key is generated into `.jongo/secret`.
- **CSRF:** unsafe requests need a token. Browser code sends it automatically. Classic HTML forms need `input_(type="hidden", name="csrf_token", value=request.csrf_token)`. Cross-origin `Origin` headers are rejected.
- **Escaping:** text is always HTML-escaped. `raw()` is the explicit escape hatch.
- **Data sent to the browser:** props are converted to JSON. `User.to_dict()` never includes password hashes.

## Testing

```python
def test_add(app):
    client = app.test_client()
    assert client.get("/").status == 200
    todo = client.rpc(add_todo, "Write tests")     # full HTTP round trip, CSRF included
    assert client.navigate("/")["title"] == "Todos"
```

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

## Deployment

`app` is a standard WSGI application:

```bash
JONGO_SECRET_KEY=... gunicorn app:app        # or: jongo run --port 8000 --migrate
```

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

Version 0.1, which is young. Known limits:
- SQLite only.
- No WebSockets yet.
- No classes in browser code.
- Components re-render their subtree without memoisation.

Bug reports and ideas are welcome.
