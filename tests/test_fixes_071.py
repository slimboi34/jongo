"""Regression tests for 0.7.1: the defects the machine-driven test of 0.7.0 found.

Multipart header parsing (linear, and the codec cache), floats in ``__in``, a lone ``]`` in
``portable_pattern``, ``Choice`` with an integral float, and browser builtins that now raise
what CPython raises. The randomised and tabled parity checks are in test_properties_071.py.
"""
from __future__ import annotations

import encodings
import io
import time
from pathlib import Path

import pytest

from jongo import db, forms
from jongo.compiler.bundle import Bundler
from jongo.db import connection
from jongo.db.dialect import translate
from jongo.errors import CompileError, JongoError
from jongo.http import Request

from conftest import use_test_database
from test_compiler import needs_node, normalise, run_in_node

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
    Item.create(n=39_999, price=39_999.5)
    assert Item.filter(n__in=list(range(40_000))).count() == 1
    # So does a huge float list, past MAX_BOUND_IN (as in 0.7.0), rather than fail.
    assert Item.filter(price__in=[i + 0.5 for i in range(40_000)]).count() == 1
    from jongo.db.query import MAX_BOUND_IN

    sql, params = compile_lookup(Item, "price__in", [i + 0.5 for i in range(MAX_BOUND_IN + 1)])
    assert params == [] and "10000.5" in sql


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


@pytest.mark.parametrize("pattern", [r"(?!$)(?!$){2}", r"((?=\n)+?)", r"(?<!a)?b", r"(?=a)*", r"\B", r"a\Bb"])
def test_portable_pattern_refuses_what_javascript_reads_differently(pattern):
    # A repeated lookaround is a SyntaxError in a u-mode RegExp; \B matches an empty value in
    # JavaScript and not in Python. Both used to pass portable_pattern().
    with pytest.raises(JongoError) as info:
        forms.portable_pattern(pattern)
    assert info.value.code == "J115"


@pytest.mark.parametrize("pattern", [r"(?=a)b", r"(?!x)[a-z]+", r"(a)?", r"(?:ab)+", r"a\b", r"[(?=a)]+"])
def test_portable_pattern_still_accepts_plain_groups_and_assertions(pattern):
    assert forms.portable_pattern(pattern) == pattern


def test_choice_reads_an_integral_float_as_its_int():
    rule = forms.Schema("S", c=forms.Choice([2, "a", 0]))["c"]
    for value in (2, 2.0, "2", " 2 ", 0, -0.0, 0.0):
        assert forms.check(rule, value) == "", value
    for value in (2.5, "2.0", 3, float("inf"), float("nan"), True):
        assert forms.check(rule, value) == rule["messages"]["choices"], value
    schema = forms.Schema("S", level=forms.Choice([(1, "Low"), (2, "High")]))
    assert forms.validate(schema, {"level": 2.0}) == {}
    assert schema.clean({"level": 2.0}) == {"level": 2}


# ------------------------------------------------------- compiler / browser parity --


def s_builtin_errors_are_pythons():
    """Which except clause catches each failure: the browser must raise the same type."""
    out = []
    for f in (lambda: chr(-1), lambda: chr(0x110000), lambda: chr(1.5),
              lambda: ord("ab"), lambda: ord(""), lambda: ord(5),
              lambda: abs("a"), lambda: abs([1, 2]), lambda: abs(None),
              lambda: 8 >> -1, lambda: 8 << -1, lambda: round(float("inf")), lambda: round(float("-inf")),
              lambda: round(float("nan")), lambda: round("a"), lambda: round(2.5, 1.5),
              lambda: "a\tb".expandtabs(2.5)):
        try:
            f()
            out.append("no error")
        except ValueError:
            out.append("ValueError")
        except OverflowError:
            out.append("OverflowError")
        except TypeError:
            out.append("TypeError")
        except IndexError:
            out.append("IndexError")
    return out


def s_builtins_still_work():
    tab = "\t"
    return [chr(65), chr(0x1F600) == "\U0001F600", ord("\U0001F600"), ord("a"), abs(-2.5), abs(True), abs(-7),
            8 >> 1, 1 << 40, 5 >> 0, round(2.5, ndigits=1), round(2.675, ndigits=2), round(1234.5, ndigits=-1),
            round(float("inf"), 1) == float("inf"), round(-0.5), round(1.5),
            "a\tb".expandtabs(), "a\tb\ncd\te".expandtabs(4), "ab\rc\td".expandtabs(tabsize=3),
            (tab + "x").expandtabs(0), (tab + "x").expandtabs(-1), "\U0001F600\tx".expandtabs(4), "".expandtabs()]


