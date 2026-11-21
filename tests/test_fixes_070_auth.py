"""0.7.0 fixes to auth, rate limiting, @server argument checking and the OpenAPI body.

Each section is one confirmed finding from the 0.6.0 stress test:

1. A look-alike account ("Ann") could clear the login throttle of "ann".
2. rate_limit= counted a signed-in caller by user only, so a sign-up loop never got a 429.
3. @server arguments typed date/datetime/time/UUID/Decimal/bytes/Enum arrived unchecked.
4. User.create(password=...) and assign-then-save stored the raw password.
5. The OpenAPI body required `kwargs`, but the browser sends `args`.
6. Filling the shared limiter with fresh keys evicted a throttled caller's count.
7. login()/logout() kept the CSRF token minted before sign-in.
8. A user with no password answered a wrong guess ~1000x faster than an unknown username.
9. Two processes signing in at once after an upgrade raced to create jongo_sessions.
"""
from __future__ import annotations

import base64
import collections.abc
import dataclasses
import datetime
import decimal
import enum
import importlib.util
import inspect
import os
import re
import subprocess
import sys
import textwrap
import time
import typing
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal, NewType, Optional, TypedDict, Union

import pytest

from conftest import sqlite_only, use_test_database
from jongo import HTTPError, Jongo, JongoError, auth, db, forms, json_schema, ratelimit, rpc, server
from jongo.http import CSRF_COOKIE, Request
from jongo.ratelimit import Limiter, RateLimit, TooManyRequests
from jongo.rpc import coerce
from jongo.schema import function_schema, openapi
from jongo.testing import TestClient

HERE = Path(__file__).parent
ROOT = HERE.parent


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    saved_models = dict(db.models_registry)
    saved_functions = dict(rpc.SERVER_FUNCTIONS)
    db.models_registry.clear()
    db.models_registry.update({"User": auth.User, "LoginSession": auth.LoginSession})
    rpc.SERVER_FUNCTIONS.clear()
    use_test_database()
    monkeypatch.setattr(auth, "ITERATIONS", 1000)
    ratelimit.limiter.reset()
    auth.limiter.reset()
    yield
    ratelimit.limiter.reset()
    auth.limiter.reset()
    db.close_connections()
    db.models_registry.clear()
    db.models_registry.update(saved_models)
    rpc.SERVER_FUNCTIONS.clear()
    rpc.SERVER_FUNCTIONS.update(saved_functions)


def make_app(**options):
    app = Jongo("fixes_070_auth", root=HERE, secret_key="k" * 40, **options)
    db.migrate()
    return app


def post(client, fn, *args, headers=None, **kwargs):
    """A raw server call: the response, without TestClient.rpc turning errors into exceptions."""
    return client.post(f"/_jongo/rpc/{fn.id}", json={"args": list(args), "kwargs": kwargs}, headers=headers)


# ============================================================ 1. exact login-throttle key ==


def test_a_look_alike_account_cannot_clear_another_accounts_login_throttle():
    """9 wrong guesses on ann, a correct login as Ann, then 2 more guesses on ann: the 2nd is a 429."""
    app = make_app()

    @server
    def sign_in(request, username: str, password: str) -> bool:
        user = auth.authenticate(username, password, request)
        if user is None:
            raise HTTPError(400, "Wrong username or password")
        auth.login(request, user)
        return True

    auth.User.create_user("ann", "victim-secret")
    auth.User.create_user("Ann", "attacker-pw")          # the lookup is exact, so this is another account
    client = TestClient(app)
    assert [post(client, sign_in, "ann", f"guess{i}").status for i in range(9)] == [400] * 9
    assert client.rpc(sign_in, "Ann", "attacker-pw") is True
    client.logout()
    assert post(client, sign_in, "ann", "guess9").status == 400
    refused = post(client, sign_in, "ann", "guess10")
    assert refused.status == 429 and refused.json()["error"]["code"] == "J213", \
        "Ann's correct password must not have reset ann's count"


def test_the_throttle_key_is_the_exact_username(monkeypatch):
    monkeypatch.setattr(auth, "LOGIN_ATTEMPTS", 3)
    db.migrate()
    auth.User.create_user("ann", "right")
    auth.User.create_user("Ann", "other")
    assert [auth.authenticate("ann", g) for g in "abc"] == [None] * 3
    with pytest.raises(TooManyRequests):
        auth.authenticate("ann", "right")
    assert auth.authenticate("Ann", "other").username == "Ann", "a different exact name has its own count"
    with pytest.raises(TooManyRequests):
        auth.authenticate("ann", "right")                 # ...and Ann signing in didn't clear ann's
    assert auth.authenticate("ANN", "right") is None, "a variant spelling is not ann's account"


def test_a_correct_password_clears_only_its_own_count(monkeypatch):
    monkeypatch.setattr(auth, "LOGIN_ATTEMPTS", 3)
    db.migrate()
    auth.User.create_user("ann", "right")
    auth.User.create_user("Ann", "other")
    auth.authenticate("ann", "x"), auth.authenticate("ann", "y")
    auth.authenticate("Ann", "x"), auth.authenticate("Ann", "y")
    assert auth.authenticate("ann", "right").username == "ann"
    assert [auth.authenticate("ann", g) for g in "abc"] == [None] * 3, "ann's count started again"
    assert auth.authenticate("Ann", "z") is None
    with pytest.raises(TooManyRequests):
        auth.authenticate("Ann", "other")                 # Ann's three failures still stand


# ================================================= 2. rate limits count address and user ==


