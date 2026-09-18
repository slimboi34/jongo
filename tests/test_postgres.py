"""PostgreSQL support.

The dialect's SQL translation is tested everywhere; the live-backend tests run when
``JONGO_TEST_DATABASE`` points at PostgreSQL:

    JONGO_TEST_DATABASE=postgres://localhost/jongo_test .venv/bin/python -m pytest
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from conftest import on_postgres, use_test_database
from jongo import db
from jongo.db import connection
from jongo.db.dialect import Dialect, PostgresDialect, for_backend, translate

postgres_only = pytest.mark.skipif(not on_postgres(), reason="needs a PostgreSQL database")


# -- translation (runs on any backend) ------------------------------------------------


class TestTranslation:
    def test_sqlite_is_the_identity(self):
        sql = 'SELECT * FROM "t" WHERE "a" = ? AND "b" LIKE \'%x%\''
        assert translate(sql, True, "?") == sql

    def test_placeholders_become_percent_s(self):
        assert translate('SELECT ? , ?', True, "%s") == "SELECT %s , %s"

    def test_question_mark_inside_a_string_literal_is_left_alone(self):
        sql = "SELECT * FROM t WHERE a = ? AND b = 'why? because'"
        assert translate(sql, True, "%s") == "SELECT * FROM t WHERE a = %s AND b = 'why? because'"

    def test_question_mark_inside_an_identifier_is_left_alone(self):
        sql = 'SELECT "why?" FROM t WHERE a = ?'
        assert translate(sql, True, "%s") == 'SELECT "why?" FROM t WHERE a = %s'

    def test_doubled_quotes_do_not_confuse_the_scanner(self):
        sql = "SELECT * FROM t WHERE a = 'it''s a ? here' AND b = ?"
        assert translate(sql, True, "%s") == "SELECT * FROM t WHERE a = 'it''s a ? here' AND b = %s"

    def test_percent_is_doubled_only_when_parameters_are_bound(self):
        sql = "SELECT * FROM t WHERE a LIKE '%x%' AND b = ?"
        assert translate(sql, True, "%s") == "SELECT * FROM t WHERE a LIKE '%%x%%' AND b = %s"
        assert translate("SELECT * FROM t WHERE a LIKE '%x%'", False, "%s") == \
            "SELECT * FROM t WHERE a LIKE '%x%'"


class TestDialects:
    def test_unknown_backend_is_rejected(self):
        with pytest.raises(ValueError, match="mysql"):
            for_backend("mysql")

    def test_limit_without_a_limit(self):
        assert Dialect().limit_offset(None, 5) == (" LIMIT ? OFFSET ?", [-1, 5])
        assert PostgresDialect().limit_offset(None, 5) == (" LIMIT ALL OFFSET ?", [5])
        assert PostgresDialect().limit_offset(3, 5) == (" LIMIT ? OFFSET ?", [3, 5])

    def test_not_equal_and_negation(self):
        assert Dialect().not_equal('"a"') == '"a" IS NOT ?'
        assert PostgresDialect().not_equal('"a"') == '"a" IS DISTINCT FROM ?'
        assert Dialect().negate('"a" = ?').endswith(", 0)")
        assert PostgresDialect().negate('"a" = ?').endswith(", false)")

    def test_case_sensitive_lookups_use_glob_or_like(self):
        sqlite_sql, sqlite_params = Dialect().text_condition('"a"', "contains", "x*y")
        assert "GLOB" in sqlite_sql and sqlite_params == ["*x[*]y*"]
        pg_sql, pg_params = PostgresDialect().text_condition('"a"', "contains", "x_y")  # LIKE needs _ escaped
        assert "LIKE" in pg_sql and "ILIKE" not in pg_sql and pg_params == ["%x\\_y%"]

    def test_case_insensitive_lookups_use_ilike_on_postgres(self):
        sql, params = PostgresDialect().text_condition('"a"', "istartswith", "Ann")
        assert "ILIKE" in sql and params == ["Ann%"]

    def test_types_map_both_ways(self):
        pg = PostgresDialect()
        assert pg.ddl_type("INTEGER") == "BIGINT" and pg.ddl_type("REAL") == "DOUBLE PRECISION"
        assert pg.normalize_type("bigint") == "INTEGER"
        assert pg.normalize_type("double precision") == "REAL"
        assert pg.normalize_type("character varying(50)") == "TEXT"
        assert Dialect().ddl_type("INTEGER") == "INTEGER"

    def test_identity_column_replaces_autoincrement(self):
        assert "AUTOINCREMENT" in Dialect().pk_ddl()
        assert "IDENTITY" in PostgresDialect().pk_ddl()

    def test_insert_returns_the_new_id_on_postgres(self):
        assert PostgresDialect().insert("INSERT INTO t (a) VALUES (?)").endswith('RETURNING "id"')
        assert Dialect().insert("INSERT INTO t (a) VALUES (?)") == "INSERT INTO t (a) VALUES (?)"


class TestUrls:
    def test_postgres_urls_select_the_postgres_backend(self):
        assert connection._parse_url("postgres://host/app")[0] == "postgres"
        assert connection._parse_url("postgresql://host/app")[0] == "postgres"

    def test_the_url_is_passed_to_the_driver_untouched(self):
        url = "postgresql://user:pw@host:5432/app?sslmode=require"
        assert connection._parse_url(url) == ("postgres", url)


# -- the live backend -----------------------------------------------------------------


@pytest.fixture
def pg():
    """A fresh PostgreSQL database with two related models."""
    use_test_database()
    saved = dict(db.models_registry)
    db.models_registry.clear()

    class Author(db.Model):
        name = db.Text(unique=True)
        rating = db.Float(default=0.0)

    class Book(db.Model):
        title = db.Text(max_length=200)
        published = db.Date(null=True)
        created = db.DateTime(auto_now_add=True)
        tags = db.JSON(default=list)
        shelved = db.Bool(default=False)
        author = db.ForeignKey(Author, null=True, on_delete="SET NULL")

    db.migrate()
    yield type("Models", (), {"Author": Author, "Book": Book})
    db.close_connections()
    db.models_registry.clear()
    db.models_registry.update(saved)


@postgres_only
class TestLiveBackend:
    def test_backend_is_reported(self, pg):
        assert db.backend() == "postgres"

    def test_insert_returns_the_generated_id(self, pg):
        first = pg.Author.create(name="ann")
        second = pg.Author.create(name="ben")
        assert first.pk == 1 and second.pk == 2

    def test_explicit_id_advances_the_sequence(self, pg):
        """GENERATED BY DEFAULT would otherwise hand out an id that already exists."""
        pg.Author(id=42, name="zed").save()
        assert pg.Author.create(name="next").pk == 43

    def test_every_value_type_round_trips(self, pg):
        book = pg.Book.create(
            title="Dune", published=date(1965, 8, 1), tags=["sf", "classic"], shelved=True,
        )
        fresh = pg.Book.get(id=book.pk)
        assert fresh.published == date(1965, 8, 1)
        assert fresh.tags == ["sf", "classic"]
        assert fresh.shelved is True
        assert isinstance(fresh.created, datetime)

    def test_case_insensitive_lookups(self, pg):
        pg.Author.create(name="Ann")
        assert pg.Author.filter(name__icontains="nn").count() == 1
        assert pg.Author.filter(name__istartswith="a").count() == 1
        assert pg.Author.filter(name__contains="nn").count() == 1
        assert pg.Author.filter(name__contains="NN").count() == 0  # case-sensitive

    def test_like_wildcards_in_the_value_are_escaped(self, pg):
        pg.Author.create(name="100% sure")
        pg.Author.create(name="100x sure")
        assert pg.Author.filter(name__contains="100%").count() == 1

    def test_unique_violation_becomes_a_validation_error(self, pg):
        pg.Author.create(name="ann")
        with pytest.raises(db.ValidationError) as caught:
            pg.Author.objects.create(name="ann")
        assert "name" in caught.value.errors

    def test_foreign_key_is_enforced(self, pg):
        with pytest.raises(db.ValidationError):
            pg.Book.create(title="Orphan", author_id=999)

    def test_on_delete_set_null(self, pg):
        author = pg.Author.create(name="ann")
        book = pg.Book.create(title="Dune", author=author)
        author.delete()
        assert pg.Book.get(id=book.pk).author_id is None

    def test_transactions_roll_back(self, pg):
        pg.Author.create(name="ann")
        with pytest.raises(RuntimeError):
            with db.transaction():
                pg.Author.create(name="ben")
                raise RuntimeError("boom")
        assert pg.Author.count() == 1

    def test_columns_change_in_place_without_a_rebuild(self, pg):
        pg.Author.create(name="ann", rating=4.5)
        db.models_registry.pop("author", None)

        class Author(db.Model):  # noqa: F811
            name = db.Text(unique=True)
            rating = db.Text()  # REAL -> TEXT

        plan = db.plan_migrations([Author])
        assert [op.kind for op in plan] == ["alter_column"]
        assert not plan[0].rebuild and "ALTER COLUMN" in plan[0].sql[0]
        db.migrate([Author])
        assert Author.get(name="ann").rating == "4.5"

    def test_a_huge_in_list_is_inlined_safely(self, pg):
        """The inlined-literal path has to survive placeholder translation."""
        pg.Author.create(name="ann")
        names = [f"n{i}" for i in range(3000)] + ["ann", "it's a ? trap", "100%"]
        assert pg.Author.filter(name__in=names).count() == 1

    def test_ordering_and_slicing(self, pg):
        for index, name in enumerate(["c", "a", "b"]):
            pg.Author.create(name=name, rating=float(index))
        assert list(pg.Author.all().order_by("name").values_list("name", flat=True)) == ["a", "b", "c"]
        assert list(pg.Author.all().order_by("-rating")[1:].values_list("name", flat=True)) == ["a", "c"]