def s_operators_on_wrong_types_raise():
    """+, -, *, **, unary -/+, slicing, ** unpacking and subscripting on the wrong types raise
    TypeError, as in CPython, instead of JavaScript's coercion ("1,2" + 0, "a" - 1 is NaN,
    [1, 2][1.5:], 5["valueOf"])."""
    a, items, none = "a", [1, 2], None

    def take(**kw):
        return kw

    def set_item(target):
        target[0] = 1

    def del_item(target):
        del target[0]

    five, yes, pair = 5, True, {1, 2}
    out = []
    for f in (lambda: a + 1, lambda: 1 + a, lambda: items + 0, lambda: none + items, lambda: (1,) + items,
              lambda: a - a, lambda: items - 1, lambda: a * a, lambda: items * 2.5, lambda: none * 2,
              lambda: 2 * none, lambda: -a, lambda: +items, lambda: -none, lambda: a ** 2, lambda: items ** 2,
              lambda: items[1.5:], lambda: "abc"[::"x"], lambda: items[:none or "1"], lambda: take(**items),
              lambda: take(**a), lambda: {**items}, lambda: 2 + "a", lambda: "n=" + 1, lambda: five["valueOf"],
              lambda: yes[0], lambda: pair[0], lambda: set_item(pair), lambda: del_item(five)):
        try:
            f()
            out.append("no error")
        except TypeError:
            out.append("TypeError")
    return out


def s_operators_on_right_types_still_work():
    n, t, items = 2, True, [1, 2, 3]

    def take(**kw):
        return sorted(kw.items())

    return ["a" + "b", [1] + [2], repr((1,) + (2,)), 2 * "ab", "ab" * 2, t * [1], [0] * 3, -n, +n, -t, +t,
            n ** 2, 2 ** -1, n ** 0.5 > 1.41, items[t:], items[::-1], items[1:None], "abc"[::2],
            {**{"a": 1}, "b": 2}, take(**{"k": 1}), n - t, 1.5 * n, f"{n}" + "!", -(-n)]


SAMPLES = {name: fn for name, fn in dict(globals()).items() if name.startswith("s_")}


@needs_node
def test_builtins_match_cpython(tmp_path):
    results = run_in_node(SAMPLES, tmp_path)
    for name, fn in SAMPLES.items():
        assert results[name].get("ok") == normalise(fn()), (name, results[name])


def chr_past_the_c_int_range():
    out = []
    for n in (2 ** 31, -(2 ** 31) - 1):
        try:
            chr(n)
            out.append("no error")
        except ValueError:
            out.append("ValueError")
        except OverflowError:
            out.append("OverflowError")
    return out


@needs_node
def test_chr_past_the_c_int_range_follows_python_3_13(tmp_path):
    # CPython changed here: 3.10-3.12 raise OverflowError, 3.13+ ValueError. The browser follows 3.13+.
    results = run_in_node({"chr_past_the_c_int_range": chr_past_the_c_int_range}, tmp_path)
    assert results["chr_past_the_c_int_range"].get("ok") == ["ValueError", "ValueError"], results


def literal_str_method():
    return "abc".encode()


def literal_fstring_method(x):
    return f"{x}".format_map({})


def literal_list_method():
    return [3, 1].bit_length()


def literal_dict_method():
    return {"a": 1}.fromkeys(["b"])


def literal_attribute_read():
    return "abc".translate


@pytest.mark.parametrize("fn", [literal_str_method, literal_fstring_method, literal_list_method,
                                literal_dict_method, literal_attribute_read], ids=lambda f: f.__name__)
def test_a_missing_method_on_a_literal_is_a_compile_error(fn):
    with pytest.raises(CompileError) as info:
        Bundler(roots=[Path(__file__).parent]).resolve(fn.__name__, fn)
    assert info.value.code == "J103"


def literal_methods_that_exist():
    return [" a ".strip(), ",".join(["a", "b"]), "{}!".format(1), [1, 2].index(2), {"a": 1}.get("a"),
            {1, 2}.union({3}), (1, 2).count(1), "a\tb".expandtabs(2), [x for x in "ab"].copy()]


def test_supported_methods_on_literals_still_compile():
    Bundler(roots=[Path(__file__).parent]).resolve(
        "literal_methods_that_exist", literal_methods_that_exist)


def dict_mutation_limit_sample():
    """guide §18: the browser loops over the keys a dict had when the loop began, and doesn't
    raise RuntimeError when the loop changes them (CPython does). Pinned so a change is seen."""
    d = {"a": 1}
    seen = []
    for k in d:
        seen.append(k)
        d[k + "x"] = 1
    return seen


@needs_node
def test_documented_dict_mutation_limit(tmp_path):
    with pytest.raises(RuntimeError):
        dict_mutation_limit_sample()
    results = run_in_node({"dict_mutation_limit_sample": dict_mutation_limit_sample}, tmp_path)
    assert results["dict_mutation_limit_sample"] == {"ok": ["a"]}