def test_a_sign_up_loop_from_one_address_is_refused_on_the_sixth_call():
    app = make_app()
    created = []

    @server(rate_limit="5/minute")
    def sign_up(request, username: str) -> int:
        user = auth.User.create_user(username, "pw")
        auth.login(request, user)                         # each call is signed in as the newest account
        created.append(user.id)
        return user.id

    client = TestClient(app)
    assert [post(client, sign_up, f"user{i}").status for i in range(5)] == [200] * 5
    refused = post(client, sign_up, "user5")
    assert refused.status == 429 and refused.json()["error"]["code"] == "J213"
    assert len(created) == 5, "the refused call did not run"


def test_a_refused_request_is_not_charged_to_the_other_identity():
    app = make_app()

    @server(rate_limit="2/minute")
    def ping(request) -> str:
        return "pong"

    ann = auth.User.create_user("ann", "pw")
    anonymous, signed_in = TestClient(app), TestClient(app)
    signed_in.login(ann)
    assert [post(anonymous, ping).status for _ in range(2)] == [200, 200]   # the address is now full
    assert post(signed_in, ping).status == 429, "the same address, so the same address counter"
    user_key = f"@server {ping.id}|user:{ann.id}"
    assert ratelimit.limiter.peek(user_key, RateLimit(1, 60)) == (True, 0), "ann was refunded"


def test_the_same_user_from_another_address_is_still_counted_per_user():
    app = make_app(trust_proxy=True)

    @server(rate_limit="2/minute")
    def ping(request) -> str:
        return "pong"

    ann = auth.User.create_user("ann", "pw")
    client = TestClient(app)
    client.login(ann)
    statuses = [post(client, ping, headers={"X-Forwarded-For": f"10.0.0.{i}"}).status for i in range(3)]
    assert statuses == [200, 200, 429], "switching address doesn't reset the user's window"


def test_address_factor_loosens_or_turns_off_the_address_side(monkeypatch):
    app = make_app()

    @server(rate_limit="2/minute")
    def ping(request) -> str:
        return "pong"

    users = [auth.User.create_user(f"user{i}", "pw") for i in range(4)]
    clients = []
    for user in users:
        clients.append(TestClient(app))
        clients[-1].login(user)

    monkeypatch.setattr(ratelimit, "ADDRESS_FACTOR", 3)  # an office: the address may make 6 calls
    statuses = [post(c, ping).status for c in clients for _ in range(2)]
    assert statuses == [200] * 6 + [429, 429]
    ratelimit.limiter.reset()
    monkeypatch.setattr(ratelimit, "ADDRESS_FACTOR", None)  # 0.6 behaviour: users only
    assert [post(c, ping).status for c in clients for _ in range(2)] == [200] * 8


def test_client_keys_and_an_explicit_key():
    class Req:
        client_ip = "203.0.113.9"
        user = None

    request = Req()
    assert ratelimit.client_keys(request) == ["ip:203.0.113.9"]
    assert ratelimit.client_key(request) == "ip:203.0.113.9"
    request.user = type("U", (), {"id": 42})()
    assert ratelimit.client_keys(request) == ["ip:203.0.113.9", "user:42"]
    assert ratelimit.client_key(request) == "user:42", "unchanged, for code that reads it"

    for _ in range(2):
        ratelimit.check(request, "2/minute", "reset", key="ann@example.com")
    with pytest.raises(TooManyRequests):
        ratelimit.check(request, "2/minute", "reset", key="ann@example.com")
    assert ratelimit.limiter.peek("reset|ip:203.0.113.9", RateLimit(1, 60)) == (True, 0), \
        "with key= only that key is counted"


# =============================================== 3. typed @server arguments are checked ==


class Plan(enum.Enum):
    FREE = "free"
    TEAM = "team"


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 2


class Address(TypedDict):
    street: str
    number: int


class PartialAddress(TypedDict, total=False):
    street: str


UserId = NewType("UserId", int)


@dataclasses.dataclass
class Slot:
    day: datetime.date
    plan: Plan = Plan.FREE


class Unknown:
    """A class Jongo has no rule for."""


@dataclasses.dataclass
class Tree:
    label: str
    children: list[Tree] = dataclasses.field(default_factory=list)


