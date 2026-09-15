"""The generated admin site."""
from __future__ import annotations

from pathlib import Path

import pytest

from jongo import Jongo, auth, db


class Book(db.Model):
    title = db.Text(max_length=100)
    notes = db.Text(blank=True, default="")
    pages = db.Int(default=0)
    published = db.Bool(default=False)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "ITERATIONS", 1000)
    app = Jongo("admin_test", root=Path(__file__).parent, dev=True, database=":memory:", secret_key="s")
    app.admin()
    db.migrate()
    return app.test_client()


def log_in(client, username="root", password="pw"):
    client.get("/admin/login")
    token = client.cookies["jongo_csrf"]
    response = client.post(
        "/admin/login", data={"username": username, "password": password, "csrf_token": token, "next": "/admin"}
    )
    return response, token


def test_admin_requires_login(client):
    response = client.get("/admin")
    assert response.status == 302
    assert response.headers["location"].startswith("/admin/login?next=")


def test_non_admins_are_turned_away(client):
    auth.User.create_user("guest", "pw")
    response, _ = log_in(client, "guest")
    assert response.status == 400
    assert "not an admin" in response.text


def test_admin_create_edit_search_delete(client):
    auth.User.create_user("root", "pw", is_admin=True)
    response, token = log_in(client)
    assert response.status == 303 and response.headers["location"] == "/admin"
    assert "Books" in client.get("/admin").text

    created = client.post(
        "/admin/book/new", data={"title": "Dune", "pages": "412", "published": "on", "csrf_token": token}
    )
    assert created.status == 303, created.text
    book = Book.get(title="Dune")
    assert book.pages == 412 and book.published is True

    listing = client.get("/admin/book?q=dun")
    assert "Dune" in listing.text and "Created Dune" not in listing.text or "Created" in listing.text

    invalid = client.post(f"/admin/book/{book.id}", data={"title": "Dune Messiah", "pages": "lots", "csrf_token": token})
    assert invalid.status == 400

    updated = client.post(f"/admin/book/{book.id}", data={"title": "Dune Messiah", "pages": "300", "csrf_token": token})
    assert updated.status == 303
    book.refresh()
    assert (book.title, book.pages, book.published) == ("Dune Messiah", 300, False)

    assert "can't be undone" in client.get(f"/admin/book/{book.id}/delete").text
    deleted = client.post(f"/admin/book/{book.id}/delete", data={"csrf_token": token})
    assert deleted.status == 303
    assert Book.count() == 0


def test_admin_sets_user_passwords(client):
    auth.User.create_user("root", "pw", is_admin=True)
    _, token = log_in(client)
    response = client.post(
        "/admin/user/new", data={"username": "grace", "_password": "s3cret", "csrf_token": token}
    )
    assert response.status == 303, response.text
    assert auth.authenticate("grace", "s3cret") is not None
    assert "s3cret" not in client.get("/admin/user").text
