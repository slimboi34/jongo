"""Testing a Jongo app: the page, the server function and the channel.

Run it:  .venv/bin/python -m pytest examples/patterns/12_testing.py

What to notice
--------------
* The app is defined at module level, exactly as in `app.py`; the tests import it. Only
  the database is set up per test: the `fresh_database` fixture points Jongo at a new
  in-memory SQLite database and creates the tables, so tests never touch the app's real
  data file and never see each other's rows. Put that fixture in `tests/conftest.py`
  (`jongo new` writes one for you).
* `TestClient` drives the real WSGI app: routing, sessions and CSRF all run.
  `response.status` is the status code as an int, `response.text` the body.
* `client.rpc(fn, ...)` calls a `@server` function over a full HTTP round trip, so the
  test exercises the same path the browser does — argument coercion included. A refused
  call raises `jongo.ServerError` with `.status`, `.code` and `.errors`.
* `client.navigate(path)` fetches a page the way the in-browser router does, returning
  the JSON tree rather than HTML.
* `client.stream(path)` opens an SSE stream without consuming it, for channels.
"""

import pytest

from jongo import Jongo, ServerError, broadcast, component, db, server, state
from jongo.html import button, div, h1, li, ul
from jongo.testing import TestClient

# -- the app under test (in a real project this is app.py, and the tests import it) --------

app = Jongo(__name__, title="Counter", secret_key="test-secret")


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


# -- the tests ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fresh_database():
    db.configure(":memory:")      # a new, empty database for every test
    db.migrate()                  # create the tables for every model
    yield
    db.close_connections()


def test_the_page_renders_on_the_server():
    response = TestClient(app).get("/")
    assert response.status == 200
    assert "clicked 0" in response.text          # server-rendered, before any JavaScript


def test_a_server_function_over_a_real_round_trip():
    client = TestClient(app)
    result = client.rpc(record, "first")         # CSRF token handled like a browser
    assert result["label"] == "first"
    assert Click.count() == 1


def test_each_test_starts_with_an_empty_database():
    assert Click.count() == 0                    # the row from the test above is gone


def test_a_refused_call_raises_server_error():
    with pytest.raises(ServerError) as refused:
        TestClient(app).rpc(record, 42)          # label must be a str
    assert refused.value.status == 400 and refused.value.code == "J403"


def test_navigation_returns_the_tree():
    page = TestClient(app).navigate("/")
    assert page["title"] == "Counter"


def test_a_channel_delivers():
    client = TestClient(app)
    response, chunks = client.stream("/_jongo/live?channels=clicks")
    try:
        assert response.status == 200
        client.rpc(record, "watched")
        frames = b"".join(next(chunks) for _ in range(2))
        assert b"watched" in frames
    finally:
        chunks.close()


def test_an_undeclared_channel_is_refused():
    assert TestClient(app).get("/_jongo/live?channels=secret").status == 403