def register(name, **annotations):
    """@server on a function whose annotations are real objects (this module's are strings)."""
    def fn(request, **kwargs):
        return None

    params = [inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    params += [inspect.Parameter(key, inspect.Parameter.POSITIONAL_OR_KEYWORD) for key in annotations]
    fn.__signature__ = inspect.Signature(params)
    fn.__annotations__ = dict(annotations)
    fn.__name__ = fn.__qualname__ = name
    return server(fn)


def test_dates_times_uuids_decimals_bytes_and_enums_arrive_converted():
    app = make_app()
    seen = {}

    @server
    def typed(request, day: datetime.date, at: datetime.datetime, alarm: datetime.time, key: uuid.UUID,
              amount: decimal.Decimal, blob: bytes, plan: Plan, level: Level) -> bool:
        seen.update(day=day, at=at, alarm=alarm, key=key, amount=amount, blob=blob, plan=plan, level=level)
        return True

    client = TestClient(app)
    key = "12345678-1234-5678-1234-567812345678"
    assert client.rpc(typed, "2026-11-21", "2026-11-21T09:30:00.123Z", "07:15", key, "19.99",
                      base64.b64encode(b"\x00hi").decode(), "team", 2) is True
    assert seen == {
        "day": datetime.date(2026, 11, 21),
        "at": datetime.datetime(2026, 11, 21, 9, 30, 0, 123000, tzinfo=datetime.timezone.utc),
        "alarm": datetime.time(7, 15), "key": uuid.UUID(key), "amount": decimal.Decimal("19.99"),
        "blob": b"\x00hi", "plan": Plan.TEAM, "level": Level.HIGH,
    }
    assert type(seen["at"]) is datetime.datetime and type(seen["day"]) is datetime.date


@pytest.mark.parametrize("hint, bad", [
    (datetime.date, {"evil": True}), (datetime.date, "2026-13-45"), (datetime.date, "21/11/2026"),
    (datetime.date, "2026-11-21T09:00:00"), (datetime.date, 20261121),
    (datetime.datetime, "2026-11-21"), (datetime.datetime, "yesterday"), (datetime.datetime, 1700000000),
    (datetime.datetime, "2026-11-21T25:00:00Z"),
    (datetime.time, "25:00"), (datetime.time, "noon"), (datetime.time, 9),
    (uuid.UUID, [1, 2, 3]), (uuid.UUID, "not-a-uuid"), (uuid.UUID, 12345),
    (decimal.Decimal, "abc"), (decimal.Decimal, "NaN"), (decimal.Decimal, "Infinity"), (decimal.Decimal, True),
    (decimal.Decimal, float("nan")), (decimal.Decimal, float("inf")), (decimal.Decimal, [5]),
    (decimal.Decimal, "1e99999"), (decimal.Decimal, "1_000"),
    (bytes, "not base64!"), (bytes, "aGk"), (bytes, 5), (bytes, ["aGk="]),
    (Plan, "enterprise"), (Plan, "FREE"), (Plan, None), (Level, True), (Level, 1.0), (Level, "1"),
])
def test_a_value_of_the_wrong_shape_is_a_400_j403(hint, bad):
    with pytest.raises(HTTPError) as caught:
        coerce(bad, hint, "v")
    assert caught.value.status == 400 and caught.value.code == "J403"
    assert caught.value.hint


def test_iso_8601_variants_are_read_on_every_python():
    utc = datetime.timezone.utc
    assert coerce("2026-11-21T09:30:00Z", datetime.datetime) == datetime.datetime(2026, 11, 21, 9, 30, tzinfo=utc)
    assert coerce("2026-11-21t09:30z", datetime.datetime) == datetime.datetime(2026, 11, 21, 9, 30, tzinfo=utc)
    assert coerce("2026-11-21 09:30:00", datetime.datetime) == datetime.datetime(2026, 11, 21, 9, 30)
    plus = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    assert coerce("2026-11-21T09:30:00.123456789+0530", datetime.datetime) == \
        datetime.datetime(2026, 11, 21, 9, 30, 0, 123456, tzinfo=plus)
    assert coerce("09:30:00.5Z", datetime.time) == datetime.time(9, 30, 0, 500000, tzinfo=utc)
    assert coerce("2026-11-21", datetime.date) == datetime.date(2026, 11, 21)


def test_decimals_come_from_text_or_numbers_without_binary_noise():
    assert coerce(0.1, decimal.Decimal) == decimal.Decimal("0.1")
    assert coerce(7, decimal.Decimal) == decimal.Decimal(7)
    assert coerce(" 1.50 ", decimal.Decimal) == decimal.Decimal("1.50")
    assert coerce("-2.5e3", decimal.Decimal) == decimal.Decimal("-2.5e3")
    assert coerce("1e4300", decimal.Decimal) == decimal.Decimal("1e4300"), "the bound itself is allowed"


def test_unions_prefer_the_exact_type_before_converting():
    assert coerce("007", int | str) == "007"
    assert coerce(7, int | str) == 7
    assert coerce(2.0, int | float) == 2.0 and type(coerce(2.0, int | float)) is float
    assert coerce(True, int | bool) is True
    assert coerce("1.5", float | str) == "1.5"
    assert coerce("2026-11-21", Optional[datetime.date]) == datetime.date(2026, 11, 21)
    assert coerce(None, Optional[datetime.date]) is None
    assert coerce("2026-11-21", Union[datetime.date, str]) == "2026-11-21", "text is exactly a str"
    assert coerce("2026-11-21", Union[int, datetime.date]) == datetime.date(2026, 11, 21)
    with pytest.raises(HTTPError):
        coerce(2**63, int | str)


def test_typeddicts_newtypes_annotated_and_abstract_containers_are_checked():
    assert coerce({"street": "Main", "number": "12", "extra": 1}, Address) == {"street": "Main", "number": 12}
    with pytest.raises(HTTPError) as caught:
        coerce({"street": "Main"}, Address, "home")
    assert "'number'" in caught.value.message
    with pytest.raises(HTTPError):
        coerce({"street": "Main", "number": "twelve"}, Address)
    assert coerce({}, PartialAddress) == {}
    assert coerce("5", UserId) == 5
    assert coerce("2026-11-21", Annotated[datetime.date, "when"]) == datetime.date(2026, 11, 21)
    assert coerce([1, "2"], collections.abc.MutableSequence[int]) == [1, 2]
    assert coerce([1, 1], collections.abc.Set[int]) == frozenset({1})
    assert coerce({"a": "1"}, collections.abc.MutableMapping[str, int]) == {"a": 1}
    assert coerce({"day": "2026-11-21"}, Slot) == Slot(datetime.date(2026, 11, 21))


def test_a_float_past_floats_range_is_a_400_not_a_500():
    with pytest.raises(HTTPError) as caught:
        coerce(10**400, float, "x")
    assert caught.value.code == "J403"


@dataclasses.dataclass
class HasUnknown:
    thing: Unknown


def test_a_hint_jongo_cannot_check_is_refused_when_the_function_is_defined():
    for hint in (Unknown, list[Unknown], dict[str, Unknown], Optional[Unknown], typing.Callable[[], int],
                 type[int], tuple[int, Unknown], HasUnknown, set[HasUnknown]):
        with pytest.raises(JongoError) as caught:
            register("handler", thing=hint)
        assert caught.value.code == "J405", hint
        assert "can't check" in str(caught.value) and "thing" in str(caught.value) and caught.value.hint
    assert not rpc.SERVER_FUNCTIONS, "a refused function is never registered"


def test_a_refused_hint_names_the_part_that_cannot_be_checked():
    with pytest.raises(JongoError) as caught:
        register("handler", thing=dict[str, list[Unknown]])
    assert "Unknown" in str(caught.value).split("(in")[0]


def test_hints_that_already_worked_are_still_accepted():
    class Gadget(db.Model):
        name = db.Text(max_length=40)

    Order070 = forms.Schema("Order070", qty=forms.Int(min=1))
    assert Order070

    @server
    def fine(request, a: int, b: str = "", c: Optional[float] = None, d: list[dict[str, int]] = (),
             e: Literal["x", "y"] = "x", f: Any = None, g: Gadget | None = None, h: Order070 = None,
             i: Slot | None = None, j: Address | None = None, k: UserId = 0, n: Tree | None = None,
             *rest: datetime.date, **more: decimal.Decimal) -> Unknown:   # the return hint is never checked
        return None

    @server
    def untyped(request, anything, other=None):
        return anything

    assert fine.hints["a"] is int and untyped.hints == {}


def test_a_forward_reference_is_checked_when_it_resolves(tmp_path):
    """Names defined further down the module are checked on first use, not at decoration."""
    source = textwrap.dedent('''
        from __future__ import annotations
        from jongo import server

        @server
        def later(request, thing: Thing) -> None: ...

        @server
        def fine_later(request, thing: Point) -> int:
            return thing.x

        class Thing:
            pass

        import dataclasses

        @dataclasses.dataclass
        class Point:
            x: int
    ''')
    path = tmp_path / "forward_mod_070.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location("forward_mod_070", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["forward_mod_070"] = module
    try:
        spec.loader.exec_module(module)                    # nothing raised: Thing didn't exist yet
        assert module.fine_later.hints["thing"] is module.Point
        with pytest.raises(JongoError) as caught:
            module.later.hints
        assert caught.value.code == "J405"
    finally:
        sys.modules.pop("forward_mod_070", None)


def test_a_page_query_parameter_typed_date_arrives_as_a_date():
    app = make_app()

    @app.route("/day")
    def day(request, when: datetime.date = None):
        return {"type": type(when).__name__, "iso": when.isoformat() if when else None}

    client = TestClient(app)
    assert client.get("/day?when=2026-11-21").json() == {"type": "date", "iso": "2026-11-21"}
    bad = client.get("/day?when=tomorrow")
    assert bad.status == 400 and "tomorrow" in bad.text   # J211, rendered as the error page


# The schema-vs-coerce table: every hint x every value. Whatever the schema accepts, coerce()
# must accept too, and accept as the right Python type.

_RFC3339_TIME = r"\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})"
_TABLE_HINTS = {
    "int": int, "float": float, "str": str, "bool": bool, "Optional[int]": Optional[int],
    "Union[int,str]": Union[int, str], "Union[str,int]": Union[str, int], "list[int]": list[int],
    "list[dict[str,int]]": list[dict[str, int]], "dict[str,int]": dict[str, int], "dict[int,str]": dict[int, str],
    "tuple[int,str]": tuple[int, str], "tuple[int,...]": tuple[int, ...], "set[int]": set[int],
    "frozenset[str]": frozenset[str], "Literal['a','b']": Literal["a", "b"], "Literal[1,2]": Literal[1, 2],
    "Literal[True]": Literal[True], "Literal[None]": Literal[None], "date": datetime.date,
    "datetime": datetime.datetime, "time": datetime.time, "uuid": uuid.UUID, "Decimal": decimal.Decimal,
    "bytes": bytes, "Slot": Slot, "Plan": Plan, "Level": Level, "Address": Address, "UserId": UserId,
    "None": type(None), "Any": Any, "list": list, "dict": dict, "tuple": tuple,
    "Optional[list[int]]": Optional[list[int]], "dict[str,Optional[float]]": dict[str, Optional[float]],
    "Optional[date]": Optional[datetime.date], "Sequence[int]": collections.abc.Sequence[int],
    "dict[Plan,int]": dict[Plan, int],
}
_TABLE_VALUES = [
    None, True, False, 0, 1, -1, 2, 1.0, 1.5, -0.0, 2**63, 2**63 - 1, -(2**63), 2**64, 1e300, "", "a", "b", "1",
    " 7 ", "1.5", "x", "2024-01-02", "2024-13-45", "2024-01-02T03:04:05", "2024-01-02T03:04:05Z",
    "2024-01-02T03:04:05.5+05:30", "03:04:05Z", "03:04", "25:00:00Z", "12345678-1234-5678-1234-567812345678",
    "not-a-uuid", "aGk=", "aGk", "free", "team", "FREE", [], [1], [1, 1], [1, "a"], [1.0, 1], ["a", "b"],
    [1, 2, 3], [1, "x", 3], {}, {"a": 1}, {"1": "x"}, {"a": "x"}, {"a": None}, {"a": 1.5}, {"x": 1},
    {"x": 1, "y": 2.5}, {"x": "1"}, {"y": 1}, {"free": 1}, {"street": "Main", "number": 1},
    {"day": "2024-01-02"}, {"day": "2024-01-02", "plan": "team"}, {"day": "nope"}, [{"a": 1}], [None],
]
#: (hint, value) pairs where JSON Schema can't say what coerce() requires: integers must fit in
#: 64 bits (SQLite's), and Literal/Enum values match by exact type, so 1.0 isn't 1.
_KNOWN = {("int", 2**63), ("int", 2**64), ("int", 1e300), ("Optional[int]", 2**63), ("Optional[int]", 2**64),
          ("Optional[int]", 1e300), ("Union[int,str]", 2**63), ("Union[int,str]", 2**64), ("Union[int,str]", 1e300),
          ("Union[str,int]", 2**63), ("Union[str,int]", 2**64), ("Union[str,int]", 1e300), ("UserId", 2**63),
          ("UserId", 2**64), ("UserId", 1e300), ("Literal[1,2]", 1.0), ("Level", 1.0)}
_PYTHON_TYPE = {datetime.date: datetime.date, datetime.datetime: datetime.datetime, datetime.time: datetime.time,
                uuid.UUID: uuid.UUID, decimal.Decimal: decimal.Decimal, bytes: bytes, Plan: Plan, Level: Level}


def _strict_formats():
    jsonschema = pytest.importorskip("jsonschema")
    checker = jsonschema.FormatChecker()

    @checker.checks("date-time", raises=ValueError)
    def date_time(value):
        if not isinstance(value, str):
            return True
        found = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[Tt](" + _RFC3339_TIME + ")", value)
        if not found:
            return False
        datetime.date.fromisoformat(found.group(1))
        return _time_ok(found.group(2))

    @checker.checks("time", raises=ValueError)
    def time_(value):
        return not isinstance(value, str) or (re.fullmatch(_RFC3339_TIME, value) is not None and _time_ok(value))

    return jsonschema, checker


def _time_ok(text):
    hours, minutes, seconds = int(text[0:2]), int(text[3:5]), int(text[6:8])
    return hours < 24 and minutes < 60 and seconds < 60


def test_the_schema_and_the_rpc_boundary_agree_on_every_pair():
    jsonschema, checker = _strict_formats()
    refused_but_valid, wrong_type = [], []
    for label, hint in _TABLE_HINTS.items():
        validator = jsonschema.Draft202012Validator(json_schema(hint), format_checker=checker)
        for value in _TABLE_VALUES:
            try:
                result, accepted = coerce(value, hint, "v"), True
            except HTTPError:
                result, accepted = None, False
            if validator.is_valid(value) and not accepted and (label, value) not in _KNOWN:
                refused_but_valid.append((label, value))
            expected = _PYTHON_TYPE.get(hint)
            if accepted and expected is not None and type(result) is not expected:
                wrong_type.append((label, value, type(result).__name__))
    assert len(_TABLE_HINTS) * len(_TABLE_VALUES) >= 1768       # at least the stress test's table
    assert refused_but_valid == [], "a value the schema allows must reach the function"
    assert wrong_type == [], "an accepted value arrives as the annotated type"


def test_the_new_types_refuse_what_their_schema_refuses():
    """The other direction, for the types this release starts checking (Decimal also reads text)."""
    jsonschema, checker = _strict_formats()
    lenient = []
    for hint in (datetime.date, datetime.datetime, datetime.time, uuid.UUID, decimal.Decimal, bytes, Plan, Level):
        validator = jsonschema.Draft202012Validator(json_schema(hint), format_checker=checker)
        for value in _TABLE_VALUES:
            try:
                coerce(value, hint, "v")
            except HTTPError:
                continue
            if not validator.is_valid(value):
                lenient.append((hint.__name__, value))
    allowed = {("Decimal", "1"), ("Decimal", " 7 "), ("Decimal", "1.5"), ("datetime", "2024-01-02T03:04:05"),
               ("time", "03:04")}
    assert set(lenient) <= allowed, set(lenient) - allowed


def test_schemas_for_the_new_types_describe_what_coerce_reads():
    assert json_schema(Plan) == {"title": "Plan", "enum": ["free", "team"]}
    assert json_schema(Level) == {"title": "Level", "enum": [1, 2]}
    assert json_schema(UserId) == {"type": "integer"}
    assert json_schema(Address) == {"type": "object", "title": "Address", "required": ["street", "number"],
                                    "properties": {"street": {"type": "string"}, "number": {"type": "integer"}}}
    assert json_schema(dict[int, str])["propertyNames"] == {"pattern": "^\\s*-?[0-9]+\\s*$"}
    assert json_schema(dict[Plan, int])["propertyNames"] == {"title": "Plan", "enum": ["free", "team"]}
    assert "pattern" in json_schema(bytes) and json_schema(bytes)["contentEncoding"] == "base64"
    assert json_schema(collections.abc.MutableSet[int]) == {"type": "array", "items": {"type": "integer"},
                                                            "uniqueItems": True}


# =============================================================== 4. passwords are hashed ==


def test_creating_a_user_with_a_raw_password_is_refused():
    db.migrate([auth.User])
    with pytest.raises(db.ValidationError) as caught:
        auth.User.create(username="ann", password="s3cret-pass")
    assert "create_user" in caught.value.errors["password"]
    assert auth.User.filter(username="ann").count() == 0, "nothing was written"


def test_assigning_a_raw_password_and_saving_is_refused():
    db.migrate([auth.User])
    user = auth.User.create_user("ann", "s3cret-pass")
    stored = user.password
    user.password = "other-pass"
    with pytest.raises(db.ValidationError):
        user.save()
    assert auth.User.get(id=user.id).password == stored
    user.set_password("other-pass")
    user.save()
    assert auth.authenticate("ann", "other-pass").id == user.id


def test_hashed_and_blank_passwords_still_save():
    db.migrate([auth.User])
    invited = auth.User.create(username="invited")                       # no password yet: fine
    assert invited.password == "" and not invited.check_password("")
    imported = auth.User.create(username="imported", password=auth.hash_password("pw"))
    assert auth.authenticate("imported", "pw").id == imported.id
    legacy = auth.User.create(username="legacy", password="pbkdf2_sha1$1000$salt$ZGlnZXN0")
    assert legacy.pk is not None, "a hash in the same layout from elsewhere can be stored"


def test_signing_in_a_user_whose_row_holds_an_old_raw_password_still_works():
    """Rows written by 0.6 (User.create(password=...)) mustn't make login() fail on save."""
    app = make_app()
    user = auth.User.create_user("ann", "pw")
    auth.User.filter(id=user.id).update(password="raw-from-0.6")

    @server
    def magic_link(request, username: str) -> str:
        auth.login(request, auth.User.get(username=username))
        return request.user.username

    client = TestClient(app)
    assert client.rpc(magic_link, "ann") == "ann"
    assert auth.User.get(id=user.id).last_login is not None


def test_editing_a_user_in_the_admin_keeps_the_stored_hash():
    app = Jongo("admin_070", root=HERE, dev=True, secret_key="k" * 40)
    app.admin()
    db.models_registry.update({"User": auth.User, "LoginSession": auth.LoginSession})
    db.migrate()
    root = auth.User.create_user("root", "pw", is_admin=True)
    bob = auth.User.create_user("bob", "bob-pw")
    client = app.test_client()
    client.login(root)
    client.get("/admin/user")
    form = {"username": "bob", "email": "bob@example.com", "name": "Bob", "_password": "",
            "csrf_token": client.cookies[CSRF_COOKIE]}
    response = client.post(f"/admin/user/{bob.id}", data=form)
    assert response.status == 303, response.text
    assert auth.User.get(id=bob.id).name == "Bob" and auth.authenticate("bob", "bob-pw").id == bob.id


# ======================================================= 5. the OpenAPI body matches calls ==


def body_app():
    app = make_app()

    @server
    def add_item(request, name: str, price: float, tags: list[str] = (), *, note: str = "") -> dict:
        return {"name": name, "price": price, "tags": list(tags), "note": note}

    @server
    def strict(request, a: int, /, b: int, *, c: int) -> int:
        return a + b + c

    @server
    def optional_only(request, n: int = 1) -> int:
        return n

    @server
    def spread(*values: int, **named: str) -> int:
        return len(values) + len(named)

    app.fns = dict(add_item=add_item, strict=strict, optional_only=optional_only, spread=spread)
    return app


def _body(app, fn):
    operation = openapi(app)["paths"][f"/_jongo/rpc/{fn.id}"]["post"]
    return operation["requestBody"], operation["requestBody"]["content"]["application/json"]["schema"]


def test_the_request_body_describes_args_and_kwargs_without_requiring_either():
    app = body_app()
    request_body, body = _body(app, app.fns["add_item"])
    assert request_body["required"] is True
    assert "required" not in body
    args, kwargs = body["properties"]["args"], body["properties"]["kwargs"]
    assert [item["type"] for item in args["prefixItems"]] == ["string", "number", "array"]
    assert args["maxItems"] == 3 and "note" not in str(args["prefixItems"]), "keyword-only can't go by position"
    assert "required" not in kwargs and set(kwargs["properties"]) == {"name", "price", "tags", "note"}
    assert [(s.get("properties", {}).get("args", {}).get("minItems", 0),
             s.get("properties", {}).get("kwargs", {}).get("required", [])) for s in body["anyOf"]] == [
        (0, ["name", "price"]), (1, ["price"]), (2, [])]
    assert function_schema(app.fns["add_item"])["required"] == ["name", "price"], "the MCP inputSchema is unchanged"

    _, strict = _body(app, app.fns["strict"])
    assert "a" not in strict["properties"]["kwargs"]["properties"], "positional-only can't go by name"
    assert [s["properties"]["args"]["minItems"] for s in strict["anyOf"]] == [1, 2]
    assert all(s["properties"]["kwargs"]["required"][-1] == "c" for s in strict["anyOf"])

    request_body, optional = _body(app, app.fns["optional_only"])
    assert request_body["required"] is False and "anyOf" not in optional, "an empty body is a valid call"
    _, spread = _body(app, app.fns["spread"])
    assert spread["properties"]["args"]["items"] == {"type": "integer"} and "maxItems" not in spread["properties"]["args"]
    assert spread["properties"]["kwargs"]["additionalProperties"] is True


@pytest.mark.parametrize("fn, payload, ok", [
    ("add_item", {"args": ["Lamp", 9.5], "kwargs": {}}, True),                    # what the browser sends
    ("add_item", {"args": ["Lamp", 9.5]}, True),
    ("add_item", {"kwargs": {"name": "Lamp", "price": 9.5}}, True),               # by name only
    ("add_item", {"args": ["Lamp"], "kwargs": {"price": 9.5, "note": "x"}}, True),  # mixed
    ("add_item", {"args": ["Lamp", 9.5, ["a"]], "kwargs": {"note": "n"}}, True),
    ("add_item", {}, False),
    ("add_item", {"args": ["Lamp"]}, False),
    ("add_item", {"kwargs": {"name": "Lamp"}}, False),
    ("add_item", {"args": ["Lamp", 9.5, [], "extra"]}, False),
    ("add_item", {"args": [3, 9.5]}, False),
    ("add_item", {"kwargs": {"name": "Lamp", "price": 9.5, "colour": "red"}}, False),
    ("strict", {"args": [1, 2], "kwargs": {"c": 3}}, True),
    ("strict", {"args": [1], "kwargs": {"b": 2, "c": 3}}, True),
    ("strict", {"kwargs": {"a": 1, "b": 2, "c": 3}}, False),
    ("strict", {"args": [1, 2, 3]}, False),
    ("optional_only", {}, True),
    ("optional_only", {"args": [4]}, True),
    ("spread", {"args": [1, 2, 3], "kwargs": {"x": "y"}}, True),
    ("spread", {"args": ["one"]}, False),
])
def test_the_body_schema_accepts_exactly_the_calls_the_server_accepts(fn, payload, ok):
    app = body_app()
    function = app.fns[fn]
    response = TestClient(app).post(f"/_jongo/rpc/{function.id}", json=payload)
    assert (response.status == 200) is ok, response.text
    jsonschema = pytest.importorskip("jsonschema")
    _, body = _body(app, function)
    assert jsonschema.Draft202012Validator(body).is_valid(payload) is ok


# ================================================= 6. a flood never lifts a caller's block ==


def test_flooding_the_limiter_never_evicts_a_blocked_counter(monkeypatch):
    """The stress test's cycle: 50k fresh keys used to evict a throttled login and buy 10 guesses."""
    monkeypatch.setattr(ratelimit, "MAX_KEYS", 100)
    now = [0.0]
    limiter = Limiter(clock=lambda: now[0])
    login, flood = RateLimit(10, 300), RateLimit(3, 3600)
    victim = "login|user:victim"
    while limiter.hit(victim, login)[0]:
        now[0] += 0.001
    for i in range(5000):                                   # fifty times the table
        now[0] += 0.001
        assert limiter.hit(f"reset|x{i}", flood) == (True, 0), "callers under their limit make room"
        assert limiter.peek(victim, login)[0] is False, f"the victim's block was lifted after {i} keys"
    assert len(limiter) <= 100
    assert limiter.peek("reset|x4999", flood) == (True, 0) and len(limiter._hits["reset|x4999"]) == 1, \
        "the most recent callers are kept"
    now[0] += 301
    assert limiter.peek(victim, login) == (True, 0), "the block ends when its window does"


def test_a_table_full_of_blocked_callers_refuses_new_ones(monkeypatch):
    monkeypatch.setattr(ratelimit, "MAX_KEYS", 100)
    now = [0.0]
    limiter = Limiter(clock=lambda: now[0])
    once = RateLimit(1, 600)                                # one hit and the key is over its limit
    for i in range(100):
        now[0] += 0.001
        assert limiter.hit(f"blocked{i}", once) == (True, 0)
    allowed, retry_after = limiter.hit("newcomer", RateLimit(5, 60))
    assert not allowed and 590 <= retry_after <= 600, "fails closed until the first block ends"
    assert all(limiter.peek(f"blocked{i}", once)[0] is False for i in range(100)), "nobody was evicted"
    limiter.reset("blocked7")
    assert limiter.hit("newcomer", RateLimit(5, 60)) == (True, 0), "a reset frees a slot at once"
    assert limiter.hit("another", RateLimit(5, 60))[0] is False
    now[0] += 601
    assert limiter.hit("another", RateLimit(5, 60)) == (True, 0), "and room returns as blocks end"


def test_expired_keys_go_first_then_the_least_recent_under_their_limit(monkeypatch):
    monkeypatch.setattr(ratelimit, "MAX_KEYS", 10)
    now = [0.0]
    limiter = Limiter(clock=lambda: now[0])
    for i in range(3):
        limiter.hit(f"short{i}", RateLimit(5, 1))           # gone after a second
    for i in range(3):
        limiter.hit(f"long{i}", RateLimit(1, 600))          # over their limit for ten minutes
    for i in range(4):
        now[0] += 1
        limiter.hit(f"calm{i}", RateLimit(5, 600))          # well under their limit
    now[0] += 1
    assert limiter.hit("new0", RateLimit(5, 60)) == (True, 0)
    assert not any(f"short{i}" in limiter._hits for i in range(3)), "expired keys went first"
    assert all(f"calm{i}" in limiter._hits for i in range(4)), "...and were enough"
    for i in range(1, 4):
        assert limiter.hit(f"new{i}", RateLimit(5, 60)) == (True, 0)
    assert limiter.hit("new4", RateLimit(5, 60)) == (True, 0)
    assert "calm0" not in limiter._hits, "then the least recent caller under its limit"
    assert all(f"long{i}" in limiter._hits for i in range(3)), "never one that is over it"


def test_the_login_throttle_has_its_own_limiter(monkeypatch):
    monkeypatch.setattr(ratelimit, "MAX_KEYS", 50)
    monkeypatch.setattr(auth, "LOGIN_ATTEMPTS", 3)
    db.migrate()
    auth.User.create_user("victim", "right")
    assert auth.limiter is not ratelimit.limiter
    for guess in "abc":
        auth.authenticate("victim", guess)
    for i in range(500):                                    # fill the rate_limit= limiter many times over
        try:
            ratelimit.check(None, "3/hour", "reset-password", key=f"x{i}@example.com")
        except TooManyRequests:
            pass
    with pytest.raises(TooManyRequests):
        auth.authenticate("victim", "right")


# ================================================================== 7. CSRF at login/out ==


class _PlainRequest:
    def __init__(self):
        self.session = {}
        self.headers = {}
        self.client_ip = "127.0.0.1"


class _RotatingRequest(_PlainRequest):
    rotations = 0

    def rotate_csrf(self):
        self.rotations += 1


def test_login_and_logout_rotate_the_csrf_token():
    db.migrate()
    ann = auth.User.create_user("ann", "pw")
    request = _RotatingRequest()
    auth.login(request, ann)
    assert request.rotations == 1 and request.user is ann
    auth.logout(request)
    assert request.rotations == 2 and request.user is None


def test_login_works_on_a_request_without_rotate_csrf():
    db.migrate()
    ann = auth.User.create_user("ann", "pw")
    request = _PlainRequest()
    auth.login(request, ann)
    assert request.user is ann and request.session["_uid"] == ann.id
    auth.logout(request)
    assert request.user is None


@pytest.mark.skipif(not hasattr(Request, "rotate_csrf"), reason="Request.rotate_csrf arrives with the http batch")
def test_a_csrf_token_minted_before_sign_in_is_replaced():
    app = make_app()

    @server
    def sign_in(request, username: str, password: str) -> bool:
        auth.login(request, auth.authenticate(username, password, request))
        return True

    @server
    def sign_out(request) -> bool:
        auth.logout(request)
        return True

    auth.User.create_user("ann", "pw")
    client = TestClient(app)
    client.post(f"/_jongo/rpc/{sign_in.id}", json={"args": ["ghost", "x"], "kwargs": {}})  # mints a token
    before = client.cookies[CSRF_COOKIE]
    assert client.rpc(sign_in, "ann", "pw") is True
    after_login = client.cookies[CSRF_COOKIE]
    assert after_login != before
    assert client.rpc(sign_out) is True
    assert client.cookies[CSRF_COOKIE] != after_login


# ========================================================= 8. constant work for blank users ==


def test_a_user_without_a_password_costs_one_hash_like_everyone_else(monkeypatch):
    db.migrate([auth.User])
    auth.User.create(username="invited")                          # exists, no password yet
    auth.User.create_user("known", "pw")
    auth.User.create_user("legacy", "pw")
    auth.User.filter(username="legacy").update(password="raw-from-0.6")
    hashed = []
    real = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda password, encoded: hashed.append(1) or real(password, encoded))
    for name in ("nobody", "known", "invited", "legacy"):
        hashed.clear()
        assert auth.authenticate(name, "guess") is None
        assert hashed == [1], f"{name}: exactly one password hash per attempt"


