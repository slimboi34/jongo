"""Every API the guide and README promise must actually exist and behave as written.

Documentation that describes a function signature nobody can call is worse than no
documentation, so each claim in docs/guide.md that can be checked mechanically is checked
here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import jongo
from conftest import use_test_database
from jongo import (Jongo, Response, component, css, db, fragment, global_css, h, js, live,
                   navigate, raw, redirect, ref, refresh, server, state)
from jongo.html import div, h1, input_, li, p, span, ul
from jongo.testing import TestClient

GUIDE = Path(__file__).parent.parent / "docs" / "guide.md"
README = Path(__file__).parent.parent / "README.md"


@pytest.fixture(autouse=True)
def fresh():
    saved = dict(db.models_registry)
    db.models_registry.clear()
    use_test_database()
    yield
    db.close_connections()
    db.models_registry.clear()
    db.models_registry.update(saved)


# -- the public surface ------------------------------------------------------------------


def test_everything_the_guide_imports_exists():
    for name in ("Jongo", "Page", "Request", "Response", "redirect", "json_response",
                 "component", "state", "effect", "live", "ref", "navigate", "refresh",
                 "form_values", "js", "server", "css", "global_css", "h", "raw", "fragment",
                 "broadcast", "VNode", "HTTPError", "NotFound", "Forbidden", "ServerError",
                 "CompileError", "JongoError"):
        assert hasattr(jongo, name), f"jongo.{name} is documented but missing"


def test_db_surface():
    for name in ("Model", "Text", "Int", "Float", "Bool", "DateTime", "Date", "JSON",
                 "ForeignKey", "Q", "QuerySet", "ValidationError", "DoesNotExist",
                 "MultipleObjectsReturned", "transaction", "migrate", "plan_migrations",
                 "configure", "backend", "integrity_error", "query", "execute"):
        assert hasattr(db, name), f"db.{name} is documented but missing"


def test_auth_surface():
    from jongo import auth

    for name in ("User", "authenticate", "login", "logout"):
        assert hasattr(auth, name), f"jongo.auth.{name} is documented but missing"
    assert hasattr(auth.User, "create_user")


def test_cli_commands_exist():
    from jongo import cli

    source = Path(cli.__file__).read_text()
    for command in ("new", "dev", "run", "migrate", "createadmin", "routes", "shell", "build"):
        assert f'"{command}"' in source, f"jongo {command} is documented but not in the CLI"


# -- styles: the signature the guide shows --------------------------------------------


def test_css_takes_declaration_dicts_not_a_css_string():
    sheet = css(card={"padding": 16, "border_radius": 12,
                      ":hover": {"background": "#fafafa"},
                      "& h2": {"margin": 0},
                      "@media (max-width: 600px)": {"padding": 8}})
    assert sheet.card.startswith("card-")
    assert "padding" in sheet.css()
    with pytest.raises(TypeError):        # a raw string is *not* the API
        css(".card { padding: 16px }")


def test_global_css_takes_a_string_or_a_dict():
    assert global_css("body { margin: 0 }").css().strip() == "body { margin: 0 }"
    assert "margin" in global_css({"body": {"margin": 0}}).css()


# -- pages, layouts and routing ----------------------------------------------------------


def test_page_route_and_layout():
    app = Jongo("guide_pages", secret_key="k")

    @app.layout
    def Shell(children):
        return div(span("shell"), children)

    @app.page("/")
    def home():
        return div(h1("home"))

    @app.page("/posts/<int:id>")
    def post(id: int):
        return div(f"post {id}")

    @app.get("/plain")
    def plain(request):
        return {"ok": True}

    @app.route("/go", methods=("POST",))
    def go(request):
        return redirect("/")

    client = TestClient(app)
    body = client.get("/").text
    assert "home" in body and "shell" in body, "the layout must wrap the page"
    assert "post 7" in client.get("/posts/7").text
    assert client.get("/posts/seven").status == 404
    assert client.get("/plain").json() == {"ok": True}
    assert client.post("/go").status in (302, 303)


def test_a_layout_component_receives_the_page_as_children():
    """The guide writes `def Shell(children)`; this is what actually arrives."""
    app = Jongo("guide_layout", secret_key="k")

    @app.layout
    @component
    def Shell(children):
        return div(span("shell"), children)

    @app.page("/")
    def home():
        return div("inner")

    body = TestClient(app).get("/").text
    assert "inner" in body and "shell" in body


# -- components and hooks ------------------------------------------------------------------


def test_component_props_and_keys_and_conditionals():
    app = Jongo("guide_components", secret_key="k")

    @component
    def Row(title, tone="plain"):
        return li(title, class_=f"row row--{tone}")

    @component
    def List(rows, error=None):
        return div(
            ul([Row(title=row, key=row) for row in rows]),
            p(error) if error else None,          # None renders nothing
            fragment(span("a"), span("b")),
        )

    @app.page("/")
    def home():
        return List(rows=["x", "y"])

    body = TestClient(app).get("/").text
    assert "row--plain" in body and "<li" in body
    assert "None" not in body


def test_state_effect_ref_and_live_are_component_only():
    from jongo.errors import JongoError

    for hook in (lambda: state(0), lambda: ref(None), lambda: live("c", lambda d: None)):
        with pytest.raises(JongoError):
            hook()


def test_browser_only_helpers_raise_on_the_server():
    from jongo.errors import JongoError

    for fn in (navigate, refresh):
        with pytest.raises(JongoError):
            fn("/x")


# -- server functions ------------------------------------------------------------------------


def test_server_function_types_and_model_parameters():
    app = Jongo("guide_server", secret_key="k")

    class Note(db.Model):
        title = db.Text(max_length=50)

    @server
    def rename(request, note: Note, title: str) -> dict:
        note.title = title
        note.save()
        return note.to_dict()

    @server
    def counts(request, values: list[int], flags: dict[str, bool]) -> dict:
        return {"total": sum(values), "flags": flags}

    @app.page("/")
    def home():
        return div("ok")

    db.migrate([Note])
    note = Note.create(title="before")
    client = TestClient(app)

    assert client.rpc(rename, note.pk, "after")["title"] == "after"
    assert client.rpc(counts, [1, 2, 3], {"a": True}) == {"total": 6, "flags": {"a": True}}

    response = client.post(f"/_jongo/rpc/{rename.id}", json={"args": [999999, "x"]})
    assert response.status == 404, "a missing model row must 404 before the function runs"


def test_the_error_contract_the_guide_describes():
    """Forbidden and ValidationError reach the browser; anything else does not."""
    from jongo import Forbidden

    app = Jongo("guide_errors", secret_key="k")

    class Person(db.Model):
        email = db.Text(max_length=50, unique=True)

    @server
    def refuse(request) -> dict:
        raise Forbidden("not yours")

    @server
    def invalid(request, email: str) -> dict:
        return Person.create(email=email).to_dict()

    @server
    def surprise(request) -> dict:
        raise RuntimeError("internal detail that must not leak")

    @app.page("/")
    def home():
        return div("ok")

    db.migrate([Person])
    client = TestClient(app)

    refused = client.post(f"/_jongo/rpc/{refuse.id}", json={"args": []})
    assert refused.status == 403 and "not yours" in refused.text

    client.rpc(invalid, "a@b.c")
    duplicate = client.post(f"/_jongo/rpc/{invalid.id}", json={"args": ["a@b.c"]})
    assert duplicate.status == 400
    assert duplicate.json()["error"]["errors"]["email"]

    leaked = client.post(f"/_jongo/rpc/{surprise.id}", json={"args": []})
    assert leaked.status == 500
    assert "internal detail" not in leaked.text, "an unexpected exception must not leak"


# -- the ORM ---------------------------------------------------------------------------------


def test_every_documented_lookup_runs():
    class Book(db.Model):
        title = db.Text(max_length=100, index=True)
        pages = db.Int(default=0)
        tags = db.JSON(default=list)
        note = db.Text(null=True)

    db.migrate([Book])
    Book.create(title="The Wind", pages=300, tags=["classic"])
    Book.create(title="Dune", pages=500)

    lookups = {
        "title__exact": "Dune", "title__iexact": "dune", "title__contains": "un",
        "title__icontains": "UN", "title__startswith": "D", "title__istartswith": "d",
        "title__endswith": "e", "title__iendswith": "E", "pages__gt": 100,
        "pages__gte": 100, "pages__lt": 1000, "pages__lte": 1000,
        "pages__in": [300, 500], "note__isnull": True, "title__ne": "Nothing",
    }
    for lookup, value in lookups.items():
        Book.filter(**{lookup: value}).count()     # must compile and run on both backends

    assert Book.filter(db.Q(title__startswith="The") | db.Q(pages=500)).count() == 2
    assert Book.filter(title="missing").first() is None
    assert Book.filter(title="Dune").exists()
    assert Book.all().order_by("-pages")[:1][0].title == "Dune"
    with db.transaction():
        Book.filter(title="Dune").update(pages=501)
    assert Book.get(title="Dune").pages == 501


def test_validation_error_carries_field_errors():
    class Person(db.Model):
        email = db.Text(max_length=100, unique=True)

    db.migrate([Person])
    Person.create(email="a@b.c")
    with pytest.raises(db.ValidationError) as caught:
        Person.create(email="a@b.c")
    assert "email" in caught.value.errors


def test_relations_and_reverse_accessors():
    class Author(db.Model):
        name = db.Text(max_length=50)

    class Book(db.Model):
        title = db.Text(max_length=50)
        author = db.ForeignKey(Author, related_name="books", on_delete="CASCADE")

    db.migrate([Author, Book])
    author = Author.create(name="ann")
    author.books.create(title="one")
    assert Book.filter(author__name__iexact="ANN").count() == 1
    assert author.books.count() == 1


# -- the guide's own text ------------------------------------------------------------------


def test_the_guide_has_no_broken_internal_anchors():
    text = GUIDE.read_text()
    anchors = {re.sub(r"[^a-z0-9]+", "-", line.lstrip("#").strip().lower()).strip("-")
               for line in text.splitlines() if line.startswith("#")}
    for target in re.findall(r"\]\(#([a-z0-9-]+)\)", text):
        assert target in anchors, f"the guide links to #{target}, which is not a heading"


def test_the_guide_links_to_files_that_exist():
    text = GUIDE.read_text()
    root = GUIDE.parent
    for href in re.findall(r"\]\((\.\./[^)#]+)\)", text):
        assert (root / href).resolve().exists(), f"the guide links to {href}, which does not exist"


def test_the_readme_and_guide_agree_on_the_css_api():
    """Both show `css(name={...})`, never a raw CSS string."""
    for path in (README, GUIDE):
        text = path.read_text()
        assert not re.search(r"(?<!global_)css\(\s*\"\"\"", text), f"{path.name} shows css() taking a string"
