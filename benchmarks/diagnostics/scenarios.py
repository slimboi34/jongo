"""Realistic broken Jongo apps, one mistake each.

Each scenario is an app directory written from ``files``, optional ``setup`` code run
first (with the target Jongo), ``env`` for the probes, and ``requests`` the runtime probe
sends in dev mode. Scoring needs:

* ``needles`` — substrings of the failure's own message (any one identifies it). They are
  taken from wording both versions share, so neither is favoured.
* ``location`` — for problems with a place in the code, the line marked ``# <-- HERE``
  in ``app.py`` (``alt_lines`` lists other lines that are equally right, e.g. the
  ``def`` under a decorator).
* ``migrate`` — False when the runtime probe must not auto-migrate first (``jongo dev`` does).
* ``expect`` — the catalogue code 0.5.0 assigns (reported, not used for the 0.4.0 score).
"""
from textwrap import dedent

HEADER = "from jongo import Jongo, component, db, server, state\nfrom jongo.html import *\n"


def app(body: str, database: str = '":memory:"') -> str:
    return HEADER + f"app = Jongo(__name__, database={database})\n" + dedent(body)


SCENARIOS = [
    {
        "name": "import inside a component",
        "files": {"app.py": app('''
            @component
            def Clock():
                import time  # <-- HERE
                return p(str(time.time()))

            @app.page("/")
            def home():
                return Clock()
        ''')},
        "requests": [("GET", "/", None)],
        "needles": ["`Import` statements"],
        "location": True,
        "expect": "J101",
    },
    {
        "name": "database model used in a component",
        "files": {"app.py": app('''
            class Todo(db.Model):
                title = db.Text()

            @component
            def Todos():
                return ul(*[li(t.title) for t in Todo.all()])  # <-- HERE

            @app.page("/")
            def home():
                return Todos()
        ''')},
        "requests": [("GET", "/", None)],
        "needles": ["is a database model"],
        "location": True,
        "expect": "J102",
    },
    {
        "name": "undefined name in a component",
        "files": {"app.py": app('''
            @component
            def Greeting(name):
                return p(f"Hello {nmae}")  # <-- HERE

            @app.page("/")
            def home():
                return Greeting(name="Ada")
        ''')},
        "requests": [("GET", "/", None)],
        "needles": ["'nmae' is not defined"],
        "location": True,
        "expect": "J103",
    },
    {
        "name": "hook called in a page function",
        "files": {"app.py": app('''
            @app.page("/")
            def home():
                count = state(0)  # <-- HERE
                return div(p(count.value))
        ''')},
        "requests": [("GET", "/", None)],
        "needles": ["can only be called while a @component is rendering"],
        "location": True,
        "expect": "J107",
    },
    {
        "name": "component returns a list",
        "files": {"app.py": app('''
            @component  # <-- HERE
            def Items():
                return [li("a"), li("b")]

            @app.page("/")
            def home():
                return ul(Items())
        ''')},
        "requests": [("GET", "/", None)],
        "needles": ["returned a list"],
        "location": True,
        "alt_lines": [1],  # the def line just below the decorator
        "expect": "J110",
    },
    {
        "name": "model changed, migration not run",
        "files": {"app.py": app('''
            class Todo(db.Model):
                title = db.Text()
                due = db.Text(null=True)

            @app.page("/")
            def home():
                return ul(*[li(t.title) for t in Todo.filter(due=None)])
        ''', database='"app.sqlite3"')},
        "setup": "import sqlite3\nc = sqlite3.connect('app.sqlite3')\n"
                 "c.execute('CREATE TABLE todo (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL)')\n"
                 "c.execute(\"INSERT INTO todo (title) VALUES ('milk')\")\nc.commit()\n",
        "env": {"JONGO_DEV": ""},   # deployed with `jongo run` / gunicorn, which don't migrate unasked
        "migrate": False,
        "requests": [("GET", "/", None)],
        "needles": ["no such column", "todo.due", '"todo"."due"'],
        "location": False,
        "expect": "J302",
    },
    {
        "name": "server function called with the wrong type",
        "files": {"app.py": app('''
            @server
            def add_days(days: int) -> int:
                return days * 2

            @app.page("/")
            def home():
                return p("hi")
        ''')},
        "requests": [("RPC", "app.add_days", {"args": ["three"]})],
        "needles": ["must be int"],
        "location": False,
        "expect": "J403",
    },
    {
        "name": "server function called with too many arguments",
        "files": {"app.py": app('''
            @server
            def rename(title: str) -> str:
                return title

            @app.page("/")
            def home():
                return p("hi")
        ''')},
        "requests": [("RPC", "app.rename", {"args": ["a", "b"]})],
        "needles": ["too many positional arguments"],
        "location": False,
        "expect": "J404",
    },
    {
        "name": "call to a server function that does not exist",
        "files": {"app.py": app('''
            @server
            def save(text: str) -> str:
                return text

            @app.page("/")
            def home():
                return p("hi")
        ''')},
        "requests": [("RPC", "app.svae", {"args": ["x"]})],
        "needles": ["no server function"],
        "location": False,
        "expect": "J401",
    },
    {
        "name": "unknown route converter",
        "files": {"app.py": app('''
            @app.page("/posts/<integer:id>")  # <-- HERE
            def post(id):
                return p(id)
        ''')},
        "requests": [("GET", "/posts/1", None)],
        "needles": ["unknown converter"],
        "location": True,
        "expect": "J201",
    },
    {
        "name": "SQLite volume not mounted (unwritable path)",
        "files": {"app.py": app('''
            class Note(db.Model):
                text = db.Text()

            @app.page("/")
            def home():
                return ul(*[li(n.text) for n in Note.all()])
        ''', database='"/jongo-missing-volume/app.sqlite3"')},
        "requests": [("GET", "/", None)],
        "needles": ["jongo-missing-volume"],
        "location": False,
        "expect": "J301",
    },
    {
        "name": "PostgreSQL server not running",
        "files": {"app.py": app('''
            class Note(db.Model):
                text = db.Text()

            @app.page("/")
            def home():
                return ul(*[li(n.text) for n in Note.all()])
        ''', database='"postgres://jongo@127.0.0.1:5999/notes"')},
        "requests": [("GET", "/", None)],
        "needles": ["5999", "connection refused"],
        "location": False,
        "expect": "J301",
    },
    {
        "name": "production deploy with a weak secret key",
        "files": {"app.py": app('''
            @app.page("/")
            def home():
                return p("hi")
        ''')},
        "env": {"JONGO_ENV": "production", "JONGO_SECRET_KEY": "dev", "JONGO_DEV": ""},
        "requests": [("GET", "/", None)],
        "needles": ["secret key", "JONGO_SECRET_KEY"],
        "location": False,
        "expect": "J501",
    },
    {
        "name": "server function hint names an undefined type",
        "files": {"app.py": app('''
            @server  # <-- HERE
            def archive(note: "Note") -> None:
                pass

            @app.page("/")
            def home():
                return p("hi")
        ''')},
        "requests": [("RPC", "app.archive", {"args": [1]})],
        "needles": ["can't resolve the type"],
        "location": True,
        "alt_lines": [1],
        "expect": "J405",
    },
    {
        "name": "two server functions built by one factory",
        "files": {"app.py": app('''
            def make(kind):
                @server  # <-- HERE
                def handler(x: int) -> str:
                    return f"{kind}:{x}"
                return handler

            a = make("a")
            b = make("b")

            @app.page("/")
            def home():
                return p("hi")
        ''')},
        "requests": [("GET", "/", None)],
        "needles": ["resolve to the same id"],
        "location": True,
        "alt_lines": [1],
        "expect": "J406",
    },
    {
        "name": "HTML form posted without the CSRF token",
        "files": {"app.py": app('''
            @app.post("/subscribe")
            def subscribe(request):
                return "thanks"

            @app.page("/")
            def home():
                return form(input_(name="email"), button("Go"), method="post", action="/subscribe")
        ''')},
        "requests": [("RAWPOST", "/subscribe", "email=a@b.c")],
        "needles": ["CSRF"],
        "location": False,
        "expect": "J207",
    },
    {
        "name": "module without a Jongo app",
        "files": {"app.py": "from jongo import Jongo\nmy_app = None\n"},
        "requests": [],
        "needles": ["doesn't define a Jongo app"],
        "location": False,
        "expect": "J504",
    },
    {
        "name": "duplicate value for a unique field",
        "files": {"app.py": app('''
            class User(db.Model):
                email = db.Text(unique=True)

            @server
            def signup(email: str) -> dict:
                return User.create(email=email).to_dict()

            @app.page("/")
            def home():
                return p("hi")
        ''')},
        "requests": [("RPC", "app.signup", {"args": ["a@b.c"]}),
                     ("RPC", "app.signup", {"args": ["a@b.c"]})],
        "needles": ["already exists"],
        "location": False,
        "expect": "J304",
    },
    {
        "name": "url_for with a route name that does not exist",
        "files": {"app.py": app('''
            @app.page("/")
            def home():
                return a("Profile", href=app.url_for("profil"))  # <-- HERE

            @app.page("/profile", name="profile")
            def profile():
                return p("me")
        ''')},
        "requests": [("GET", "/", None)],
        "needles": ["no route named"],
        "location": True,
        "expect": "J203",
    },
]
