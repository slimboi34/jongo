"""Form schemas: one definition, checked in CPython and in the compiled JavaScript.

The promise of ``jongo.forms`` is that the browser and the server apply the same rules.
So the heart of this file is a parity test: every rule, against a table of awkward
values, run natively and run in Node from the compiled bundle, and the two results must
be identical. The rest covers the server side: cleaning, ``@server`` parameters, building
a schema from a model, and the errors a bad definition raises.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import use_test_database
from jongo import Jongo, ServerError, component, db, form_values, forms, rpc, server, state, styles, vdom
from jongo.errors import JongoError
from jongo.html import button, form
from jongo.testing import TestClient
from test_compiler import needs_node, run_in_node

Signup = forms.Schema(
    "Signup",
    email=forms.Email(),
    password=forms.Text(min_length=8, secret=True),
    confirm=forms.Text(same_as="password", label="Confirmation", secret=True),
    name=forms.Text(min_length=2, max_length=10, required=False),
    age=forms.Int(min=18, max=120, required=False),
    score=forms.Float(min=0, max=1, required=False),
    role=forms.Choice(["member", "admin"], default="member"),
    plan=forms.Choice([(1, "Free"), (2, "Pro")]),
    code=forms.Text(pattern=r"[A-Z]{2}-\d{3}", required=False, messages={"pattern": "Use the form AB-123."}),
    slug=forms.Text(pattern=r"\w+(?:-\w+)*", required=False),
    terms=forms.Bool(required=True, label="The terms"),
    news=forms.Bool(),
)

#: Values chosen to sit on the edges where Python and JavaScript are known to differ:
#: bools that are ints, floats that are whole, non-ASCII digits and spaces, astral
#: characters, numbers too big for a double, and text where a number is expected.
AWKWARD = [
    None, "", "   ", "a", "ab", "abcdefghij", "abcdefghijk", " padded ", "x@y.z", "x@y", "a b@c.d",
    "x@y.z\n", "AB-123", "ab-123", "AB-1234", "AB-١٢٣", "one-two", "one--two", "naïve", "😀😀", "😀" * 11,
    "0", "17", "18", "120", "121", "-5", " 42 ", "4.0", "4.5", "1e3", "1e400", ".5", "5.", "+5", "0x10",
    "٤٢", "9007199254740991", "9007199254740993", "99999999999999999999", "0.5", "1", "1.0000001",
    "on", "off", "true", "FALSE", "yes", "no", "maybe", "member", "admin", "Member", "root", "1 ", "2", "3",
    True, False, 0, 1, 2, 3, 17, 18, 4.0, 4.5, -1, 0.5, 1.5, 120, 121, 9007199254740993,
    [], ["a"], {}, {"a": 1},
]


def py_results():
    out = {}
    for field in Signup:
        out[field] = [forms.check(Signup[field], value, {"password": "secret-pw"}) for value in AWKWARD]
    return out


def s_check_every_rule():
    out = {}
    for field in Signup:
        out[field] = [forms.check(Signup[field], value, {"password": "secret-pw"}) for value in AWKWARD]
    return out


FORMS = [
    {},
    {"email": "ann@example.com", "password": "secret-pw", "confirm": "secret-pw", "plan": "2", "terms": "on"},
    {"email": "ann@example.com", "password": "secret-pw", "confirm": "secret-pW", "plan": "9", "terms": "off"},
    {"email": " ann@example.com ", "password": " spaced pw ", "confirm": " spaced pw ", "plan": 1, "terms": True,
     "age": "18", "score": "0.25", "name": "Ann", "code": "AB-123", "slug": "a-b", "news": "on", "extra": "x"},
    {"email": "nope", "password": "short", "confirm": "", "age": "abc", "score": "2", "role": "owner",
     "plan": "", "code": "x", "slug": "-a", "terms": "", "news": "perhaps", "name": "x"},
]


def s_validate_whole_forms():
    return [forms.validate(Signup, values) for values in FORMS]


def s_attrs():
    return [forms.attrs(Signup, name) for name in Signup]


def s_matches():
    return [forms.matches(forms.EMAIL_PATTERN, v) for v in ["a@b.co", "a@b", "a b@c.d", "a@b.co\n", "é@ü.io"]]


@needs_node
def test_every_rule_gives_the_same_answer_in_python_and_in_the_browser(tmp_path):
    """The parity test: ~70 awkward values x 12 fields, CPython vs the compiled bundle."""
    result = run_in_node({"check": s_check_every_rule}, tmp_path)["check"]
    assert "ok" in result, result
    expected = py_results()
    for field in Signup:
        for value, py, js in zip(AWKWARD, expected[field], result["ok"][field]):
            assert py == js, f"{field} disagrees on {value!r}: python {py!r}, browser {js!r}"


@needs_node
def test_whole_forms_validate_identically(tmp_path):
    result = run_in_node({"forms": s_validate_whole_forms, "attrs": s_attrs, "matches": s_matches}, tmp_path)
    assert result["forms"]["ok"] == s_validate_whole_forms()
    assert result["attrs"]["ok"] == s_attrs()
    assert result["matches"]["ok"] == s_matches() == [True, False, False, False, True]


# -- rules, on the server ------------------------------------------------------------------


def test_the_awkward_values_are_judged_as_documented():
    def ok(field, value):
        return forms.check(Signup[field], value, {"password": "secret-pw"}) == ""

    assert ok("email", "x@y.z") and not ok("email", "x@y") and not ok("email", "a b@c.d")
    assert not ok("email", None) and ok("name", None) and ok("name", "   ")
    assert ok("name", "😀😀") and not ok("name", "😀" * 11)        # length is in characters
    assert ok("age", "18") and ok("age", " 42 ") and ok("age", 18) and ok("age", 18.0)
    assert not ok("age", "17") and not ok("age", "4.5") and not ok("age", 4.5) and not ok("age", True)
    assert not ok("age", "٤٢"), "non-ASCII digits are not a whole number on either side"
    assert not ok("age", "+5") and not ok("age", "0x10")
    assert ok("score", "0.5") and ok("score", ".5") and ok("score", 1) and not ok("score", "1.0000001")
    assert not ok("score", "1e400"), "infinity is not a number a form may send"
    assert ok("code", "AB-123") and not ok("code", "AB-١٢٣") and not ok("code", "AB-1234")
    assert ok("plan", "2") and ok("plan", 2) and not ok("plan", "3") and not ok("plan", True)
    assert ok("terms", "on") and ok("terms", True) and not ok("terms", None) and not ok("terms", "maybe")
    assert ok("news", None) and ok("news", "off")
    assert ok("confirm", "secret-pw") and not ok("confirm", "secret-pW")


def test_messages_are_prerendered_and_overridable():
    assert Signup["age"]["messages"]["min"] == "Age must be at least 18."
    assert Signup["code"]["messages"]["pattern"] == "Use the form AB-123."
    assert Signup["name"]["messages"]["max_length"] == "Name must be at most 10 characters."
    assert Signup.validate({})["terms"] == "This box must be ticked."
    assert forms.Schema(ok=forms.Bool(required=True, messages={"required": "Accept the terms."})).validate({}) == {
        "ok": "Accept the terms."}
    whole = forms.Schema(limit=forms.Float(min=5.0, max=7.5))
    assert whole["limit"]["min"] == 5 and isinstance(whole["limit"]["min"], int)   # prints "5" on both sides
    assert whole["limit"]["messages"] == {**whole["limit"]["messages"], "min": "Limit must be at least 5.",
                                          "max": "Limit must be at most 7.5."}


def test_clean_returns_typed_values_and_only_the_schemas_fields():
    data = Signup.clean(FORMS[3])
    assert data == {
        "email": "ann@example.com", "password": " spaced pw ", "confirm": " spaced pw ", "name": "Ann",
        "age": 18, "score": 0.25, "role": "member", "plan": 1, "code": "AB-123", "slug": "a-b",
        "terms": True, "news": True,
    }
    assert "extra" not in data, "a key the schema doesn't declare must never come through"
    assert type(data["age"]) is int and type(data["score"]) is float and type(data["plan"]) is int
    assert Signup(FORMS[1])["age"] is None and Signup(FORMS[1])["news"] is False     # calling is cleaning


def test_clean_raises_validation_error_with_field_messages():
    with pytest.raises(db.ValidationError) as caught:
        Signup.clean(FORMS[4])
    errors = caught.value.errors
    assert errors["email"] == "Enter a valid email address."
    assert errors["age"] == "Age must be a whole number."
    assert errors["role"] == "Role must be one of: member, admin."
    assert set(errors) == {"email", "password", "confirm", "name", "age", "score", "role", "plan", "code",
                           "slug", "terms", "news"}
    with pytest.raises(db.ValidationError):
        Signup.clean("not a dict")


def test_defaults_give_a_forms_starting_values():
    assert Signup.defaults()["role"] == "member" and Signup.defaults()["terms"] is False
    assert Signup.defaults()["email"] == ""


# -- patterns ----------------------------------------------------------------------------------


def test_patterns_are_rewritten_to_the_shared_syntax():
    assert forms.portable_pattern(r"\d{3}-\w+") == "[0-9]{3}-[A-Za-z0-9_]+"
    assert forms.portable_pattern(r"[\d\s]+") == r"[0-9 \t\n\r\f\v]+"
    assert forms.portable_pattern(r"\D\S") == r"[^0-9][^ \t\n\r\f\v]"
    assert forms.portable_pattern(r"a.b") == r"a[^\n\r]b"
    assert forms.portable_pattern(r"a\.b\@c\-d") == r"a\.b@c-d"       # \@ and \- are errors in JS u-mode
    assert forms.portable_pattern(r"[a\-z\]]") == r"[a\-z\]]"
    assert forms.portable_pattern(r"[.]") == "[.]"                     # a dot in a class is just a dot


@pytest.mark.parametrize("pattern", [r"(?P<year>\d+)", r"(?i)abc", r"\Aabc\Z", r"a++", r"(?>a)", r"[\D]",
                                     r"(?<name>a)", r"(unclosed", r"\p{L}+"])
def test_patterns_only_one_engine_understands_are_refused_at_definition(pattern):
    with pytest.raises(JongoError) as caught:
        forms.Text(pattern=pattern)
    assert caught.value.code == "J115"


@pytest.mark.parametrize("build", [
    lambda: forms.Schema(email="not a rule"),
    lambda: forms.Schema(a=forms.Text(same_as="missing")),
    lambda: forms.Schema(a=forms.Int(min=5, default=1)),
    lambda: forms.Text(min_length=5, max_length=2),
    lambda: forms.Int(min=5, max=2),
    lambda: forms.Choice([]),
    lambda: forms.Choice([1, "1"]),
    lambda: forms.Choice([1.5]),
    lambda: forms.Text(messages={"nope": "x"}),
    lambda: forms.Schema("A", "B"),
])
def test_a_schema_that_makes_no_sense_is_an_error_when_it_is_defined(build):
    with pytest.raises(JongoError) as caught:
        build()
    assert caught.value.code == "J115"


# -- from a model, and as a @server parameter ----------------------------------------------------


@pytest.fixture()
def registries():
    saved = [dict(r) for r in (db.models_registry, vdom.COMPONENTS, rpc.SERVER_FUNCTIONS, styles._REGISTRY)]
    db.models_registry.clear()
    use_test_database()
    yield
    db.close_connections()
    for registry, before in zip((db.models_registry, vdom.COMPONENTS, rpc.SERVER_FUNCTIONS, styles._REGISTRY),
                                saved):
        registry.clear()
        registry.update(before)


def test_a_schema_built_from_a_model_follows_the_models_fields(registries):
    class Team(db.Model):
        name = db.Text(max_length=40)

    class Member(db.Model):
        email = db.Text(max_length=200, unique=True)
        role = db.Text(max_length=20, default="member", choices=[("member", "Member"), ("admin", "Admin")])
        seats = db.Int(default=1)
        rate = db.Float(null=True)
        active = db.Bool(default=True)
        team = db.ForeignKey(Team)
        notes = db.Text(blank=True, default="")
        secret = db.Text(editable=False, default="")
        joined = db.DateTime(auto_now_add=True)

    schema = forms.Schema.from_model(Member, exclude=("rate",))
    assert schema.__name__ == "MemberForm"
    assert list(schema) == ["email", "role", "seats", "active", "team_id", "notes"]
    assert schema["email"]["max_length"] == 200 and schema["email"]["required"]
    assert schema["role"]["type"] == "choice" and schema["role"]["choice_labels"] == ["Member", "Admin"]
    assert schema["role"]["default"] == "member" and not schema["role"]["required"]
    assert schema["seats"]["type"] == "int" and schema["team_id"]["min"] == 1
    assert not schema["notes"]["required"]
    cleaned = schema.clean({"email": "a@b.co", "team_id": "3"})
    assert cleaned == {"email": "a@b.co", "role": "member", "seats": 1, "active": False, "team_id": 3,
                       "notes": ""}
    assert list(forms.Schema.from_model(Member, only=("email", "role"))) == ["email", "role"]

    class Odd(db.Model):
        code = db.Text(max_length=3, default="TOO-LONG")       # a default the column's own limit rejects
        level = db.Int(default=0)

    odd = forms.Schema.from_model(Odd)
    assert "default" not in odd["code"], "an unusable default is not carried over"
    assert odd["level"]["default"] == 0


def test_a_server_parameter_annotated_with_a_schema_is_validated_and_cleaned(registries):
    app = Jongo("forms_app", database=":memory:" if not __import__("conftest").on_postgres() else None)
    use_test_database()
    seen = []

    @server
    def sign_up(request, data: Signup, note: str = "") -> dict:
        seen.append(data)
        return {"email": data["email"], "plan": data["plan"], "note": note}

    client = TestClient(app)
    good = {**FORMS[1], "is_admin": True}
    assert client.rpc(sign_up, good, note="hi") == {"email": "ann@example.com", "plan": 2, "note": "hi"}
    assert "is_admin" not in seen[0] and seen[0]["role"] == "member"

    with pytest.raises(ServerError) as caught:
        client.rpc(sign_up, {"email": "nope"})
    assert caught.value.status == 400 and caught.value.code == "J304"
    assert caught.value.errors["email"] == "Enter a valid email address."
    assert caught.value.errors["password"] == "Password is required."
    assert len(seen) == 1, "the function must not run when the schema rejects the call"

    with pytest.raises(ServerError) as caught:
        client.rpc(sign_up, "a string")
    assert caught.value.code == "J403" and "must be an object" in str(caught.value)


def test_a_form_component_renders_on_the_server_and_compiles(registries):
    app = Jongo("forms_ui", database=None)

    @server
    def save(request, data: Signup) -> dict:
        return data

    @component
    def SignupForm():
        errors = state({})

        async def submit(event):
            values = form_values(event)
            found = forms.validate(Signup, values)
            errors.set(found)
            if not found:
                try:
                    await save(values)
                except ServerError as exc:
                    errors.set(exc.errors or {})

        return form([forms.Field(schema=Signup, name=name, error=errors.value.get(name), key=name)
                     for name in Signup], button("Join"), on_submit=submit)

    @app.page("/")
    def home():
        return SignupForm()

    client = TestClient(app)
    html = client.get("/").text
    assert '<input name="email" type="email" autocomplete="email" required maxlength="254">' in html
    assert '<input name="password" type="password" required minlength="8">' in html
    assert '<input name="age" type="number" step="1" inputmode="numeric" min="18" max="120">' in html
    assert '<option value="member" selected>member</option>' in html
    seats = forms.Schema(seats=forms.Int(min=1, default=3))
    assert 'placeholder="3"' in vdom.render_to_string(forms.Field(schema=seats, name="seats"))
    assert '<select name="plan" required><option value=""></option><option value="1">Free</option>' in html
    assert '<input name="terms" type="checkbox" required><span class="jongo-field-label">The terms</span>' in html
    bundle = client.get("/_jongo/app.js")
    assert bundle.status == 200, bundle.text[:400]
    # the schema is inlined as data, and the validator is the compiled Python, not a second copy
    assert '"Enter a valid email address."' in bundle.text
    assert "jongo_forms__validate" in bundle.text and "jongo_forms__check" in bundle.text
    assert "$matches(" in bundle.text


def test_an_error_is_shown_next_to_its_field_with_an_alert_role():
    html = vdom.render_to_string(forms.Field(schema=Signup, name="email", value="x", error="Enter a valid email address."))
    assert 'aria-invalid="true"' in html and 'value="x"' in html
    assert '<span class="jongo-field-error" role="alert">Enter a valid email address.</span>' in html
    multi = forms.Schema(bio=forms.Text(multiline=True, required=False, help="A line or two"))
    html = vdom.render_to_string(forms.Field(schema=multi, name="bio"))
    assert "<textarea" in html and '<span class="jongo-field-help">A line or two</span>' in html


def test_module_qualified_helpers_compile():
    """``forms.validate`` in a component: the compiler resolves ``module.name`` statically."""
    from jongo.compiler.bundle import Bundler

    def uses_module_attribute():
        return forms.validate(Signup, {})

    here = Path(__file__).parent
    js = Bundler(roots=[here]).resolve("uses_module_attribute", uses_module_attribute)
    assert js.endswith("uses_module_attribute")

    def uses_missing_attribute():
        return forms.no_such_thing(1)

    from jongo.errors import CompileError

    with pytest.raises(CompileError) as caught:
        Bundler(roots=[here]).resolve("uses_missing_attribute", uses_missing_attribute)
    assert caught.value.code == "J103" and "no_such_thing" in str(caught.value)