def test_check_password_on_a_blank_password_hashes_and_says_no(monkeypatch):
    calls = []
    monkeypatch.setattr(auth, "verify_password", lambda password, encoded: calls.append(encoded) or True)
    assert auth.User(username="x").check_password("") is False, "even if the hash check said yes"
    assert calls == [auth._dummy()]


# ================================================== 9. creating jongo_sessions concurrently ==


_CHILD = textwrap.dedent('''
    import os, sys, time
    from jongo import db, auth
    db.configure(sys.argv[1])
    user = auth.User.get(username="ann")
    open(os.path.join(sys.argv[2], "ready-" + sys.argv[3]), "w").close()
    go = os.path.join(sys.argv[2], "go")
    deadline = time.time() + 30
    while not os.path.exists(go) and time.time() < deadline:
        time.sleep(0.0005)
    try:
        auth.session_data_for(user)
        print("ok")
    except BaseException as exc:
        print("ERR", type(exc).__name__, exc)
''')


@sqlite_only
@pytest.mark.parametrize("round_", range(2))
def test_processes_signing_in_at_once_after_an_upgrade_all_succeed(tmp_path, round_):
    database = str(tmp_path / "app.sqlite3")
    env = {**os.environ, "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    setup = ("from jongo import db\nfrom jongo.auth import User\n"
             f"db.configure({database!r}); db.migrate([User]); User.create_user('ann', 'pw')")
    subprocess.run([sys.executable, "-c", setup], check=True, env=env, timeout=60)
    count = 6
    children = [subprocess.Popen([sys.executable, "-c", _CHILD, database, str(tmp_path), str(i)],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
                for i in range(count)]
    deadline = time.time() + 60
    while len(list(tmp_path.glob("ready-*"))) < count and time.time() < deadline:
        time.sleep(0.01)
    (tmp_path / "go").touch()
    results = [child.communicate(timeout=60)[0].strip().splitlines()[-1] for child in children]
    assert results == ["ok"] * count, results


def test_a_table_created_by_someone_else_mid_way_counts_as_success(monkeypatch):
    """As on PostgreSQL, where BEGIN takes no lock: both look, both create, one CREATE fails."""
    migrate = sys.modules["jongo.db.migrate"]

    db.migrate()                                            # the table really exists...
    monkeypatch.setattr(auth, "_table_ready", None)
    real_live_tables, calls = migrate.live_tables, []

    def stale_view(conn):                                   # ...but both looks before CREATE say it doesn't
        calls.append(1)
        tables = real_live_tables(conn)
        if len(calls) <= 2:
            tables.pop(auth.LoginSession._meta.table, None)
        return tables

    monkeypatch.setattr(migrate, "live_tables", stale_view)
    monkeypatch.setattr(migrate, "plan_migrations", lambda models=None: [migrate._create_table(auth.LoginSession)])
    auth._ensure_table()                                    # CREATE TABLE fails; the re-check sees it: fine
    assert auth._table_ready is not None and len(calls) == 3


def test_a_create_that_really_failed_is_still_reported(monkeypatch):
    migrate = sys.modules["jongo.db.migrate"]

    db.migrate([auth.User])                                 # a 0.5 database: no sessions table
    monkeypatch.setattr(auth, "_table_ready", None)

    def broken(conn, op):
        raise migrate.MigrationError(f"{op.describe()} failed: disk full")

    monkeypatch.setattr(migrate, "_apply", broken)
    with pytest.raises(migrate.MigrationError):
        auth._ensure_table()
    assert auth._table_ready is None
