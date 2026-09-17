"""Regression tests for the 0.2.2 re-audit fixes (27 of 32; 5 documented limitations).

Grouped by subsystem. Compiler tests need `node` (skipped otherwise).
"""
from __future__ import annotations

import decimal
import io
import json
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from jongo import HTTPError, Jongo, ServerError, db, server
from jongo.compiler.bundle import Bundler
from jongo.errors import JongoError
from jongo.html import a, div, h, iframe, img
from jongo.vdom import render_to_string, to_json_data

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
HERE = Path(__file__).parent


def _run_js(fn, tmp_path):
    b = Bundler(roots=[HERE])
    js = b.resolve(fn.__name__, fn)
    p = tmp_path / "b.js"
    p.write_text(b.render(f"console.log(JSON.stringify({js}()));"))
    proc = subprocess.run([NODE, str(p)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ---- compiler (browser must match CPython) ----------------------------------------------

def _c2_bool_int_membership():
    return [True in {1, 2, 3}, {1: "x"}.get(True), 1 in {True: 9}, {1: "a"}.setdefault(True, "b")]

def _c5_bitwise_beyond_32bit():
    return [1 << 40, 0xFFFFFFFF & 0xFFFFFFFF, (1 << 33) | 1, 0xFF ^ 0x0F]

def _c7_int_parse_rejects_garbage():
    out = []
    for bad in ["1a", "42px", "0x1g"]:
        try:
            int(bad); out.append("BUG")
        except ValueError:
            out.append("ok")
    out.append(int("0b101", 2))
    out.append(int("1_000"))
    return out

def _c9_float_parse():
    # inf/nan can't round-trip through JSON, so assert the parse worked via properties.
    return [float("inf") > 1e308, float("-inf") < -1e308, float("1_000"), float("nan") != float("nan")]

def _c10_bool_times_list():
    return [True * [1, 2], False * [1, 2], [1] * True]

def _c12_format_spec():
    return [f"{255:#06x}", f"{8:#o}", f"{5:#b}", f"{65:c}"]


@needs_node
@pytest.mark.parametrize("fn", [
    _c2_bool_int_membership, _c5_bitwise_beyond_32bit, _c7_int_parse_rejects_garbage,
    _c9_float_parse, _c10_bool_times_list, _c12_format_spec,
])
def test_compiler_022_matches_python(fn, tmp_path):
    assert _run_js(fn, tmp_path) == json.loads(json.dumps(fn()))


# ---- vdom -------------------------------------------------------------------------------

def test_v1_srcdoc_is_dropped():
    assert "srcdoc" not in render_to_string(iframe(srcdoc="<script>steal()</script>"))

def test_v2_on_handler_attribute_names_dropped():
    for name in ("onerror", "onfocus", "OnClick"):
        assert name.lower() not in render_to_string(img(src="x", **{name: "steal()"})).lower()

def test_v3_decimal_nonfinite_becomes_null():
    assert to_json_data(decimal.Decimal("NaN")) is None
    assert to_json_data(decimal.Decimal("Infinity")) is None
    assert to_json_data(decimal.Decimal("1.5")) == 1.5

def test_v4_data_texthtml_blocked_image_allowed():
    assert "data:text/html" not in render_to_string(iframe(src="data:text/html,<script>x</script>"))
    assert "data:image/png" in render_to_string(img(src="data:image/png;base64,AAAA"))

def test_v5_sentinel_element_prop_name_is_inert():
    payload = {"t": "img", "p": {"src": "x", "onerror": "steal()"}}
    html = render_to_string(div(**{"$v": payload}))
    assert "<img" not in html  # revived as data, not a live element

def test_v6_uppercase_void_and_rawtext_tags():
    # An uppercase SCRIPT tag must get raw-text handling: the child's </SCRIPT is
    # neutralized (</ -> <\/) so it can't break out, rather than HTML-escaped like a normal element.
    html = render_to_string(h("SCRIPT", "</SCRIPT ><img>"))
    assert "<\\/SCRIPT" in html and "&lt;" not in html

def test_v7_colliding_dict_keys_raise():
    with pytest.raises(JongoError):
        to_json_data({1: "a", "1": "b"})  # distinct keys, same JSON string


# ---- http -------------------------------------------------------------------------------

def _app():
    return Jongo("h_app", secret_key="s")

def test_h1_negative_content_length_rejected():
    from jongo.http import Request
    env = {"REQUEST_METHOD": "POST", "PATH_INFO": "/", "QUERY_STRING": "",
           "CONTENT_LENGTH": "-1", "wsgi.input": io.BytesIO(b"x" * 1000)}
    with pytest.raises(HTTPError):
        _ = Request(env).body

def test_h2_after_request_error_becomes_500_not_crash():
    app = _app()

    @app.get("/x")
    def x():
        return "ok"

    @app.after_request
    def boom(request, response):
        raise RuntimeError("hook blew up")

    assert app.test_client().get("/x").status == 500  # caught, not escaped to WSGI

def test_h3_x_forwarded_proto_needs_trust_proxy():
    untrusting = Jongo("np", secret_key="s")
    trusting = Jongo("tp", secret_key="s", trust_proxy=True)
    from jongo.http import Request
    env = {"REQUEST_METHOD": "GET", "PATH_INFO": "/", "HTTP_X_FORWARDED_PROTO": "http",
           "wsgi.url_scheme": "https"}
    assert Request(env, untrusting).scheme == "https"   # spoof ignored
    assert Request(env, trusting).scheme == "http"       # honored only when opted in

def test_h5_crlf_stripped_from_headers():
    from jongo.http import redirect
    resp = redirect("/next\r\nSet-Cookie: evil=1")
    captured = {}
    resp({"REQUEST_METHOD": "GET"}, lambda s, hs, e=None: captured.update(headers=hs))
    for name, value in captured["headers"]:
        assert "\r" not in value and "\n" not in value

def test_h6_set_cookie_bad_path_clean_error():
    from jongo.http import Response
    with pytest.raises(ValueError):
        Response().set_cookie("x", "y", path="/bad\r\npath")


# ---- rpc / orm --------------------------------------------------------------------------

@pytest.fixture
def memdb():
    db.configure(":memory:")
    yield
    db.close_connections()

def test_o1_naive_datetime_round_trips_type_consistent(memdb):
    class Ev(db.Model):
        when = db.DateTime()

    db.migrate()
    naive = datetime(2024, 1, 2, 3, 4, 5)
    Ev.create(when=naive)
    back = Ev.all()[0].when
    assert back == naive and back.tzinfo is None      # regression fixed: same kind
    assert (back < naive) is False                     # no TypeError

def test_o1_ordering_still_correct_across_offsets(memdb):
    class Ev(db.Model):
        when = db.DateTime()

    db.migrate()
    later = datetime(2024, 1, 1, 23, 0, tzinfo=timezone(timedelta(hours=-5)))  # 04:00Z
    earlier = datetime(2024, 1, 2, 1, 0, tzinfo=timezone.utc)                  # 01:00Z
    Ev.create(when=later)
    Ev.create(when=earlier)
    order = [e.when for e in Ev.order_by("when")]
    assert order[0].replace(tzinfo=timezone.utc) <= order[1].replace(tzinfo=timezone.utc)

def test_o2_bad_fk_raises_validationerror_not_integrityerror(memdb):
    class Author(db.Model):
        name = db.Text()

    class Book(db.Model):
        author = db.ForeignKey(Author)

    db.migrate()
    with pytest.raises(db.ValidationError):
        Book.create(author_id=99999)  # nonexistent FK

def test_o3_update_unique_violation_is_validationerror(memdb):
    class Tag(db.Model):
        name = db.Text(unique=True)

    db.migrate()
    Tag.create(name="a")
    b = Tag.create(name="b")
    with pytest.raises(db.ValidationError):
        Tag.filter(id=b.id).update(name="a")

def test_o4_rpc_dict_coerces_keys(memdb):
    from typing import Dict
    from jongo.rpc import coerce
    out = coerce({"1": "x", "2": "y"}, Dict[int, str], "d")
    assert set(out.keys()) == {1, 2} and out[1] == "x"

def test_o7_rpc_int_number_is_bounded():
    from jongo.rpc import coerce
    assert coerce(5, int, "n") == 5
    for big in (10 ** 30, 2 ** 63, 1e30):
        with pytest.raises(HTTPError):
            coerce(big, int, "n")
