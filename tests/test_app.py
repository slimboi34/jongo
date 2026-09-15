"""End-to-end tests of the web layer through the in-process test client."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from jongo import HTTPError, Jongo, Page, component, css, db, redirect, server, state
from jongo.html import *
from jongo.vdom import render_to_string

HERE = Path(__file__).parent


class Note(db.Model):
    text = db.Text(max_length=100)
    pinned = db.Bool(default=False)


box = css(note={"padding": 8, ":hover": {"color": "red"}})


@component
def NoteList(notes, heading="Notes"):
    items = state(notes)
    return div(
        h2(heading),
        ul([li(n["text"], key=n["id"], class_=box.note) for n in items.value]),
        button("Clear", on_click=lambda e: items.set([])),
    )


@component
def Shell(children, user=None):
    return div(p(f"Hi {user}" if user else "Signed out"), children)


@server
def add_note(text: str, pinned: bool = False) -> dict:
    return Note.create(text=text, pinned=pinned).to_dict()


@server
def pin(note: Note) -> dict:
    note.pinned = True
    note.save()
    return note.to_dict()


@server
def sign_in(request, username: str, password: str):
    from jongo.auth import authenticate, login

    user = authenticate(username, password)
    if user is None:
        raise HTTPError(400, "Wrong username or password")
    login(request, user)
    return redirect("/")


@server(login_required=True)
def whoami(request) -> str:
    return request.user.username


@pytest.fixture
def app(monkeypatch):
    from jongo import auth

    monkeypatch.setattr(auth, "ITERATIONS", 1000)
    application = Jongo("test_app", root=HERE, dev=True, database=":memory:", secret_key="test-secret")
    db.migrate()
    application.layout(Shell, props=lambda request: {"user": request.user.username if request.user else None})

    @application.page("/", title="Home")
    def home():
        return NoteList(notes=Note.all(), heading="All notes")

    @application.page("/notes/<id>")
    def note(id: int):
        found = Note.filter(id=id).first()
        if not found:
            raise HTTPError(404, "No such note")
        return Page(p(found.text), title=found.text)

    @application.page("/broken")
    def broken():
        return button("x", on_click=lambda e: None)

    @application.get("/api/add")
    def add(a: int, b: int = 0):
        return {"sum": a + b}

    @application.post("/form")
    def form_post(request):
        return f"got {request.form['name']}"

    return application


@pytest.fixture
def client(app):
    return app.test_client()


def test_page_is_server_rendered_with_boot_data(client):
    Note.create(text="first note")
    response = client.get("/")
    assert response.status == 200
    assert "<title>Home</title>" in response.text
    assert "<h2>All notes</h2>" in response.text
    assert "first note" in response.text
    assert "Signed out" in response.text
    boot = json.loads(re.search(r'<script id="jongo-data" type="application/json">(.*?)</script>', response.text).group(1))
    assert boot["tree"]["C"].endswith("Shell")
    assert boot["dev"] is True
    assert re.search(r"/_jongo/app\.js\?v=\w+", response.text)


def test_navigation_returns_json_tree(client):
    data = client.navigate("/")
    assert data["title"] == "Home"
    assert data["tree"]["c"][0]["C"].endswith("NoteList")
    assert data["build"]


def test_path_converter_from_annotation_and_404(client):
    note = Note.create(text="hello")
    assert "<p>hello</p>" in client.get(f"/notes/{note.id}").text
    assert client.get("/notes/abc").status == 404
    missing = client.get("/notes/999")
    assert missing.status == 404 and "No such note" in missing.text


def test_rpc_roundtrip_and_type_validation(client):
    note = client.rpc(add_note, "buy milk", pinned=True)
    assert note["text"] == "buy milk" and note["pinned"] is True
    with pytest.raises(Exception) as err:
        client.rpc(add_note, 42)
    assert err.value.status == 400 and "must be str" in str(err.value)


def test_rpc_model_argument(client):
    note = Note.create(text="pin me")
    assert client.rpc(pin, note.id)["pinned"] is True
    with pytest.raises(Exception) as err:
        client.rpc(pin, 12345)
    assert err.value.status == 404


def test_rpc_requires_csrf_token(client):
    client.get("/")
    response = client.post(f"/_jongo/rpc/{add_note.id}", json={"args": ["x"]}, headers={"X-CSRF-Token": "forged"})
    assert response.status == 403


def test_csrf_survives_messy_cookies_from_other_localhost_apps(client):
    client.get("/")
    token = client.cookies["jongo_csrf"]
    messy = f'prefs={{"theme": "dark"}}; other="unterminated; jongo_csrf={token}; x=1'
    response = client.post(
        f"/_jongo/rpc/{add_note.id}",
        json={"args": ["from a messy browser"]},
        headers={"X-CSRF-Token": token, "Cookie": messy},
    )
    assert response.status == 200, response.text


def test_login_flow_and_request_user(client):
    from jongo.auth import User

    User.create_user("ada", "correct horse")
    with pytest.raises(Exception) as err:
        client.rpc(whoami)
    assert err.value.status == 401
    with pytest.raises(Exception):
        client.rpc(sign_in, "ada", "wrong")
    assert client.rpc(sign_in, "ada", "correct horse") == {"redirect": "/"}
    assert client.rpc(whoami) == "ada"
    assert "Hi ada" in client.get("/").text


def test_tampered_session_is_ignored(client):
    from jongo.auth import User

    User.create_user("bob", "pw")
    client.rpc(sign_in, "bob", "pw")
    client.cookies["jongo_session"] = client.cookies["jongo_session"][:-2] + "xx"
    assert "Signed out" in client.get("/").text


def test_handlers_outside_components_give_a_helpful_error(client):
    response = client.get("/broken")
    assert response.status == 500
    assert "outside any @component" in response.text


def test_routes_coerce_query_params(client):
    assert client.get("/api/add?a=2&b=40").json() == {"sum": 42}
    assert client.get("/api/add?a=two").status == 400


def test_form_post_needs_csrf_field(client):
    client.get("/")
    assert client.post("/form", data={"name": "x"}, headers={"X-CSRF-Token": ""}).status == 403
    token = client.cookies["jongo_csrf"]
    assert client.post("/form", data={"name": "Ada", "csrf_token": token}).text == "got Ada"


def test_assets_are_served(client):
    js = client.get("/_jongo/app.js")
    assert js.status == 200 and "compiled from Python" in js.text
    css_response = client.get("/_jongo/app.css")
    assert ".note-" in css_response.text and ":hover" in css_response.text


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_bundle_is_valid_javascript(app, tmp_path):
    js, _ = app.bundle()
    path = tmp_path / "app.js"
    path.write_text(js)
    result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_example_todo_app_compiles(tmp_path):
    example = HERE.parent / "examples" / "todo"
    code = (
        "import sys; sys.path.insert(0, '.');"
        "import app; js, h = app.app.bundle(); open(sys.argv[1], 'w').write(js)"
    )
    out = tmp_path / "todo.js"
    import sys

    result = subprocess.run(
        [sys.executable, "-c", code, str(out)], cwd=example, capture_output=True, text=True,
        env={"JONGO_DATABASE": ":memory:", "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    if shutil.which("node"):
        check = subprocess.run(["node", "--check", str(out)], capture_output=True, text=True)
        assert check.returncode == 0, check.stderr


def test_render_to_string_rules():
    html = render_to_string(
        div(
            "a", "b",
            input_(disabled=True, value="x<y"),
            span(class_=["btn", {"on": True, "off": False}], style={"font_size": 12, "opacity": 0.5}),
            data_id=7,
            aria_label="Box",
        )
    )
    assert html == (
        '<div data-id="7" aria-label="Box">a<!---->b<input disabled value="x&lt;y">'
        '<span class="btn on" style="font-size: 12px; opacity: 0.5"></span></div>'
    )
