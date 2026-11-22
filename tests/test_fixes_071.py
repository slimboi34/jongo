"""Regression tests for 0.7.1: the defects the machine-driven test of 0.7.0 found.

Multipart header parsing (linear, and the codec cache), floats in ``__in``, a lone ``]`` in
``portable_pattern``, ``Choice`` with an integral float, and browser builtins that now raise
what CPython raises. The randomised and tabled parity checks are in test_properties_071.py.
"""
from __future__ import annotations

import encodings
import io
import time

import pytest

from jongo import db, forms
from jongo.db import connection
from jongo.db.dialect import translate
from jongo.http import Request

from conftest import use_test_database

# ------------------------------------------------------------ multipart part headers --


def _multipart_request(heads, data=b"value", content_type="multipart/form-data; boundary=B"):
    body = b"".join(b"--B\r\n" + head + b"\r\n\r\n" + data + b"\r\n" for head in heads) + b"--B--\r\n"
    return Request({"REQUEST_METHOD": "POST", "PATH_INFO": "/form", "CONTENT_TYPE": content_type,
                    "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)})


def _parse_seconds(head: bytes, parts: int = 8, repeats: int = 5) -> float:
    """The best of ``repeats`` timings of parsing a form of ``parts`` parts with this header."""
    best = float("inf")
    for _ in range(repeats):
        request = _multipart_request([head] * parts)
        start = time.perf_counter()
        try:
            request.form
        except Exception:  # noqa: BLE001 - a quick refusal is a fine outcome; only the cost matters
            pass
        best = min(best, time.perf_counter() - start)
    return best


CD = b'Content-Disposition: form-data; name="f"'
#: Header shapes, by size in bytes. The first is 0.7.0's attack (super-linear in email's
#: parameter splitter: ~2 s for one 14 KB header); the rest are accepted and must stay linear.
SHAPES = {
    "unquoted ;\" pairs": lambda n: CD + b';"' * (n // 2),
    "semicolons in a quoted filename": lambda n: CD + b'; filename="' + b";" * n + b'"',
    "escaped quotes in a quoted filename": lambda n: CD + b'; filename="' + b'\\";' * (n // 3) + b'"',
    "a long token": lambda n: CD + b"; filename=" + b"a" * n,
    "folded lines": lambda n: CD + b";\r\n " + b"x" * n,
}


@pytest.mark.parametrize("shape", SHAPES, ids=list(SHAPES))
def test_part_header_parse_scales_linearly(shape):
    # 14x the header: linear work grows ~14x, the 0.7.0 parse grew ~100x. The ratio, not a
    # wall-clock figure, is asserted (with best-of-5 timings and a small floor for noise).
    make = SHAPES[shape]
    small, large = _parse_seconds(make(1000)), _parse_seconds(make(14000))
    assert large <= 42 * small + 0.002, (shape, small, large)


def test_the_04_attack_header_is_refused_as_malformed():
    request = _multipart_request([CD + b';"' * 7000])
    with pytest.raises(Exception) as info:
        request.form
    assert info.value.status == 400 and info.value.code == "J212"


@pytest.mark.parametrize("head,ok", [
    (CD + b"".join(b"; x%d=1" % i for i in range(63)), True),   # 64 parameters with name=
    (CD + b"".join(b"; x%d=1" % i for i in range(64)), False),
    (CD + b";" * 64, True),                                      # empty ones count too
    (CD + b";" * 65, False),
    (b"\r\n".join([CD] + [b"X-A: 1"] * 63), True),               # 64 header lines
    (b"\r\n".join([CD] + [b"X-A: 1"] * 64), False),
])
def test_part_header_caps(head, ok):
    request = _multipart_request([head])
    if ok:
        assert request.form == {"f": "value"}
    else:
        with pytest.raises(Exception) as info:
            request.form
        assert info.value.status == 400


@pytest.mark.parametrize("head,expected", [
    # (header lines, (field name, filename or None))
    ([CD], ("f", None)),
    ([b"Content-Disposition: form-data; name=f"], ("f", None)),
    ([b'Content-Disposition: form-data; name="a\\"b"'], ('a"b', None)),
    ([b'Content-Disposition: form-data; name="a\\\\b"'], ("a\\b", None)),
    ([b'Content-Disposition: form-data; name = "spaced"'], ("spaced", None)),
    ([b'CONTENT-DISPOSITION: FORM-DATA; NAME="up"; FILENAME="B.TXT"'], ("up", "B.TXT")),
    ([CD + b'; filename=""'], ("f", None)),                       # an empty file input: a text field
    ([CD + b'; filename="a;b.txt"'], ("f", "a;b.txt")),
    ([CD + b'; filename="  spaced.txt  "'], ("f", "spaced.txt")),
    ([CD + b'; filename="\xc3\xa9t\xc3\xa9.txt"'], ("f", "été.txt")),   # raw UTF-8, as browsers send
    ([CD + b"; filename*=UTF-8''%E2%82%AC%20rates.txt"], ("f", "€ rates.txt")),
    ([CD + b"; filename*=iso-8859-1'en'%E9t%E9.txt"], ("f", "été.txt")),
    ([CD + b"; filename*0*=UTF-8''%C3; filename*1*=%A9.txt"], ("f", "é.txt")),  # split mid-character
    ([CD + b'; filename*0="long"; filename*1="name.txt"'], ("f", "longname.txt")),
    ([CD + b"; filename=\"fallback.txt\"; filename*=UTF-8''%C3%A9.txt"], ("f", "é.txt")),  # RFC 6266
    ([b"Content-Disposition: form-data; name*=UTF-8''f%C3%A9"], ("fé", None)),
    ([CD + b'; filename="=?utf-8?b?w6l0w6kudHh0?="'], ("f", "été.txt")),   # RFC 2047 (Spring)
    ([CD + b'; filename="=?utf-8?q?a_b=C3=A9?= =?utf-8?q?.txt?="'], ("f", "a bé.txt")),
    ([CD, b'Content-Type: image/png; name="x.png"'], ("f", "x.png")),  # as email's get_filename()
    ([b"Content-Disposition: form-data;", b' name="folded"'], ("folded", None)),
    ([CD, b'Content-Disposition: form-data; name="second"'], ("f", None)),  # the first header wins
    ([b"X-Other: 1", CD], ("f", None)),
    ([b"Not a header line", CD], (None, None)),               # the header block ended there
])
def test_part_header_values(head, expected):
    request = _multipart_request([b"\r\n".join(head)])
    name, filename = expected
    if name is None:
        assert request.form == {} and request.files == {}
    elif filename is None:
        assert request.form == {name: "value"} and request.files == {}
    else:
        assert request.form == {} and request.files[name].filename == filename


@pytest.mark.parametrize("content_type,charset_value", [
    (b"Content-Type: text/plain; charset=latin-1", "café"),
    (b'Content-Type: text/plain; charset="ISO-8859-1"', "café"),
    (b"Content-Type: text/plain; charset=punycode", "caf�"),      # quadratic decoder: read as UTF-8
    (b"Content-Type: text/plain; charset=base64", "caf�"),        # not a text encoding
    (b"Content-Type: text/plain; charset=no-such-codec", "caf�"),
])
def test_part_charset(content_type, charset_value):
    request = _multipart_request([b"\r\n".join([CD, content_type])], data=b"caf\xe9")
    assert request.form["f"] == charset_value


def test_unknown_charset_names_do_not_grow_the_codec_cache():
    # codecs.lookup() makes the encodings package cache every name it can't find, forever;
    # a form naming a fresh charset per part grew the server by its header bytes per request.
    names = [f"made-up-charset-{i}" for i in range(50)]
    heads = [CD.replace(b'"f"', b'"f%d"' % i) + b"\r\nContent-Type: text/plain; charset=" + n.encode()
             for i, n in enumerate(names)]
    request = _multipart_request(heads)
    assert len(request.form) == 50
    assert not {encodings.normalize_encoding(n.lower()) for n in names} & set(encodings._cache)


@pytest.mark.parametrize("content_type,boundary", [
    ("multipart/form-data; boundary=B", b"B"),
    ('multipart/form-data; boundary="quoted ; boundary"', b"quoted ; boundary"),
    ("multipart/form-data; charset=utf-8; BOUNDARY=x-y", b"x-y"),
    ("multipart/form-data" + "; x=1" * 70 + "; boundary=B", None),   # past the parameter cap
    ("multipart/form-data; boundary=", None),
])
def test_multipart_boundary(content_type, boundary):
    from jongo.http import _multipart_boundary

    assert _multipart_boundary(content_type) == boundary


@pytest.mark.parametrize("filler", ['; x="' + ";" * 10 + '"', ';"'], ids=["quoted semicolons", "unquoted ;\" pairs"])
def test_request_content_type_boundary_parse_is_linear(filler):
    from jongo.http import _multipart_boundary

    def seconds(n):
        value = "multipart/form-data; boundary=B" + filler * (n // len(filler))
        best = float("inf")
        for _ in range(5):
            start = time.perf_counter()
            _multipart_boundary(value)
            best = min(best, time.perf_counter() - start)
        return best

    small, large = seconds(4000), seconds(56000)
    assert large <= 42 * small + 0.002, (small, large)


# --------------------------------------------------------------- floats in __in --


@pytest.fixture
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


FLOATS = [-2.901816873243615e-258, 1e-300, 1.23456789e-290, 6.022e-280, -3.5e-310, 5e-324, 0.1, 1 / 3,
          -0.0, 1.7976931348623157e308, 123456789.123456789]


def test_float_in_matches_exact(memory_db):
    class Reading(db.Model):
        value = db.Float(null=True)

    db.migrate()
    for v in FLOATS:
        Reading.create(value=v)
    for v in FLOATS:
        via_in = sorted(r.id for r in Reading.filter(value__in=[v]))
        via_exact = sorted(r.id for r in Reading.filter(value=v))
        assert via_in == via_exact and via_in, v
    assert Reading.filter(value__in=FLOATS).count() == len(FLOATS)
    assert Reading.exclude(value__in=FLOATS[:3]).count() == len(FLOATS) - 3


def test_in_binds_floats_and_still_inlines_ints_and_strings(memory_db):
    from jongo.db.query import compile_lookup

    class Item(db.Model):
        n = db.Int(null=True)
        price = db.Float(null=True)
        tag = db.Text(null=True)

    db.migrate()
    sql, params = compile_lookup(Item, "price__in", [1e-300, 2.5])
    assert sql.count("?") == 2 and params == [1e-300, 2.5]
    sql, params = compile_lookup(Item, "tag__in", ["50%", "a?b", None])
    assert "'50%'" in sql and "'a?b'" in sql and params == []
    # A huge int list stays inlined: bound, it would pass SQLite's variable limit (32,766).
    Item.create(n=39_999)
    assert Item.filter(n__in=list(range(40_000))).count() == 1


def test_inlined_literals_beside_bound_floats_translate_for_postgres():
    # A list that mixes inlined literals and bound values must keep both right on psycopg's
    # %s paramstyle: ? inside a literal is text, % inside it is doubled, and each ? is a %s.
    sql = '"tag" IN (\'50%\', \'a?b\', ?)'
    assert translate(sql, True, "%s") == '"tag" IN (\'50%%\', \'a?b\', %s)'


# --------------------------------------------------------------------- forms --


@pytest.mark.parametrize("pattern,portable", [
    (r"[A-Z]+]", r"[A-Z]+\]"),
    (r"a]", r"a\]"),
    (r"(a|b])", r"(a|b\])"),
    (r"[]a]", r"[\]a]"),      # a leading ] in a class was already escaped
    (r"\]", r"\]"),
])
def test_portable_pattern_escapes_a_lone_closing_bracket(pattern, portable):
    assert forms.portable_pattern(pattern) == portable


def test_choice_reads_an_integral_float_as_its_int():
    rule = forms.Schema("S", c=forms.Choice([2, "a", 0]))["c"]
    for value in (2, 2.0, "2", " 2 ", 0, -0.0, 0.0):
        assert forms.check(rule, value) == "", value
    for value in (2.5, "2.0", 3, float("inf"), float("nan"), True):
        assert forms.check(rule, value) == rule["messages"]["choices"], value
    schema = forms.Schema("S", level=forms.Choice([(1, "Low"), (2, "High")]))
    assert forms.validate(schema, {"level": 2.0}) == {}
    assert schema.clean({"level": 2.0}) == {"level": 2}
