"""Regression tests for 0.7.0: ORM fields, queries and migrations (stress-test findings).

Every test runs against a fresh database (in-memory SQLite by default, or the
``JONGO_TEST_DATABASE`` PostgreSQL server) with an empty model registry.
"""
from __future__ import annotations

import pytest

from jongo import db
from jongo.db import connection

from conftest import on_postgres, sqlite_only, use_test_database


@pytest.fixture(autouse=True)
def memory_db():
    saved_registry = dict(db.models_registry)
    saved_database = connection._database
    db.models_registry.clear()
    use_test_database()
    yield
    db.close_connections()
    db.models_registry.clear()
    db.models_registry.update(saved_registry)
    connection._database = saved_database


# ------------------------------------------- help_text= alias and on_delete="NO ACTION" ---


def test_help_text_is_an_alias_for_help():
    field = db.Text(help_text="Given name")
    assert field.help == "Given name" and field.help_text == "Given name"
    assert db.Text(help="Shown below").help_text == "Shown below"
    assert db.ForeignKey("Anything", help_text="Pick one").help == "Pick one"


def test_help_and_help_text_together_is_an_error():
    with pytest.raises(TypeError, match="help= or help_text="):
        db.Int(help="a", help_text="b")


def test_on_delete_no_action_is_accepted_and_migrates_once():
    class Author(db.Model):
        name = db.Text()

    class Book(db.Model):
        title = db.Text()
        author = db.ForeignKey(Author, on_delete="no_action")

    assert Book._meta.field("author").on_delete == "NO ACTION"
    db.migrate()
    assert db.plan_migrations() == []  # introspection reads it back the same way: no rebuild loop
    author = Author.create(name="Le Guin")
    Book.create(title="The Dispossessed", author=author)
    with pytest.raises(connection.dialect().integrity_errors()):  # refuses to orphan the book, like RESTRICT
        author.delete()
    assert Book.count() == 1


def test_on_delete_still_rejects_unknown_actions():
    with pytest.raises(ValueError, match="NO ACTION"):
        db.ForeignKey("Anything", on_delete="SET DEFAULT")


# ------------------------------------------ ForeignKey to an undefined model keeps J310 ---


def test_foreign_key_to_an_undefined_model_is_j310_not_j303():
    from jongo.errors import classify

    class Review(db.Model):
        body = db.Text()
        owner = db.ForeignKey("Nope")

    with pytest.raises(db.MigrationError) as caught:  # still a MigrationError for existing except clauses
        db.plan_migrations()
    assert caught.value.code == "J310"
    assert 'points to model "Nope"' in str(caught.value)
    code, hint = classify(caught.value)
    assert code == "J310" and "null=True" not in hint and "define (import)" in hint


def test_jongo_check_reports_j310_for_a_foreign_key_to_an_undefined_model():
    from jongo import Jongo
    from jongo.introspect import check

    app = Jongo(__name__, database=":memory:")

    class Review(db.Model):
        body = db.Text()
        owner = db.ForeignKey("Nope")

    problems = [p for p in check(app)["problems"] if p["kind"] == "migrations"]
    assert [p["code"] for p in problems] == ["J310"]
    assert "Nope" in problems[0]["message"] and "null=True" not in problems[0]["hint"]
