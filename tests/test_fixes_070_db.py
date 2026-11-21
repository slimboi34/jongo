"""Regression tests for 0.7.0: ORM fields, queries and migrations (stress-test findings).

Every test runs against a fresh database (in-memory SQLite by default, or the
``JONGO_TEST_DATABASE`` PostgreSQL server) with an empty model registry.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

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


# ------------------------------------------------------------- db.JSON rejects NaN/Infinity ---


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_json_field_rejects_nan_and_infinity(bad):
    class Reading(db.Model):
        data = db.JSON(null=True)

    db.migrate()
    with pytest.raises(db.ValidationError) as caught:
        Reading.create(data={"value": bad, "nested": [1, 2]})
    assert caught.value.code == "J304"
    assert caught.value.errors == {"data": "Enter finite numbers only (not NaN or infinity)."}
    assert Reading.count() == 0

    good = Reading.create(data={"value": 1.5})
    with pytest.raises(db.ValidationError, match="finite numbers"):
        Reading.filter(id=good.pk).update(data=[bad])
    with pytest.raises(db.ValidationError, match="finite numbers"):  # a JSON string with NaN tokens too
        db.JSON().clean('{"value": NaN}')
    assert Reading.get(id=good.pk).data == {"value": 1.5}


def test_json_field_still_reports_unserialisable_values():
    field = db.JSON()
    with pytest.raises(db.ValidationError, match="not JSON serialisable"):
        field.clean({"when": object()})
    circular: list = []
    circular.append(circular)
    with pytest.raises(db.ValidationError, match="not JSON serialisable"):
        field.clean(circular)
    assert field.clean('{"a": [1, 2.5, null]}') == {"a": [1, 2.5, None]}


@sqlite_only
def test_stored_json_is_strictly_valid():
    class Reading(db.Model):
        data = db.JSON(null=True)

    db.migrate()
    Reading.create(data={"value": 1e308, "list": [-0.0, 3]})
    row = connection.get_connection().execute(f'SELECT json_valid(data) FROM "{Reading._meta.table}"').fetchone()
    assert row[0] == 1


# ------------------------------------------------------- a NUL in a text lookup term ---

PATTERN_LOOKUPS = ["contains", "icontains", "startswith", "istartswith", "endswith", "iendswith", "iexact"]


@pytest.mark.parametrize("lookup", PATTERN_LOOKUPS)
@pytest.mark.parametrize("term", ["\x00", "a\x00", "a\x00zzz", "\x00b"])
def test_a_nul_in_a_pattern_lookup_matches_nothing(lookup, term):
    class Doc(db.Model):
        title = db.Text()

    db.migrate()
    for title in ("alpha", "beta", "gamma"):
        Doc.create(title=title)
    assert Doc.filter(**{f"title__{lookup}": term}).count() == 0
    assert Doc.exclude(**{f"title__{lookup}": term}).count() == 3
    assert Doc.filter(db.Q(**{f"title__{lookup}": term}) | db.Q(title="beta")).count() == 1


def test_nul_terms_leave_search_and_ordinary_lookups_alone():
    class Doc(db.Model):
        title = db.Text()

    db.migrate()
    for title in ("alpha", "beta"):
        Doc.create(title=title)
    assert Doc.search("a\x00zzz", "title").count() == 0
    assert Doc.filter(title__contains="a").count() == 2
    assert Doc.filter(title__startswith="al").count() == 1


@sqlite_only  # PostgreSQL can't store a NUL in text at all
def test_exact_still_finds_a_stored_nul_on_sqlite():
    class Doc(db.Model):
        title = db.Text()

    db.migrate()
    Doc.create(title="a\x00b")
    Doc.create(title="a")
    assert [d.title for d in Doc.filter(title="a\x00b")] == ["a\x00b"]
    assert Doc.filter(title__startswith="a\x00zzz").count() == 0  # used to match "a\x00b"
    assert Doc.filter(title__iexact="a\x00").count() == 0  # used to match "a\x00b"


# ------------------------------------ a NOT NULL column left over from an earlier schema ---


def test_insert_failing_on_a_leftover_not_null_column_names_it():
    class User(db.Model):
        fullname = db.Text()
        age = db.Int()

    db.migrate()
    User.create(fullname="a", age=3)

    class User(db.Model):  # noqa: F811 -- fullname renamed to name
        name = db.Text(default="")
        age = db.Int()

    results = db.migrate()  # adds "name"; keeps "fullname" because dropping it is destructive
    assert [(op.kind, applied) for op, applied in results] == [("add_column", True), ("drop_column", False)]
    with pytest.raises(db.ValidationError) as caught:
        User.create(name="b", age=4)
    error = caught.value
    assert error.code == "J304" and list(error.errors) == ["__all__"]
    message = error.errors["__all__"]
    assert 'Column "fullname" is NOT NULL in the database but is not a field on User' in message
    assert "jongo migrate --allow-destructive" in message
    assert "allow-destructive" in error.hint

    db.migrate(allow_destructive=True)
    assert User.create(name="b", age=4).pk == 2


def test_a_missing_value_for_a_real_field_still_says_required():
    from jongo.db.query import integrity_to_validation

    class Note(db.Model):
        body = db.Text()

    db.migrate()
    with pytest.raises(connection.dialect().integrity_errors()) as caught:
        db.execute(f'INSERT INTO "{Note._meta.table}" ("body") VALUES (NULL)')
    assert integrity_to_validation(caught.value, Note).errors == {"body": "This field is required."}


# ------------------------------------------ DateTime/Date lookups given ISO strings ---

UTC = timezone.utc
NOON = datetime(2024, 3, 10, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def events():
    class Event(db.Model):
        label = db.Text()
        when = db.DateTime(null=True)

    db.migrate()
    for label, when in [("before", NOON - timedelta(seconds=1)), ("equal", NOON),
                        ("half-second", NOON + timedelta(milliseconds=500)), ("hour", NOON + timedelta(hours=1))]:
        Event.create(label=label, when=when)
    Event.create(label="unset", when=None)
    return Event


def labels(queryset):
    return sorted(event.label for event in queryset)


# The same instant as NOON, spelled the ways a query parameter or a JSON body spells it.
NOON_STRINGS = [
    "2024-03-10T12:00:00Z",
    "2024-03-10T12:00:00z",
    "2024-03-10T12:00:00+00:00",
    "2024-03-10T17:00:00+05:00",
    "2024-03-10T07:00:00-05:00",
    "2024-03-10 17:00:00+05:00",
    "2024-03-10T12:00:00.000Z",
    "2024-03-10T12:00:00.000000+00:00",
    " 2024-03-10T12:00:00Z ",
]


@pytest.mark.parametrize("text", NOON_STRINGS)
@pytest.mark.parametrize("lookup", ["exact", "ne", "gt", "gte", "lt", "lte"])
def test_datetime_lookup_with_an_iso_string_matches_the_datetime(events, text, lookup):
    key = "when" if lookup == "exact" else f"when__{lookup}"
    assert labels(events.filter(**{key: text})) == labels(events.filter(**{key: NOON}))


@pytest.mark.parametrize("text", NOON_STRINGS)
def test_datetime_range_and_in_with_iso_strings(events, text):
    assert labels(events.filter(when__gte=text, when__lt="2024-03-10T13:00:00Z")) == ["equal", "half-second"]
    assert labels(events.filter(when__in=[text, "2024-03-10T18:00:00+05:00"])) == ["equal", "hour"]
    assert labels(events.exclude(when=text)) == ["before", "half-second", "hour", "unset"]


def test_expected_rows_for_the_reported_strings(events):
    assert labels(events.filter(when="2024-03-10T12:00:00Z")) == ["equal"]
    assert labels(events.filter(when__lt="2024-03-10T12:00:00Z")) == ["before"]
    assert labels(events.filter(when__gt="2024-03-10T17:00:00+05:00")) == ["half-second", "hour"]
    assert labels(events.filter(when__lte="2024-03-10T17:00:00+05:00")) == ["before", "equal"]


def test_naive_strings_compare_like_naive_datetimes():
    # A naive value stays naive (it round-trips to an equal naive datetime), so a naive string
    # behaves exactly like the naive datetime it names: against naive rows it matches exactly.
    class Shift(db.Model):
        label = db.Text()
        start = db.DateTime()

    db.migrate()
    base = datetime(2024, 3, 10, 12, 0, 0)
    for label, offset in [("early", -1), ("noon", 0), ("late", 3600)]:
        Shift.create(label=label, start=base + timedelta(seconds=offset))
    for text in ("2024-03-10 12:00:00", "2024-03-10T12:00:00", "2024-03-10T12:00", "2024-03-10T12:00:00.000000"):
        for lookup in ("exact", "gt", "gte", "lt", "lte"):
            key = "start" if lookup == "exact" else f"start__{lookup}"
            assert labels(Shift.filter(**{key: text})) == labels(Shift.filter(**{key: base})), (text, lookup)
    assert labels(Shift.filter(start="2024-03-10 12:00:00")) == ["noon"]
    assert labels(Shift.filter(start__gte="2024-03-10")) == ["early", "late", "noon"]  # a date is midnight
    assert labels(Shift.filter(start__lt="2024-03-11")) == ["early", "late", "noon"]


def test_unparseable_datetime_strings_are_still_compared_as_text(events):
    assert labels(events.filter(when__startswith="2024-03-10T12:00:00.5")) == ["half-second"]
    assert labels(events.filter(when__contains="T13:")) == ["hour"]
    assert events.filter(when="not a date").count() == 0
    assert labels(events.filter(when__gt="2024")) == ["before", "equal", "half-second", "hour"]


def test_date_lookups_accept_full_datetime_strings():
    class Entry(db.Model):
        day = db.Date()

    db.migrate()
    for day in (date(2024, 3, 9), date(2024, 3, 10), date(2024, 3, 11)):
        Entry.create(day=day)

    def days(qs):
        return sorted(entry.day.isoformat() for entry in qs)

    assert days(Entry.filter(day="2024-03-10")) == ["2024-03-10"]
    assert days(Entry.filter(day=" 2024-03-10 ")) == ["2024-03-10"]
    assert days(Entry.filter(day="2024-03-10T08:30:00")) == ["2024-03-10"]
    assert days(Entry.filter(day="2024-03-10T23:30:00-05:00")) == ["2024-03-11"]  # its UTC date
    assert days(Entry.filter(day="2024-03-10T00:30:00+05:00")) == ["2024-03-09"]
    assert days(Entry.filter(day__gte="2024-03-10T00:00:00Z")) == ["2024-03-10", "2024-03-11"]
    assert days(Entry.filter(day__lt="2024-03-10 12:00:00")) == ["2024-03-09"]
    assert days(Entry.filter(day__in=["2024-03-09T10:00:00Z", "2024-03-11"])) == ["2024-03-09", "2024-03-11"]
    assert Entry.filter(day="nonsense").count() == 0


def test_datetime_default_given_as_a_string_fills_existing_rows_normalised():
    class Task(db.Model):
        title = db.Text()

    db.migrate()
    Task.create(title="old")

    class Task(db.Model):  # noqa: F811
        title = db.Text()
        due = db.DateTime(default="2024-03-10T17:00:00+05:00")

    db.migrate()
    assert Task.get(title="old").due == NOON
    assert Task.filter(due=NOON).count() == 1


# ------------------------------------------------------- integers beyond 64 bits ---

BIG = 2**63  # one past the largest INTEGER (BIGINT on PostgreSQL)


@pytest.fixture
def numbers():
    class Owner(db.Model):
        name = db.Text()

    class Num(db.Model):
        n = db.Int(null=True)
        owner = db.ForeignKey(Owner, null=True)

    db.migrate()
    owner = Owner.create(name="o")
    for value in (-5, 0, 7, 2**63 - 1, -(2**63), None):
        Num.create(n=value, owner=owner if value == 7 else None)
    return SimpleNamespace(Num=Num, Owner=Owner, owner=owner)


def test_int_field_rejects_values_beyond_64_bits_with_j304(numbers):
    Num = numbers.Num
    for value in (BIG, -BIG - 1, 2**70, "9223372036854775808", 1e19):
        with pytest.raises(db.ValidationError) as caught:
            Num.create(n=value)
        assert caught.value.code == "J304"
        assert caught.value.errors == {"n": "Enter a whole number between -9223372036854775808 and "
                                            "9223372036854775807."}
    with pytest.raises(db.ValidationError, match="between"):
        Num.all().update(n=2**70)
    with pytest.raises(db.ValidationError, match="between"):
        db.Int().clean("99999999999999999999")
    assert Num.create(n=2**63 - 1).n == 2**63 - 1 and Num.create(n=-(2**63)).n == -(2**63)


def test_foreign_key_and_explicit_id_beyond_64_bits_are_validation_errors(numbers):
    with pytest.raises(db.ValidationError) as caught:
        numbers.Num.create(owner_id=BIG)
    assert list(caught.value.errors) == ["owner"] and "out of range" in caught.value.errors["owner"]
    with pytest.raises(db.ValidationError) as caught:
        numbers.Num(id=BIG, n=1).save()
    assert caught.value.errors == {"id": "Enter a whole number between -9223372036854775808 and "
                                         "9223372036854775807."}
    field = numbers.Num._meta.field("owner")
    with pytest.raises(db.ValidationError, match="out of range"):
        field.clean(str(BIG))


def test_lookups_with_out_of_range_ints_match_like_python_would(numbers):
    Num = numbers.Num

    def values(**lookups):
        return sorted((num.n for num in Num.filter(**lookups)), key=lambda v: (v is None, v or 0))

    stored = [-(2**63), -5, 0, 7, 2**63 - 1]
    for big in (BIG, 2**70, float(2**64)):
        assert values(n=big) == [] and values(id=big) == [] and values(pk=big) == []
        assert values(n__gt=big) == [] and values(n__gte=big) == []
        assert values(n__lt=big) == stored and values(n__lte=big) == stored
        assert values(n__gt=-big - 1) == stored and values(n__gte=-big - 1) == stored
        assert values(n__lt=-big - 1) == [] and values(n__lte=-big - 1) == []
        assert values(n__ne=big) == [*stored, None]  # ne includes NULL, as ne=<any value> does
        assert values(n__in=[big]) == [] and values(n__in=[big, 7]) == [7]
        assert values(owner=big) == [] and values(owner__id=big) == []
        assert Num.exclude(n=big).count() == 6
    with pytest.raises(Num.DoesNotExist):
        Num.get(id=BIG)
    with pytest.raises(numbers.Owner.DoesNotExist):
        numbers.Owner.get(pk=-BIG - 1)
    assert Num.filter(id=BIG).update(n=1) == 0 and Num.filter(id=BIG).delete() == 0


def test_impossible_in_candidates_do_not_match_null_rows(numbers):
    Num = numbers.Num
    assert Num.filter(n__in=["abc"]).count() == 0  # used to compile to "n IS NULL"
    assert [num.n for num in Num.filter(n__in=["abc", 7])] == [7]
    assert [num.n for num in Num.filter(n__in=[None])] == [None]
    assert sorted(Num.filter(n__in=[None, 0]).values_list("n", flat=True), key=lambda v: v is None) == [0, None]


def test_float_field_reports_ints_too_large_for_a_float():
    class Measure(db.Model):
        x = db.Float(null=True)

    db.migrate()
    with pytest.raises(db.ValidationError, match="finite number"):
        Measure.create(x=10**400)
    with pytest.raises(db.ValidationError, match="finite number"):
        Measure.filter(x=10**400).count()
    assert Measure.create(x=2**70).x == float(2**70)
