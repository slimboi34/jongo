"""Testing a Jongo app: the page, the server function and the channel.

Run it:  .venv/bin/python -m pytest examples/patterns/12_testing.py

What to notice
--------------
* `TestClient` drives the real WSGI app: routing, sessions and CSRF all run.
* `client.rpc(fn, ...)` calls a `@server` function over a full HTTP round trip, so the
  test exercises the same path the browser does — argument coercion included.
* `client.navigate(path)` fetches a page the way the in-browser router does, returning
  the JSON tree rather than HTML.
* `client.stream(path)` opens an SSE stream without consuming it, for channels.
"""

import pytest

from jongo import Jongo, broadcast, component, db, server, state
from jongo.html import button, div, h1, li, ul
from jongo.testing import TestClient


def build_app():
    app = Jongo("pattern_testing", title="Counter", secret_key="test-secret")

    class Click(db.Model):
        label = db.Text(max_length=50)

    @app.channel("clicks")
    def clicks(request):
        return True

    @server
    def record(request, label: str) -> dict:
        row = Click.create(label=label)
        broadcast("clicks", row.to_dict())
        return row.to_dict()

    @component
    def Counter(initial):
        count = state(initial)
        return div(
            button(f"clicked {count.value}", on_click=lambda e: count.set(count.value + 1)),
        )

    @app.page("/")
    def home():
        return div(h1("Counter"), Counter(initial=0),
                   ul([li(c.label, key=c.pk) for c in Click.all()]))

    return app, Click, record


@pytest.fixture
def app():
    db.configure(":memory:")
    application, model, record = build_app()
    db.migrate([model])
    yield application, model, record
    db.close_connections()


def test_the_page_renders_on_the_server(app):
    application, _, _ = app
    response = TestClient(application).get("/")
    assert response.status == 200
    assert "clicked 0" in response.text          # server-rendered, before any JavaScript


def test_a_server_function_over_a_real_round_trip(app):
    application, Click, record = app
    client = TestClient(application)
    result = client.rpc(record, "first")         # CSRF token handled like a browser
    assert result["label"] == "first"
    assert Click.count() == 1


def test_navigation_returns_the_tree(app):
    application, _, _ = app
    page = TestClient(application).navigate("/")
    assert page["title"] == "Counter"


def test_a_channel_delivers(app):
    application, _, record = app
    client = TestClient(application)
    response, chunks = client.stream("/_jongo/live?channels=clicks")
    try:
        assert response.status == 200
        client.rpc(record, "watched")
        frames = b"".join(next(chunks) for _ in range(2))
        assert b"watched" in frames
    finally:
        chunks.close()


def test_an_undeclared_channel_is_refused(app):
    application, _, _ = app
    assert TestClient(application).get("/_jongo/live?channels=secret").status == 403
