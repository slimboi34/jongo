"""Tests for jongo.db: connections, fields, models, querysets, relations and migrations."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from jongo import db
from jongo.db import connection, models as models_module


@pytest.fixture(autouse=True)
def memory_db():
    """Give every test a fresh in-memory database and an empty model registry."""
    saved_registry = dict(db.models_registry)
    saved_database = connection._database
    db.models_registry.clear()
    db.configure(":memory:")
    yield
    db.close_connections()
    db.models_registry.clear()
    db.models_registry.update(saved_registry)
    connection._database = saved_database


@pytest.fixture
def app():
    class User(db.Model):
        username = db.Text(max_length=50, unique=True)
        score = db.Float(default=0.0)
        active = db.Bool(default=True)

        def __str__(self):
            return self.username

    class Todo(db.Model):
        title = db.Text(max_length=200)
        done = db.Bool(default=False)
        priority = db.Int(default=2, choices=[(1, "Low"), (2, "Normal"), (3, "High")])
        due = db.Date(null=True)
        notes = db.Text(null=True, blank=True)
        created = db.DateTime(auto_now_add=True)
        updated = db.DateTime(auto_now=True)
        data = db.JSON(default=dict)
        owner = db.ForeignKey("User", null=True, related_name="todos", on_delete="SET NULL")

        class Meta:
            table = "todos"

    class Comment(db.Model):
        body = db.Text()
        todo = db.ForeignKey(Todo)

    db.migrate()
    return SimpleNamespace(User=User, Todo=Todo, Comment=Comment)


@pytest.fixture
def people(app):
    alice = app.User.create(username="alice", score=9.5)
    bob = app.User.create(username="bob", score=4.0, active=False)
    todos = [
        app.Todo.create(title="Buy milk", owner=alice, priority=1),
        app.Todo.create(title="Write REPORT", owner=alice, priority=3, done=True),
        app.Todo.create(title="walk the dog", owner=bob, notes="twice"),
        app.Todo.create(title="100% effort_now", priority=3),
    ]
    return SimpleNamespace(alice=alice, bob=bob, todos=todos, **vars(app))


def titles(queryset):
    return [todo.title for todo in queryset]


def count_selects():
    """Return a list that collects SELECT statements run on the current connection."""
    statements: list[str] = []
    db.get_connection().set_trace_callback(
        lambda sql: statements.append(sql) if sql.lstrip().upper().startswith("SELECT") else None
    )
    return statements


# -- connection -----------------------------------------------------------------------


class TestConnection:
    def test_parse_urls(self, tmp_path):
        assert connection._parse_url(":memory:") == ":memory:"
        assert connection._parse_url("sqlite:///:memory:") == ":memory:"
        assert connection._parse_url("sqlite://") == ":memory:"
        assert connection._parse_url(f"sqlite:///{tmp_path}/abs.sqlite3") == str(tmp_path / "abs.sqlite3")
        assert connection._parse_url("sqlite:///rel.sqlite3").endswith("/rel.sqlite3")
        assert connection._parse_url(tmp_path / "plain.sqlite3") == str(tmp_path / "plain.sqlite3")
        with pytest.raises(ValueError):
            connection._parse_url("postgres://localhost/app")

    def test_default_database_from_env_or_cwd(self, tmp_path, monkeypatch):
        db.close_connections()
        monkeypatch.setattr(connection, "_database", None)
        monkeypatch.setenv("JONGO_DATABASE", str(tmp_path / "env.sqlite3"))
        assert db.database_path() == str(tmp_path / "env.sqlite3")

        monkeypatch.setattr(connection, "_database", None)
        monkeypatch.delenv("JONGO_DATABASE")
        monkeypatch.chdir(tmp_path)
        assert db.database_path() == str(tmp_path / "db.sqlite3")

    def test_execute_and_query(self):
        db.execute("CREATE TABLE things (id INTEGER PRIMARY KEY, name TEXT)")
        cursor = db.execute("INSERT INTO things (name) VALUES (?)", ["widget"])
        assert cursor.lastrowid == 1
        rows = db.query("SELECT id, name FROM things WHERE name = ?", ("widget",))
        assert rows == [{"id": 1, "name": "widget"}]
        assert type(rows[0]) is dict

    def test_file_connection_settings(self, tmp_path):
        db.configure(f"sqlite:///{tmp_path}/app.sqlite3")
        conn = db.get_connection()
        assert conn is db.get_connection()
        assert conn.isolation_level is None
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert (tmp_path / "app.sqlite3").exists()

    def test_memory_connection_has_foreign_keys(self):
        assert db.get_connection().execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_configure_again_closes_connections(self, tmp_path):
        conn = db.get_connection()
        db.configure(tmp_path / "other.sqlite3")
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
        assert db.get_connection() is not conn

    def test_memory_database_is_shared_between_threads(self):
        db.execute("CREATE TABLE shared (value TEXT)")
        seen = {}

        def worker():
            seen["conn"] = db.get_connection()
            db.execute("INSERT INTO shared VALUES ('from thread')")

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        assert seen["conn"] is db.get_connection()
        assert db.query("SELECT value FROM shared") == [{"value": "from thread"}]

    def test_file_database_thread_access(self, tmp_path):
        db.configure(tmp_path / "threads.sqlite3")

        class Hit(db.Model):
            worker = db.Int()

        db.migrate([Hit])
        connections, errors = set(), []

        def worker(number):
            try:
                connections.add(id(db.get_connection()))
                for _ in range(25):
                    Hit.create(worker=number)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert len(connections) == 4
        assert Hit.count() == 100
        assert sorted(set(Hit.all().values_list("worker", flat=True))) == [0, 1, 2, 3]


# -- transactions ------------------------------------------------------------------------


class TestTransactions:
    @pytest.fixture
    def Item(self):
        class Item(db.Model):
            name = db.Text()

        db.migrate([Item])
        return Item

    def test_commit(self, Item):
        with db.transaction():
            Item.create(name="a")
            Item.create(name="b")
        assert Item.count() == 2
        assert not db.get_connection().in_transaction

    def test_rollback_on_error(self, Item):
        with pytest.raises(RuntimeError):
            with db.transaction():
                Item.create(name="a")
                raise RuntimeError("boom")
        assert Item.count() == 0
        assert not db.get_connection().in_transaction

    def test_nested_savepoint_rollback(self, Item):
        with db.transaction():
            Item.create(name="outer")
            with pytest.raises(ValueError):
                with db.transaction():
                    Item.create(name="inner")
                    raise ValueError
            with db.transaction():
                Item.create(name="inner kept")
        assert sorted(Item.all().values_list("name", flat=True)) == ["inner kept", "outer"]

    def test_outer_rollback_discards_released_savepoints(self, Item):
        with pytest.raises(KeyError):
            with db.transaction():
                with db.transaction():
                    Item.create(name="inner")
                raise KeyError
        assert Item.count() == 0

    def test_decorator_forms(self, Item):
        @db.transaction
        def bare(fail):
            Item.create(name="bare")
            if fail:
                raise ValueError

        @db.transaction()
        def called():
            Item.create(name="called")

        bare(False)
        called()
        with pytest.raises(ValueError):
            bare(True)
        assert sorted(Item.all().values_list("name", flat=True)) == ["bare", "called"]


# -- fields ----------------------------------------------------------------------------------


class TestFieldMetadata:
    def test_metadata(self, app):
        meta = app.Todo._meta
        title, done, priority = meta.field("title"), meta.field("done"), meta.field("priority")
        owner, created, data = meta.field("owner"), meta.field("created"), meta.field("data")
        assert [f.kind for f in meta.fields] == [
            "text", "bool", "int", "date", "text", "datetime", "datetime", "json", "fk"
        ]
        assert [f.sql_type for f in (title, done, priority, created, data, owner)] == [
            "TEXT", "INTEGER", "INTEGER", "TEXT", "TEXT", "INTEGER"
        ]
        assert title.name == "title" and title.column == "title" and title.model is app.Todo
        assert title.max_length == 200 and title.required
        assert owner.column == "owner_id" and owner.related_model is app.User and not owner.required
        assert owner.on_delete == "SET NULL" and owner.index
        assert priority.choices == [(1, "Low"), (2, "Normal"), (3, "High")]
        assert priority.choice_label(3) == "High"
        assert created.editable is False and title.editable is True
        assert meta.field("updated").auto_now and created.auto_now_add
        assert data.has_default() and data.get_default() == {} and data.get_default() is not data.get_default()
        assert not title.has_default()
        assert meta.field("notes").null and meta.field("notes").blank

    def test_labels_and_help(self):
        class Profile(db.Model):
            first_name = db.Text(help="Given name")
            bio = db.Text(label="About you")
            tags = db.JSON(choices=["a", "b"])

        meta = Profile._meta
        assert meta.field("first_name").label == "First name"
        assert meta.field("first_name").help == "Given name"
        assert meta.field("bio").label == "About you"
        assert meta.field("tags").choices == [("a", "a"), ("b", "b")]
        assert meta.pk.label == "ID" and meta.pk.editable is False

    def test_invalid_on_delete(self):
        with pytest.raises(ValueError):
            db.ForeignKey("User", on_delete="EXPLODE")
        with pytest.raises(ValueError):
            db.ForeignKey("User", on_delete="SET NULL")
        assert db.ForeignKey("User", null=True, on_delete="set_null").on_delete == "SET NULL"


class TestFieldCleaning:
    def bound(self, field, name="value"):
        field.bind(None, name)
        return field

    def test_bool(self):
        field = self.bound(db.Bool())
        for raw in ("on", "true", "1", "YES", True, 1):
            assert field.clean(raw) is True
        for raw in ("off", "false", "0", "", None, False, 0):
            assert field.clean(raw) is False
        assert self.bound(db.Bool(null=True)).clean("") is None
        with pytest.raises(db.ValidationError) as info:
            field.clean("maybe")
        assert info.value.errors == {"value": "Enter true or false."}

    def test_int_and_float(self):
        number = self.bound(db.Int(null=True))
        assert number.clean(" 12 ") == 12
        assert number.clean("3.0") == 3
        assert number.clean("") is None
        assert number.clean(7) == 7
        for bad in ("1.5", "abc", 2.5):
            with pytest.raises(db.ValidationError, match="whole number"):
                number.clean(bad)
        with pytest.raises(db.ValidationError, match="required"):
            self.bound(db.Int()).clean("")

        real = self.bound(db.Float())
        assert real.clean("2.5") == 2.5
        assert real.clean(3) == 3.0
        with pytest.raises(db.ValidationError, match="Enter a number"):
            real.clean("nope")

    def test_text(self):
        required = self.bound(db.Text(max_length=5), "title")
        assert required.clean("hello") == "hello"
        assert required.clean(42) == "42"
        with pytest.raises(db.ValidationError) as info:
            required.clean("")
        assert info.value.errors == {"title": "This field is required."}
        with pytest.raises(db.ValidationError, match="at most 5 characters"):
            required.clean("too long")
        assert self.bound(db.Text(null=True)).clean("") is None
        assert self.bound(db.Text(blank=True)).clean(None) == ""

    def test_choices(self):
        field = self.bound(db.Int(choices=[(1, "One"), (2, "Two")]))
        assert field.clean("2") == 2
        with pytest.raises(db.ValidationError, match="valid choice"):
            field.clean("3")

    def test_datetime_and_date(self):
        stamp = self.bound(db.DateTime())
        aware = stamp.clean("2024-05-01T12:30:00Z")
        assert aware == datetime(2024, 5, 1, 12, 30, tzinfo=timezone.utc)
        assert stamp.clean("2024-05-01T12:30") == datetime(2024, 5, 1, 12, 30)
        assert stamp.clean(date(2024, 5, 1)) == datetime(2024, 5, 1)
        with pytest.raises(db.ValidationError, match="valid date and time"):
            stamp.clean("yesterday")
        assert self.bound(db.DateTime(null=True)).clean("") is None

        day = self.bound(db.Date())
        assert day.clean("2024-05-01") == date(2024, 5, 1)
        assert day.clean(datetime(2024, 5, 1, 9)) == date(2024, 5, 1)
        assert day.clean("2024-05-01T10:00:00") == date(2024, 5, 1)
        with pytest.raises(db.ValidationError, match="valid date"):
            day.clean("01/05/2024")

    def test_json(self):
        field = self.bound(db.JSON(null=True))
        assert field.clean('{"a": [1, 2]}') == {"a": [1, 2]}
        assert field.clean([1, 2]) == [1, 2]
        assert field.clean("") is None
        with pytest.raises(db.ValidationError, match="valid JSON"):
            field.clean("{bad")
        with pytest.raises(db.ValidationError, match="serialisable"):
            field.clean({"when": object()})
        with pytest.raises(db.ValidationError, match="required"):
            self.bound(db.JSON()).clean(None)

    def test_foreign_key_clean(self, people):
        owner = people.Todo._meta.field("owner")
        assert owner.clean(str(people.bob.pk)) == people.bob.pk
        assert owner.clean(people.alice) == people.alice.pk
        assert owner.clean("") is None
        with pytest.raises(db.ValidationError, match="does not exist"):
            owner.clean("999")
        with pytest.raises(db.ValidationError, match="valid choice"):
            owner.clean("abc")

    def test_db_round_trip(self):
        stamp = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        assert db.DateTime().to_python(db.DateTime().to_db(stamp)) == stamp
        assert db.Date().to_python(db.Date().to_db(date(2024, 1, 2))) == date(2024, 1, 2)
        assert db.JSON().to_python(db.JSON().to_db({"a": 1})) == {"a": 1}
        assert db.Bool().to_db(True) == 1 and db.Bool().to_python(0) is False
        assert db.DateTime().to_python("") is None

    def test_validation_error(self):
        simple = db.ValidationError("Nope")
        assert simple.errors == {"__all__": "Nope"} and str(simple) == "Nope"
        detailed = db.ValidationError({"title": "Required.", "age": ["Too", "young."]})
        assert detailed.errors == {"title": "Required.", "age": "Too young."}
        assert "title: Required." in str(detailed)


# -- models ----------------------------------------------------------------------------------


class TestModelDefinition:
    def test_meta(self, app):
        class BlogPost(db.Model):
            headline = db.Text()
            body = db.Text()

        meta = BlogPost._meta
        assert meta.name == "BlogPost" and meta.model is BlogPost
        assert meta.table == "blog_post"
        assert meta.verbose_name == "blog post" and meta.verbose_name_plural == "blog posts"
        assert [f.name for f in meta.fields] == ["headline", "body"]
        assert [f.name for f in meta.all_fields] == ["id", "headline", "body"]
        assert meta.pk.name == "id" and meta.pk.column == "id" and meta.pk.kind == "int"
        assert meta.field("pk") is meta.pk
        assert app.Todo._meta.table == "todos"
        assert app.Todo._meta.field("owner_id") is app.Todo._meta.field("owner")
        with pytest.raises(KeyError, match="no field"):
            meta.field("missing")

    def test_registry(self):
        class Widget(db.Model):
            name = db.Text()

        first = Widget
        assert db.models_registry["Widget"] is first
        assert db.get_model("widget") is first

        class Widget(db.Model):  # noqa: F811 - redefinition is the point
            label = db.Text()

        assert db.get_model("Widget") is Widget is not first
        with pytest.raises(LookupError):
            db.get_model("Nope")

    def test_abstract_inheritance(self):
        class Timestamped(db.Model):
            created = db.DateTime(auto_now_add=True)
            name = db.Text()

            class Meta:
                abstract = True
                ordering = ["name"]

        class Article(Timestamped):
            name = db.Text(max_length=10)
            body = db.Text(blank=True)

        assert "Timestamped" not in db.models_registry
        assert [f.name for f in Article._meta.fields] == ["created", "name", "body"]
        assert Article._meta.field("name").max_length == 10
        assert Article._meta.field("created").model is Article
        assert Timestamped._meta.field("created").model is Timestamped
        assert Article._meta.ordering == ["name"]
        assert [op.table for op in db.plan_migrations()] == ["article"]
        with pytest.raises(TypeError):
            Timestamped(name="x")
        with pytest.raises(TypeError):
            Timestamped.objects.all()
        with pytest.raises(TypeError, match="concrete model"):
            class Special(Article):
                extra = db.Text()

    def test_definition_errors(self):
        with pytest.raises(TypeError, match="unknown option"):
            class BadMeta(db.Model):
                name = db.Text()

                class Meta:
                    db_table = "x"

        with pytest.raises(TypeError, match="reserved"):
            class BadField(db.Model):
                save = db.Text()

        with pytest.raises(TypeError, match="ordering"):
            class BadOrdering(db.Model):
                name = db.Text()

                class Meta:
                    ordering = ["-nope"]

    def test_field_named_like_a_shortcut(self):
        class Counter(db.Model):
            count = db.Int(default=0)

        db.migrate([Counter])
        Counter.create(count=5)
        assert Counter.count() == 1
        assert Counter.first().count == 5


class TestModelInstances:
    def test_init_defaults_and_unknown_fields(self, app):
        todo = app.Todo(title="x")
        assert todo.pk is None and todo.id is None
        assert todo.done is False and todo.priority == 2 and todo.data == {}
        assert todo.notes is None and todo.owner is None and todo.owner_id is None
        assert app.User().username == ""
        with pytest.raises(TypeError, match="unexpected"):
            app.Todo(title="x", colour="red")

    def test_crud(self, app):
        todo = app.Todo.create(title="Buy milk", data={"tags": ["home"]}, due=date(2024, 6, 1))
        assert todo.pk == todo.id == 1

        fetched = app.Todo.get(pk=1)
        assert fetched == todo and fetched is not todo
        assert fetched.data == {"tags": ["home"]} and fetched.due == date(2024, 6, 1)
        assert fetched.done is False

        fetched.title = "Buy oat milk"
        fetched.done = True
        fetched.save()
        assert app.Todo.count() == 1
        assert todo.refresh().title == "Buy oat milk" and todo.done is True

        assert todo.delete() == 1
        assert todo.pk is None
        assert app.Todo.count() == 0

    def test_save_with_explicit_id_inserts(self, app):
        app.User(id=42, username="zed").save()
        assert app.User.get(id=42).username == "zed"
        assert app.User.create(username="next").pk == 43

    def test_auto_now_fields(self, app):
        todo = app.Todo.create(title="x")
        assert isinstance(todo.created, datetime) and todo.created.tzinfo is not None
        created, updated = todo.created, todo.updated
        todo.title = "y"
        todo.save()
        assert todo.created == created
        assert todo.updated > updated
        reloaded = app.Todo.get(id=todo.pk)
        assert reloaded.created == created and reloaded.updated == todo.updated

    def test_full_clean_collects_errors(self, app):
        todo = app.Todo(title="", priority=9, done="perhaps")
        with pytest.raises(db.ValidationError) as info:
            todo.save()
        assert set(info.value.errors) == {"title", "priority", "done"}
        assert app.Todo.count() == 0

    def test_full_clean_coerces_form_values(self, app):
        todo = app.Todo(title="From a form", done="on", priority="3", due="2024-02-03", data='{"a": 1}')
        todo.save()
        assert (todo.done, todo.priority, todo.due) == (True, 3, date(2024, 2, 3))
        # JSON assigned in code is stored as-is (strings are not re-parsed on save).
        assert app.Todo.get(id=todo.pk).data == '{"a": 1}'

    def test_unique_validation(self, app):
        app.User.create(username="alice")
        with pytest.raises(db.ValidationError) as info:
            app.User.create(username="alice")
        assert info.value.errors == {"username": "User with this username already exists."}
        user = app.User.get(username="alice")
        user.score = 1.0
        user.save()  # updating itself is not a duplicate

    def test_model_clean_hook(self):
        class Range(db.Model):
            low = db.Int()
            high = db.Int()

            def clean(self):
                if self.low > self.high:
                    raise db.ValidationError("low must not exceed high")

        db.migrate([Range])
        with pytest.raises(db.ValidationError) as info:
            Range.create(low=5, high=1)
        assert info.value.errors == {"__all__": "low must not exceed high"}

    def test_to_dict_is_json_safe(self, people):
        data = people.todos[0].to_dict()
        assert list(data) == [
            "id", "title", "done", "priority", "due", "notes", "created", "updated", "data", "owner_id"
        ]
        assert data["owner_id"] == people.alice.pk
        assert isinstance(data["created"], str)
        assert json.loads(json.dumps(data)) == data

    def test_equality_hash_str_repr(self, people):
        todo = people.todos[0]
        same = people.Todo.get(id=todo.pk)
        assert todo == same and hash(todo) == hash(same) and len({todo, same}) == 1
        assert todo != people.todos[1]
        assert str(todo) == f"Todo #{todo.pk}" and repr(todo) == f"<Todo: Todo #{todo.pk}>"
        assert repr(people.alice) == "<User: alice>"
        unsaved = people.Todo(title="x")
        assert unsaved != people.Todo(title="x") and unsaved == unsaved
        assert str(unsaved) == "Todo (unsaved)"
        with pytest.raises(TypeError):
            hash(unsaved)

    def test_objects_and_shortcuts(self, people):
        Todo = people.Todo
        assert isinstance(Todo.objects, db.QuerySet)
        assert Todo.objects.all().count() == Todo.all().count() == Todo.count() == 4
        assert Todo.objects.filter(done=True).count() == 1
        assert Todo.exclude(done=True).count() == 3
        assert Todo.first().pk == 1 and Todo.last().pk == 4
        with pytest.raises(AttributeError):
            people.todos[0].objects

    def test_get_or_create(self, app):
        user, created = app.User.get_or_create(username="carol", defaults={"score": 7.0})
        assert created and user.score == 7.0
        again, created = app.User.get_or_create(username="carol", defaults={"score": 1.0})
        assert not created and again == user and again.score == 7.0
        other, created = app.User.get_or_create(username__iexact="CAROL")
        assert not created and other == user


# -- querysets -------------------------------------------------------------------------------


class TestLookups:
    def test_exact_and_iexact(self, people):
        Todo = people.Todo
        assert titles(Todo.filter(title="Buy milk")) == ["Buy milk"]
        assert titles(Todo.filter(title__exact="buy milk")) == []
        assert titles(Todo.filter(title__iexact="BUY MILK")) == ["Buy milk"]
        assert titles(Todo.filter(notes=None).filter(owner=None)) == ["100% effort_now"]

    def test_contains_is_case_sensitive(self, people):
        Todo = people.Todo
        assert titles(Todo.filter(title__contains="REPORT")) == ["Write REPORT"]
        assert titles(Todo.filter(title__contains="report")) == []
        assert titles(Todo.filter(title__icontains="report")) == ["Write REPORT"]

    def test_starts_and_ends_with(self, people):
        Todo = people.Todo
        assert titles(Todo.filter(title__startswith="Buy")) == ["Buy milk"]
        assert titles(Todo.filter(title__startswith="buy")) == []
        assert titles(Todo.filter(title__istartswith="buy")) == ["Buy milk"]
        assert titles(Todo.filter(title__endswith="dog")) == ["walk the dog"]
        assert titles(Todo.filter(title__iendswith="DOG")) == ["walk the dog"]

    def test_wildcards_are_escaped(self, people):
        Todo = people.Todo
        Todo.create(title="100 percent")
        Todo.create(title="effortXnow")
        Todo.create(title="star*gazing")
        assert titles(Todo.filter(title__icontains="%")) == ["100% effort_now"]
        assert titles(Todo.filter(title__icontains="t_n")) == ["100% effort_now"]
        assert titles(Todo.filter(title__contains="_")) == ["100% effort_now"]
        assert titles(Todo.filter(title__contains="*")) == ["star*gazing"]
        assert titles(Todo.filter(title__iexact="100% EFFORT_NOW")) == ["100% effort_now"]

    def test_comparisons(self, people):
        Todo, User = people.Todo, people.User
        assert Todo.filter(priority__gt=2).count() == 2
        assert Todo.filter(priority__gte=2).count() == 3
        assert Todo.filter(priority__lt=2).count() == 1
        assert Todo.filter(priority__lte=2).count() == 2
        assert User.filter(score__gt=5).get() == people.alice
        future = datetime(2100, 1, 1, tzinfo=timezone.utc)
        assert Todo.filter(created__lt=future).count() == 4

    def test_in(self, people):
        Todo = people.Todo
        assert Todo.filter(priority__in=[1, 3]).count() == 3
        assert Todo.filter(priority__in=(p for p in [1])).count() == 1
        assert Todo.filter(priority__in=[]).count() == 0
        assert Todo.exclude(priority__in=[]).count() == 4
        assert Todo.filter(owner__in=[people.bob, None]).count() == 2
        assert Todo.filter(owner__in=people.User.filter(active=True)).count() == 2
        with pytest.raises(TypeError):
            Todo.filter(title__in="abc").count()

    def test_isnull_and_ne(self, people):
        Todo = people.Todo
        assert titles(Todo.filter(owner__isnull=True)) == ["100% effort_now"]
        assert Todo.filter(owner__isnull=False).count() == 3
        assert Todo.filter(notes__ne="twice").count() == 3  # NULLs are "not equal"
        assert Todo.filter(notes__ne=None).count() == 1

    def test_bool_date_json_values(self, people):
        Todo = people.Todo
        Todo.filter(title="Buy milk").update(due=date(2024, 3, 1), data={"k": 1})
        assert titles(Todo.filter(done=True)) == ["Write REPORT"]
        assert titles(Todo.filter(due=date(2024, 3, 1))) == ["Buy milk"]
        assert titles(Todo.filter(due__gte="2024-01-01")) == ["Buy milk"]
        assert titles(Todo.filter(data={"k": 1})) == ["Buy milk"]

    def test_pk_and_id(self, people):
        Todo = people.Todo
        assert Todo.get(pk=2) == Todo.get(id=2) == Todo.get(id="2")
        with pytest.raises(Todo.DoesNotExist):
            Todo.get(id="not a number")

    def test_unknown_field(self, people):
        with pytest.raises(ValueError, match="Cannot resolve"):
            people.Todo.filter(colour="red")
        with pytest.raises(ValueError, match="not a ForeignKey"):
            people.Todo.filter(title__length=3)

    def test_parameters_are_not_interpolated(self, people):
        assert people.Todo.filter(title="x' OR '1'='1").count() == 0
        assert people.Todo.filter(title__icontains="' OR 1=1 --").count() == 0


class TestQ:
    def test_or_and_not(self, people):
        Q, Todo = db.Q, people.Todo
        assert Todo.filter(Q(priority=1) | Q(done=True)).count() == 2
        assert Todo.filter(Q(priority=3) & Q(done=True)).count() == 1
        assert Todo.filter(~Q(priority=3)).count() == 2
        assert Todo.filter(Q(priority=3), title__startswith="Write").count() == 1
        nested = (Q(priority=3) | Q(title__icontains="milk")) & ~Q(owner=people.alice)
        assert titles(Todo.filter(nested)) == ["100% effort_now"]

    def test_empty_q(self, people):
        Q, Todo = db.Q, people.Todo
        assert Todo.filter(Q()).count() == 4
        assert Todo.filter(Q() | Q(done=True)).count() == 1
        assert not Q() and Q(done=True)

    def test_exclude_keeps_null_rows(self, people):
        Todo = people.Todo
        assert Todo.exclude(notes="twice").count() == 3
        assert Todo.exclude(owner__username="alice").count() == 2
        assert Todo.filter(~db.Q(owner__username="bob")).count() == 3


class TestQuerySet:
    def test_is_lazy_and_cached(self, people):
        statements = count_selects()
        qs = people.Todo.filter(done=False).order_by("title")
        assert statements == []
        assert len(qs) == 3
        list(qs)
        assert qs[0].title == "100% effort_now"
        assert qs.count() == 3 and qs.exists() and bool(qs)
        assert len(statements) == 1

    def test_chaining_returns_clones(self, people):
        base = people.Todo.all()
        filtered = base.filter(done=True)
        assert base is not filtered
        assert base.count() == 4 and filtered.count() == 1

    def test_ordering(self, people):
        Todo = people.Todo
        assert titles(Todo.order_by("title")) == ["100% effort_now", "Buy milk", "Write REPORT", "walk the dog"]
        assert titles(Todo.order_by("-priority", "title")) == [
            "100% effort_now", "Write REPORT", "walk the dog", "Buy milk"
        ]
        assert len(list(Todo.order_by("?"))) == 4
        with pytest.raises(ValueError):
            Todo.order_by("colour")

    def test_meta_ordering(self):
        class Name(db.Model):
            value = db.Text()

            class Meta:
                ordering = ["-value"]

        db.migrate([Name])
        for value in ["b", "c", "a"]:
            Name.create(value=value)
        assert [n.value for n in Name.all()] == ["c", "b", "a"]
        assert [n.value for n in Name.all().order_by("value")] == ["a", "b", "c"]
        assert Name.first().value == "c" and Name.last().value == "a"
        assert [n.pk for n in Name.all().order_by()] == [1, 2, 3]

    def test_slicing(self, people):
        qs = people.Todo.order_by("id")
        page = qs[1:3]
        assert isinstance(page, db.QuerySet)
        assert [t.pk for t in page] == [2, 3]
        assert [t.pk for t in qs[2:]] == [3, 4]
        assert [t.pk for t in qs[:2]] == [1, 2]
        assert [t.pk for t in qs[1:4][1:]] == [3, 4]
        assert [t.pk for t in qs[1:3][1:5]] == [3]
        assert qs[3].pk == 4
        assert page.count() == 2 and page.exists()
        assert qs[10:20].count() == 0
        with pytest.raises(IndexError):
            qs[10]
        with pytest.raises(ValueError):
            qs[-1]
        with pytest.raises(TypeError, match="sliced"):
            page.filter(done=True)
        assert page.first().pk == 2 and page.last().pk == 3

    def test_slicing_cached_queryset(self, people):
        qs = people.Todo.order_by("id")
        list(qs)
        statements = count_selects()
        assert [t.pk for t in qs[1:3]] == [2, 3]
        assert qs[0].pk == 1
        assert statements == []

    def test_values_and_values_list(self, people):
        qs = people.Todo.filter(owner=people.alice).order_by("id")
        assert qs.values("title", "owner", "done") == [
            {"title": "Buy milk", "owner": people.alice.pk, "done": False},
            {"title": "Write REPORT", "owner": people.alice.pk, "done": True},
        ]
        full = qs.values()[0]
        assert full["id"] == 1 and full["owner_id"] == people.alice.pk and isinstance(full["created"], datetime)
        assert qs.values_list("id", "priority") == [(1, 1), (2, 3)]
        assert qs.values_list("title", flat=True) == ["Buy milk", "Write REPORT"]
        assert qs[1:].values_list("id", flat=True) == [2]
        with pytest.raises(TypeError):
            qs.values_list("id", "title", flat=True)

    def test_search(self, people):
        Todo = people.Todo
        assert titles(Todo.search("MILK", ["title", "notes"])) == ["Buy milk"]
        assert titles(Todo.search("twice", ["title", "notes"])) == ["walk the dog"]
        assert Todo.search("bob", ["title", "owner__username"]).count() == 1
        assert Todo.search("  ", ["title"]).count() == 4
        assert Todo.search(None, "title").count() == 4
        assert Todo.filter(done=False).search("o", "title").count() == 2

    def test_update(self, people):
        Todo = people.Todo
        before = Todo.get(title="Buy milk").updated
        assert Todo.filter(owner=people.alice).update(done=True, priority="1") == 2
        milk = Todo.get(title="Buy milk")
        assert milk.done is True and milk.priority == 1 and milk.updated > before
        assert Todo.filter(title="nothing").update(done=True) == 0
        assert Todo.order_by("id")[2:].update(notes="bulk") == 2
        assert Todo.filter(notes="bulk").count() == 2
        with pytest.raises(db.ValidationError) as info:
            Todo.all().update(priority=99)
        assert "priority" in info.value.errors

    def test_delete(self, people):
        Todo = people.Todo
        assert Todo.filter(done=True).delete() == 1
        assert Todo.order_by("id")[:1].delete() == 1
        assert Todo.all().delete() == 2
        assert Todo.count() == 0

    def test_get_errors(self, people):
        Todo, User = people.Todo, people.User
        with pytest.raises(Todo.DoesNotExist) as info:
            Todo.get(title="missing")
        assert isinstance(info.value, db.DoesNotExist)
        assert not issubclass(Todo.DoesNotExist, User.DoesNotExist)
        with pytest.raises(Todo.MultipleObjectsReturned, match="it returned 3"):
            Todo.get(done=False)
        assert issubclass(Todo.MultipleObjectsReturned, db.MultipleObjectsReturned)
        assert Todo.filter(done=True).get().title == "Write REPORT"

    def test_first_last_empty(self, app):
        assert app.Todo.first() is None and app.Todo.last() is None
        assert not app.Todo.all().exists()

    def test_repr(self, people):
        assert repr(people.User.order_by("username")) == "<QuerySet [<User: alice>, <User: bob>]>"
        for n in range(25):
            people.User.create(username=f"user{n:02}")
        assert "remaining elements truncated" in repr(people.User.all())


# -- relations -------------------------------------------------------------------------------


class TestRelations:
    def test_forward_access_is_lazy_and_cached(self, people):
        todo = people.Todo.get(title="Buy milk")
        statements = count_selects()
        assert todo.owner == people.alice
        assert todo.owner is todo.owner
        assert len(statements) == 1
        assert todo.owner_id == people.alice.pk

    def test_assigning_relations(self, people):
        todo = people.Todo.get(title="Buy milk")
        todo.owner = people.bob
        assert todo.owner_id == people.bob.pk and todo.owner is people.bob
        todo.save()
        assert people.Todo.get(id=todo.pk).owner == people.bob

        todo.owner_id = people.alice.pk
        assert todo.owner == people.alice
        todo.owner = None
        assert todo.owner_id is None and todo.owner is None
        with pytest.raises(TypeError):
            todo.owner = people.todos[1]

    def test_unsaved_related_object(self, app):
        user = app.User(username="later")
        todo = app.Todo(title="x", owner=user)
        with pytest.raises(ValueError, match="has not been saved"):
            todo.save()
        user.save()
        todo.save()
        assert app.Todo.get(id=todo.pk).owner_id == user.pk

    def test_reverse_accessor(self, people):
        alice = people.alice
        assert isinstance(alice.todos, db.QuerySet)
        assert sorted(titles(alice.todos)) == ["Buy milk", "Write REPORT"]
        assert alice.todos.filter(done=True).count() == 1
        created = alice.todos.create(title="From reverse")
        assert created.owner == alice
        assert people.todos[0].comment_set.count() == 0
        with pytest.raises(ValueError, match="Save"):
            people.User(username="new").todos
        with pytest.raises(AttributeError):
            alice.todos = []

    def test_filter_by_instance_and_id(self, people):
        Todo = people.Todo
        assert Todo.filter(owner=people.alice).count() == 2
        assert Todo.filter(owner_id=people.bob.pk).count() == 1
        assert Todo.filter(owner__id=people.bob.pk).count() == 1
        assert Todo.filter(owner__pk__in=[people.alice.pk]).count() == 2

    def test_traversal(self, people):
        Todo, Comment = people.Todo, people.Comment
        assert titles(Todo.filter(owner__username="bob")) == ["walk the dog"]
        assert Todo.filter(owner__active=True, owner__score__gte=9).count() == 2
        Comment.create(body="nice", todo=people.todos[0])
        Comment.create(body="meh", todo=people.todos[2])
        assert [c.body for c in Comment.filter(todo__owner__username="alice")] == ["nice"]
        assert Comment.filter(todo__owner__username__icontains="B").get().body == "meh"
        assert Comment.exclude(todo__owner__username="alice").count() == 1

    def test_on_delete_cascade_and_set_null(self, people):
        Todo, Comment = people.Todo, people.Comment
        Comment.create(body="gone soon", todo=people.todos[0])
        people.alice.delete()
        assert Todo.filter(owner__isnull=True).count() == 3  # SET NULL
        people.todos[0].delete()
        assert Comment.count() == 0  # CASCADE

    def test_on_delete_restrict(self):
        class Team(db.Model):
            name = db.Text()

        class Player(db.Model):
            team = db.ForeignKey(Team, on_delete="RESTRICT")

        db.migrate([Team, Player])
        team = Team.create(name="red")
        Player.create(team=team)
        with pytest.raises(sqlite3.IntegrityError):
            team.delete()

    def test_self_reference_and_late_target(self):
        class Category(db.Model):
            name = db.Text()
            parent = db.ForeignKey("self", null=True, related_name="children")
            owner = db.ForeignKey("Owner", null=True)

        class Owner(db.Model):
            name = db.Text()

        db.migrate()
        owner = Owner.create(name="o")
        root = Category.create(name="root", owner=owner)
        Category.create(name="leaf", parent=root)
        assert [c.name for c in root.children] == ["leaf"]
        assert Category.get(name="leaf").parent == root
        assert Category.filter(parent__name="root").count() == 1
        assert owner.category_set.get() == root

    def test_reverse_accessor_follows_redefined_target(self, app):
        class User(db.Model):
            username = db.Text()

        assert db.get_model("User") is User
        db.migrate(allow_destructive=True)
        user = User.create(username="new")
        app.Todo.create(title="t", owner_id=user.pk)
        assert titles(user.todos) == ["t"]


# -- migrations ------------------------------------------------------------------------------


def columns(table):
    return {row["name"]: row for row in db.query(f'PRAGMA table_info("{table}")')}


def indexes(table):
    return {row["name"]: bool(row["unique"]) for row in db.query(f'PRAGMA index_list("{table}")')}


class TestMigrations:
    def test_create_tables(self, app):
        assert set(columns("todos")) == {
            "id", "title", "done", "priority", "due", "notes", "created", "updated", "data", "owner_id"
        }
        cols = columns("todos")
        assert cols["title"]["notnull"] == 1 and cols["notes"]["notnull"] == 0 and cols["id"]["pk"] == 1
        assert cols["done"]["type"] == "INTEGER" and cols["data"]["type"] == "TEXT"
        fks = db.query('PRAGMA foreign_key_list("todos")')
        assert [(fk["table"], fk["from"], fk["on_delete"]) for fk in fks] == [("user", "owner_id", "SET NULL")]
        assert indexes("user") == {"ux_user_username": True}
        assert indexes("todos") == {"ix_todos_owner_id": False}
        assert db.plan_migrations() == []
        assert db.migrate() == []

    def test_plan_describes_operations(self):
        class Note(db.Model):
            text = db.Text(index=True)
            parent = db.ForeignKey("self", null=True, on_delete="CASCADE")

        plan = db.plan_migrations()
        assert [op.describe() for op in plan] == ['Create table "note"']
        op = plan[0]
        assert op.kind == "create_table" and not op.destructive
        assert 'REFERENCES "note" ("id") ON DELETE CASCADE' in op.sql[0]
        assert 'CREATE INDEX "ix_note_text" ON "note" ("text")' in op.sql
        results = db.migrate(dry_run=True)
        assert [(result.kind, applied) for result, applied in results] == [("create_table", False)]
        assert "note" not in {row["name"] for row in db.query("SELECT name FROM sqlite_master")}

    def test_add_columns(self):
        class Post(db.Model):
            title = db.Text()

        db.migrate()
        Post.create(title="old")

        class Post(db.Model):  # noqa: F811
            title = db.Text()
            summary = db.Text(null=True)
            views = db.Int()
            rating = db.Float()
            published = db.Bool(default=True)
            extra = db.JSON()
            status = db.Text(default="draft", index=True)

        plan = db.plan_migrations()
        assert [op.describe() for op in plan] == [
            'Add column "post"."summary"',
            'Add column "post"."views"',
            'Add column "post"."rating"',
            'Add column "post"."published"',
            'Add column "post"."extra"',
            'Add column "post"."status"',
            'Create index "ix_post_status" on "post"."status"',
        ]
        db.migrate()
        old = Post.get(title="old")
        assert (old.summary, old.views, old.rating, old.published, old.extra, old.status) == (
            None, 0, 0.0, True, None, "draft"
        )
        assert columns("post")["views"]["notnull"] == 1
        assert indexes("post") == {"ix_post_status": False}
        assert db.plan_migrations() == []

    def test_drop_column_requires_permission(self):
        class Song(db.Model):
            name = db.Text()
            legacy = db.Text(null=True, index=True)

        db.migrate()
        Song.create(name="a", legacy="x")

        class Song(db.Model):  # noqa: F811
            name = db.Text()

        results = db.migrate()
        assert [(op.kind, op.destructive, applied) for op, applied in results] == [("drop_column", True, False)]
        assert results[0][0].describe() == 'Drop column "song"."legacy"'
        assert "legacy" in columns("song")

        results = db.migrate(allow_destructive=True)
        assert [applied for _, applied in results] == [True]
        assert set(columns("song")) == {"id", "name"}
        assert indexes("song") == {}
        assert Song.get(id=1).name == "a"
        assert db.plan_migrations() == []

    def test_type_change_rebuild_preserves_data(self, app):
        class Score(db.Model):
            player = db.Text(index=True)
            points = db.Text()

        db.migrate()
        first = Score.create(player="ann", points="12")
        Score.create(player="ben", points="7")
        db.execute("CREATE INDEX custom_points ON score (points)")

        class Score(db.Model):  # noqa: F811
            player = db.Text(index=True)
            points = db.Int()

        plan = db.plan_migrations()
        assert [op.kind for op in plan] == ["rebuild_table"]
        assert not plan[0].destructive and plan[0].rebuild
        assert "change the type of" in plan[0].describe()
        assert 'new_score' in plan[0].sql[0]
        db.migrate()
        assert columns("score")["points"]["type"] == "INTEGER"
        assert Score.get(id=first.pk).points == 12
        assert Score.filter(points__gt=10).get().player == "ann"
        assert set(indexes("score")) == {"ix_score_player", "custom_points"}
        assert Score.create(player="cat", points=1).pk == 3
        assert db.get_connection().execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.plan_migrations() == []

    def test_rebuild_keeps_extra_columns_and_children(self, people):
        db.execute('ALTER TABLE "user" ADD COLUMN "nickname" TEXT')
        db.execute('UPDATE "user" SET nickname = \'ally\' WHERE username = \'alice\'')
        people.Comment.create(body="kept", todo=people.todos[0])

        class Todo(db.Model):
            title = db.Text(max_length=200)
            done = db.Bool(default=False)
            priority = db.Int(default=2)
            due = db.Date(null=True)
            notes = db.Text(null=True, blank=True)
            created = db.DateTime(auto_now_add=True)
            updated = db.DateTime(auto_now=True)
            data = db.JSON(default=dict)
            owner = db.ForeignKey("User", null=True, related_name="todos", on_delete="CASCADE")

            class Meta:
                table = "todos"

        class User(db.Model):
            username = db.Text(max_length=50, unique=True)
            score = db.Text()  # REAL -> TEXT forces a rebuild of the parent table
            active = db.Bool(default=True)

        results = db.migrate()
        kinds = {(op.table, op.kind, applied) for op, applied in results}
        assert ("todos", "rebuild_table", True) in kinds and ("user", "rebuild_table", True) in kinds
        assert ("user", "drop_column", False) in kinds
        assert db.query('SELECT nickname FROM "user" WHERE username = \'alice\'') == [{"nickname": "ally"}]
        assert Todo.count() == 4 and people.Comment.count() == 1
        fks = db.query('PRAGMA foreign_key_list("todos")')
        assert fks[0]["on_delete"] == "CASCADE"
        assert indexes("todos") == {"ix_todos_owner_id": False}
        assert db.plan_migrations()[0].kind == "drop_column"

        db.migrate(allow_destructive=True)
        assert "nickname" not in columns("user")
        assert db.plan_migrations() == []
        User.get(username="alice").delete()
        assert Todo.count() == 2 and people.Comment.count() == 0

    def test_nullability_change_fills_nulls(self):
        class Task(db.Model):
            label = db.Text(null=True)

        db.migrate()
        Task.create(label=None)
        Task.create(label="set")

        class Task(db.Model):  # noqa: F811
            label = db.Text(default="untitled")

        (op,) = db.plan_migrations()
        assert op.kind == "rebuild_table" and 'NOT NULL' in op.describe()
        db.migrate()
        assert list(Task.all().order_by("id").values_list("label", flat=True)) == ["untitled", "set"]
        assert columns("task")["label"]["notnull"] == 1

    def test_index_and_unique_changes(self):
        class Tag(db.Model):
            name = db.Text(index=True)
            slug = db.Text()

        db.migrate()

        class Tag(db.Model):  # noqa: F811
            name = db.Text()
            slug = db.Text(unique=True)

        plan = db.plan_migrations()
        assert [op.describe() for op in plan] == [
            'Drop index "ix_tag_name"',
            'Create unique index "ux_tag_slug" on "tag"."slug"',
        ]
        db.migrate()
        assert indexes("tag") == {"ux_tag_slug": True}
        Tag.create(name="a", slug="a")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO tag (name, slug) VALUES ('b', 'a')")

    def test_legacy_unique_constraint_is_removed_by_rebuild(self):
        db.execute('CREATE TABLE "code" ("id" INTEGER PRIMARY KEY AUTOINCREMENT, "value" TEXT NOT NULL UNIQUE)')

        class Code(db.Model):
            value = db.Text()

        (op,) = db.plan_migrations()
        assert op.kind == "rebuild_table" and "UNIQUE" in op.describe()
        db.migrate()
        Code.create(value="x")
        Code.create(value="x")
        assert db.plan_migrations() == []

    def test_add_required_foreign_key(self):
        class Owner(db.Model):
            name = db.Text()

        class Pet(db.Model):
            name = db.Text()

        db.migrate()

        class Pet(db.Model):  # noqa: F811
            name = db.Text()
            owner = db.ForeignKey(Owner)

        (op,) = db.plan_migrations()
        assert op.kind == "rebuild_table"  # empty table: safe to rebuild
        db.execute("INSERT INTO pet (name) VALUES ('rex')")
        with pytest.raises(db.MigrationError, match="already has rows"):
            db.plan_migrations()

    def test_drop_foreign_key_column_uses_rebuild(self, people):
        class Comment(db.Model):
            body = db.Text()

        (op,) = db.plan_migrations([Comment])
        assert op.kind == "drop_column" and op.destructive and op.rebuild
        db.migrate([Comment], allow_destructive=True)
        assert set(columns("comment")) == {"id", "body"}

    def test_unregistered_foreign_key_target(self):
        class Orphan(db.Model):
            parent = db.ForeignKey("Missing")

        with pytest.raises(db.MigrationError, match='"Missing"'):
            db.plan_migrations()

    def test_unmodelled_tables_are_ignored(self, app):
        db.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, entry TEXT)")
        assert db.plan_migrations() == []
        db.migrate(allow_destructive=True)
        assert db.query("SELECT name FROM sqlite_master WHERE name = 'audit_log'")

    def test_migrate_specific_models(self):
        class Alpha(db.Model):
            name = db.Text()

        class Beta(db.Model):
            name = db.Text()

        results = db.migrate([Alpha])
        assert [(op.table, applied) for op, applied in results] == [("alpha", True)]
        assert [op.table for op in db.plan_migrations()] == ["beta"]
