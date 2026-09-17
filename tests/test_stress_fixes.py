"""Regression tests for the 0.2.0 stress-audit fixes.

Each test pins a bug that a stress audit of 0.1.0 exposed. Grouped by subsystem.
The compiler tests need `node` (skipped otherwise), like test_compiler.py.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from jongo import HTTPError, Jongo, ServerError, component, css, db, server
from jongo.auth import User, verify_password
from jongo.compiler.bundle import Bundler
from jongo.errors import JongoError
from jongo.html import a, div, h, img
from jongo.vdom import build, render_to_string, serialize, to_json_data
from datetime import datetime, timedelta, timezone

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
HERE = Path(__file__).parent


# ---- compiler: Python -> JS semantics ---------------------------------------------------

def _run_js(fn, tmp_path):
    bundler = Bundler(roots=[HERE])
    js = bundler.resolve(fn.__name__, fn)
    script = bundler.render(f"console.log(JSON.stringify({js}()));")
    path = tmp_path / "b.js"
    path.write_text(script)
    proc = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _c_set_ops():
    a, b = {1, 2, 3}, {2, 3, 4}
    return [sorted(a & b), sorted(a - b), sorted(a ^ b), sorted(a | b)]

def _c_bool_eq():
    return [True == 1, False == 0, True in [1, 2], 2 == True, 1 == 1.0]

def _c_modpow():
    return [pow(2, 10, 1000), pow(3, 4), pow(5, 3, 13)]

def _c_round():
    return [round(1.25, 1), round(2.675, 2), round(2.5), round(0.125, 2), round(-1.25, 1)]

def _c_format():
    return ["{x[0]}".format(x=[9, 8]), f"{255:#x}", f"{8:#o}", f"{5:#b}", "{0[1]}".format(["a", "b"])]

def _c_strmethods():
    return ["AbC".swapcase(), "a-b-c".rsplit("-", 1), "a.b.c".partition("."), "x".removeprefix("x")]

def _c_builtins():
    return [bin(10), oct(8), hex(255), sorted(frozenset([3, 1, 2, 1]))]


@needs_node
@pytest.mark.parametrize("fn", [_c_set_ops, _c_bool_eq, _c_modpow, _c_round, _c_format, _c_strmethods, _c_builtins])
def test_compiler_matches_python(fn, tmp_path):
    assert _run_js(fn, tmp_path) == json.loads(json.dumps(fn()))  # JSON normalizes tuples->lists


# ---- vdom: rendering / serialization security ------------------------------------------

def test_v_sentinel_stays_data_not_live_vnode():
    payload = {"$v": {"t": "img", "p": {"src": "x", "onerror": "steal()"}}}
    wire = to_json_data({"note": payload})
    assert wire == {"note": {"$$v": {"t": "img", "p": {"src": "x", "onerror": "steal()"}}}}
    revived = build({"t": "div", "p": {"note": wire["note"]}})
    assert revived.props["note"] == payload          # round-trips as plain data
    assert "<img" not in render_to_string(revived)   # never becomes a live element/handler

def test_nan_and_inf_serialize_to_null():
    assert to_json_data(float("nan")) is None
    assert to_json_data(float("inf")) is None
    assert json.loads(json.dumps(to_json_data({"avg": float("nan")}))) == {"avg": None}

def test_javascript_url_is_dropped():
    assert "javascript:" not in render_to_string(a("x", href="javascript:alert(1)"))
    assert 'href="https://ok"' in render_to_string(a("x", href="https://ok"))
    # control chars in the scheme don't sneak it past
    assert "script" not in render_to_string(a("x", href="java\tscript:alert(1)")).split(">")[0]

def test_injected_attribute_name_is_dropped():
    html = render_to_string(h("div", **{"x onmouseover=alert(1) y": "z"}))
    assert "onmouseover" not in html

def test_invalid_tag_name_raises():
    with pytest.raises(JongoError):
        render_to_string(h("div onload=alert(1)", "hi"))

def test_css_injection_key_rejected():
    with pytest.raises(ValueError):
        css(**{"x} body{display:none": {"color": "red"}})


# ---- http: sessions / csrf / routing ----------------------------------------------------

def _session_app():
    app = Jongo("sess_app", secret_key="test-secret")

    @app.post("/add")
    def add(request):
        request.session.setdefault("items", [])
        request.session["items"].append("x")  # nested mutation
        return {"items": request.session["items"]}

    return app

def test_session_nested_mutation_persists():
    client = _session_app().test_client()
    assert client.post("/add").json() == {"items": ["x"]}
    assert client.post("/add").json() == {"items": ["x", "x"]}   # persisted across requests
    assert client.post("/add").json() == {"items": ["x", "x", "x"]}

def test_login_next_is_percent_encoded():
    app = Jongo("next_app", secret_key="s")

    @app.page("/secret", login_required=True)
    def secret():
        return div("secret")

    r = app.test_client().get("/secret?view=mine&q=hello")
    assert r.status == 302
    loc = r.headers["location"]
    assert "&amp;" not in loc and "next=%2Fsecret%3Fview%3Dmine%26q%3Dhello" in loc

def test_literal_route_beats_dynamic_regardless_of_order():
    app = Jongo("route_app", secret_key="s")

    @app.get("/o/<thing>")
    def generic(thing):
        return f"generic:{thing}"

    @app.get("/o/special")
    def special():
        return "special"

    assert app.test_client().get("/o/special").text == "special"
    assert app.test_client().get("/o/other").text == "generic:other"

def test_csrf_requires_signed_token_and_rejects_null_origin():
    app = Jongo("csrf_app", secret_key="s")

    @app.post("/do")
    def do():
        return "ok"

    # An attacker-chosen self-consistent pair (unsigned) is rejected.
    forged = {"HTTP_COOKIE": "jongo_csrf=forged123456789012345678901234567890", "HTTP_X_CSRF_TOKEN": "forged123456789012345678901234567890"}
    captured = {}
    app({"REQUEST_METHOD": "POST", "PATH_INFO": "/do", "QUERY_STRING": "", "wsgi.input": __import__("io").BytesIO(b""),
         "CONTENT_LENGTH": "0", "SERVER_NAME": "t", "SERVER_PORT": "80", **forged},
        lambda s, h, e=None: captured.update(status=s))
    assert captured["status"].startswith("403")
    # A legitimate signed token (what the test client mints) succeeds.
    assert app.test_client().post("/do").status == 200

def test_set_cookie_rejects_control_chars():
    from jongo.http import Response
    with pytest.raises(ValueError):
        Response().set_cookie("x", "tab\there")


# ---- rpc / auth -------------------------------------------------------------------------

def test_literal_rejects_bool_and_float():
    from typing import Literal
    from jongo.rpc import coerce
    assert coerce(2, Literal[0, 1, 2, 3], "p") == 2
    for bad in (False, True, 1.0):
        with pytest.raises(HTTPError):
            coerce(bad, Literal[0, 1, 2, 3], "p")

def test_factory_server_functions_collide_loudly():
    def make():
        @server
        def action():
            return 1
        return action
    make()
    with pytest.raises(JongoError):
        make()

def test_coerce_rejects_huge_int_and_non_finite_float():
    from jongo.rpc import coerce
    with pytest.raises(HTTPError):
        coerce("1" * 5000, int, "n")
    for bad in ("nan", "inf", "1e999"):
        with pytest.raises(HTTPError):
            coerce(bad, float, "x")

def test_verify_password_never_crashes_on_bad_hash():
    for bad in ("pbkdf2_sha256$notanint$s$h", "pbkdf2_sha256$0$s$h", "pbkdf2_sha256$-5$s$h", "garbage", "", None):
        assert verify_password("pw", bad) is False


# ---- orm --------------------------------------------------------------------------------

@pytest.fixture
def memdb():
    db.configure(":memory:")
    yield
    db.close_connections()

def test_datetime_orders_and_filters_chronologically(memdb):
    class Ev(db.Model):
        when = db.DateTime()

    db.migrate()
    later = datetime(2024, 1, 1, 23, 0, tzinfo=timezone(timedelta(hours=-5)))   # 04:00Z
    earlier = datetime(2024, 1, 2, 1, 0, tzinfo=timezone.utc)                    # 01:00Z
    Ev.create(when=later)
    Ev.create(when=earlier)
    order = [e.when.astimezone(timezone.utc) for e in Ev.order_by("when")]
    assert order[0] == earlier and order[1] == later
    cut = datetime(2024, 1, 2, 2, 0, tzinfo=timezone.utc)
    assert [e.when.astimezone(timezone.utc) for e in Ev.filter(when__gt=cut)] == [later]

def test_float_nan_is_rejected_not_silently_nulled(memdb):
    class M(db.Model):
        v = db.Float()

    db.migrate()
    with pytest.raises(db.ValidationError):
        M.create(v=float("nan"))

def test_mutable_default_is_per_instance(memdb):
    class M(db.Model):
        opts = db.JSON(default=list)
        tags = db.JSON(default=dict)

    db.migrate()
    a = M.create()
    a.opts.append("x")
    a.tags["k"] = "v"
    b = M.create()
    assert b.opts == [] and b.tags == {}

def test_large_in_list_and_no_truncation(memdb):
    class M(db.Model):
        n = db.Int()

    db.migrate()
    for i in range(5):
        M.create(n=i)
    assert M.filter(n__in=list(range(40000))).count() == 5   # no "too many SQL variables"
    assert M.filter(n=2.9).count() == 0                       # 2.9 must not match n == 2
    assert M.filter(n=2.0).count() == 1
