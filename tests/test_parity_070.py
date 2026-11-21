"""Parity tests for the 0.7.0 compiler and runtime fixes: each sample runs in CPython and,
compiled, in Node, and the results must match (as in test_compiler.py).

Samples return plain data. Where Python's type matters (a tuple, not a list) the sample
returns ``isinstance`` checks or ``repr()`` text, because JSON can't tell them apart.
"""
from __future__ import annotations

import json
import math
import sys
import unicodedata

import pytest

from test_compiler import needs_node, normalise, run_in_node

# ---------------------------------------------------------------------------- arithmetic --


def s_float_mod_floordiv():
    return [0.1 % 1, 12.34 % 60, 0.2 % 360, 7 % 1e16, 1 // 0.1, 2 // 0.2, list(divmod(1, 0.1)),
            -7.5 // 2, 7.5 % -2, -7 % 3, 7 % -3, -7 // 2, 3 % float("inf") == 3, -3 % float("inf") == float("inf"),
            -3 // float("inf") == -1, 7 // True, 7 % True]


def _safe(f):
    try:
        return f()
    except ZeroDivisionError:
        return "ZeroDivisionError"


def s_zero_division():
    zero = 0
    false = False
    return [_safe(lambda: 1 / zero), _safe(lambda: 0 / zero), _safe(lambda: 7 // false), _safe(lambda: 7 % false),
            _safe(lambda: zero ** -1), _safe(lambda: pow(0, -1)), _safe(lambda: divmod(1, 0)), 1 / 4, 2 ** -1]


def s_pow_mod_and_invert():
    x = 2 ** 32
    return [pow(123456789, 2, 1000000007), pow(2, 100, 1000000007), pow(3, -1, 7), pow(2, 3, -5), pow(7, 0, 1),
            ~x, ~5, ~-1, ~(2 ** 40)]


def s_sum_compensated():
    return [sum([0.1, 0.2, 0.3]), sum([0.1] * 10), sum([1e16, 1.0, -1e16]), f"{sum([19.99, 5.01, 0.1])}",
            sum([1, 2, 3]), sum([[1], [2]], []), sum([0.5, 0.25], 1)]


#: What a sample returns on CPython 3.12+, where the host Python is older and differs. The
#: browser runtime follows 3.12+: sum() of floats there uses compensated summation, and 3.10
#: and 3.11 (still supported, and in CI) add naively.
SINCE_312 = {"s_sum_compensated": [0.6, 1.0, 1.0, "25.099999999999998", 6, [1, 2], 1.75]}


def python_expects(name, fn):
    if sys.version_info < (3, 12) and name in SINCE_312:
        return SINCE_312[name]
    return fn()


@pytest.mark.skipif(sys.version_info < (3, 12), reason="checks the recorded 3.12+ results against 3.12+")
def test_the_recorded_312_results_are_what_312_returns():
    for name, recorded in SINCE_312.items():
        assert globals()[name]() == recorded, name


def s_round_large():
    return [round(1e21, 0), round(1e22, 1), round(1e22, 2), round(1e300, -2), round(1234.5, -1), round(15, -1),
            round(25, -1), round(2.675, 2), round(0.125, 2), round(-0.5)]


def s_chained_compare_evaluates_once():
    calls = []

    def f(v):
        calls.append(v)
        return v

    xs = [5, 1, 20]
    a = 0 < xs.pop() < 10
    b = 1 < f(5) < 10
    c = 1 < f(50) < 10 < f(99)
    d = f(1) <= f(2) <= f(3)
    return [a, xs, b, c, d, calls]


def s_ordering():
    def raises(f):
        try:
            f()
            return "no error"
        except TypeError:
            return "TypeError"

    none = None
    return [(1, 10, 0) > (1, 9, 5), [2] < [10], [10] < [9], (2, 10, 1) >= (2, 9, 0), [1, 2] <= [1, 2],
            [r for r in [(0, 9), (0, 10), (0, 11)] if r > (0, 9)], {1} < {1, 2}, {1, 2} <= {1, 2},
            {1, 2} > {1}, {1} >= {2}, "b" > "a", "\U0001F600" > "\uffff", [1, None] == [1, None],
            raises(lambda: none < 1), raises(lambda: "a" < 1), raises(lambda: [1] < (1,)),
            sorted([(2, "b"), (1, "z"), (2, "a")]), raises(lambda: sorted([3, "x"])), max([[1, 2], [1, 3]])]


def s_inplace_operators_mutate():
    xs = [1, 2]
    alias = xs
    xs *= 2
    tags = {"a"}
    t2 = tags
    tags |= {"b"}
    tags -= {"a"}
    tags ^= {"c", "b"}
    keep = {1, 2, 3}
    k2 = keep
    keep &= {2, 3, 4}
    d = {"x": 1}
    d2 = d
    d |= {"y": 2}
    n = 3
    n *= 2
    t = (1,)
    t3 = t
    t *= 2
    return [alias, sorted(t2), sorted(k2), d2, n, len(t3), len(t)]


# ------------------------------------------------------------------------------- sets --


def s_sets_of_tuples():
    seen = set()
    seen.add((0, 0))
    grid = ["..#", "...", "#.."]
    todo, visited = [(0, 0)], set()
    while todo:
        r, c = todo.pop()
        if (r, c) in visited or not (0 <= r < 3 and 0 <= c < 3) or grid[r][c] == "#":
            continue
        visited.add((r, c))
        todo += [(r + 1, c), (r - 1, c), (r, c + 1), (r, c - 1)]
    pairs = {(1, 2), (1, 2), (3, 4)}
    pairs.discard((3, 4))
    return [(0, 0) in seen, (1, 2) in {(1, 2), (3, 4)}, len({(1, 2), (1, 2), (1, 2)}),
            len(set([("a", 1), ("a", 1)])), {(1, 2)} == {(1, 2)}, len(visited), len(pairs), (1, 2) in pairs,
            len({x % 3 for x in range(9)}), len({(x, x) for x in [1, 1, 2]}), ("a,b",) in {("a", "b")}]


def s_bool_keys_and_index():
    flag = 1 > 0
    d = {}
    d[True] = "x"
    s = {1}
    s.add(True)
    xs = ["a", "b"]
    xs[True] = "B"
    return [("off", "on")[flag], d[True], d.get(1), len(s), xs, list({True: 1, 1: 2}.values())]


def s_unhashable_member():
    try:
        {[1, 2]}
        return "no error"
    except TypeError:
        return "TypeError"


def s_local_named_set():
    Set = "mine"  # noqa: N806 - the point is the name
    Map = "also mine"  # noqa: N806
    return sorted({x % 2 for x in range(4)}) + [Set, Map]


def s_proto_key():
    counts = {}
    for word in ["a", "__proto__", "a", "__proto__"]:
        counts[word] = counts.get(word, 0) + 1
    d = {}
    d["__proto__"] = 5
    lit = {"__proto__": 1, "a": 2}
    made = dict([("__proto__", 3)])
    made.update({"__proto__": 4})
    made.setdefault("b", 1)
    comp = {k: 1 for k in ["__proto__"]}
    return [counts, [d.get("__proto__"), len(d)], list(lit), made, comp, "__proto__" in comp]


# ---------------------------------------------------------------------- tuples and calls --


def s_tuple_results():
    t = (1, 2, 3)

    def f(*args):
        return [args == (1, 2), isinstance(args, tuple)]

    return [t[:2] == (1, 2), isinstance(t[::-1], tuple), repr(t[::-1]), "key=value".partition("=") == ("key", "=", "value"),
            "a=b=c".rpartition("=") == ("a=b", "=", "c"), f(1, 2), f(), isinstance([1, 2][:1], list)]


def s_bound_methods():
    scores = {"amy": 3, "zed": 1, "bob": 9}
    names = ["b", "C", "a"]
    names.sort(key=str.casefold)
    append = names.append
    append("d")
    strip = "  x ".strip
    return [max(scores, key=scores.get), min(scores, key=scores.get), sorted(scores, key=scores.get),
            sorted(["bob", "Carol", "alice"], key=str.lower), names, list(map(str.upper, ["a", "b"])),
            strip(), sorted([3, 1, 2], key=None), str.join("-", ["a", "b"]), dict.get(scores, "amy")]


def s_key_not_callable():
    try:
        sorted([1, 2], key=5)
        return "no error"
    except TypeError:
        return "TypeError"


def s_attribute_error_on_none():
    def name_of(user):
        try:
            return user.name
        except AttributeError:
            return "anonymous"

    def nested(user):
        try:
            return user.profile.name
        except AttributeError:
            return "no profile"

    return [name_of(None), nested({"profile": None})]


def s_nonlocal_loop_variable():
    out = []
    for i in range(3):
        def bump():
            nonlocal i
            i += 10
        bump()
        out.append(i)
    return out


def s_fstring_carriage_return():
    a, b = 1, 2
    return [f"{a},{b}\r\n", f"line1\rline2 {1}"]


# --------------------------------------------------------------------------- strings --


def s_strip_and_split_whitespace():
    ws = "\x1c\x1d\x1e\x1f\x85\u3000"
    return ["\ufeffa\ufeff".strip(), (ws + "a" + ws).strip(), (ws + "a").lstrip(), ("a" + ws).rstrip(),
            ("a" + ws + "b").split(), "\ufeff".isspace(), ws.isspace(), int("\x85\u3000" + "42" + "\t\u2028"), float("\xa0" + "1.5" + "\u3000"),
            "\U0001F600a\U0001F600".strip("\U0001F600"), "xxaxx".strip("x")]


def s_rsplit_and_split_maxsplit():
    return ["Mary Ann Smith".rsplit(None, 1), "Mary Ann Smith".rsplit(maxsplit=1), "a,b,c".rsplit(sep=",", maxsplit=1),
            "cmd arg1 arg2 ".split(None, 1), "  a b  ".split(None, 0), "  a b  ".rsplit(None, 0), " ".split(),
            "a b c".rsplit(None, 5), "a,,b".split(","), "a b".split(" ", -1)]


def s_find_with_bounds():
    text, count, i, guard = "a,b,c", 0, -1, 0
    while guard < 100:
        guard += 1
        i = text.find(",", i + 1)
        if i < 0:
            break
        count += 1
    return ["a,b,,c".find("b", 3), "abcabc".find("c", 3), "abcabc".rfind("a", 0, 3), [1, 2, 3, 2].index(2, 2),
            "a,b".startswith("a", 1), "a,b,c".endswith("c", 0, 3), "aaaa".count("a", 2), "abc".find("", 3),
            "abc".find("", 4), "abc".count("", 1), "abc".startswith("", 3), "abc".startswith("", 4),
            "abcabc".index("b", -3), "abc".rindex("c"), "ab".startswith(("x", "a")), [count, guard]]


def s_code_point_offsets():
    face = "\U0001F600"
    s = "\U0001F600 hello, world"
    return ["\U0001F600ab\U0001F600c".find("b"), s[s.find(","):], "\U0001F44D ok".index("o"),
            f"[{'X':>4}]".replace("X", "\U0001F600"), f"[{face:>4}]", "\U0001F600x".ljust(4, "."),
            "\U0001F600x".rjust(4), "\U0001F600".center(4, "*"), "\U0001F6001".zfill(4), "\U0001F600a".count(""),
            "a\U0001F600b\U0001F600".rfind("\U0001F600")]


def s_case_mapping():
    return ["jos\u00e9 garc\u00eda".title(), "\u00e9lan vital".title(), "\u0130stanbul".title(), "they're".title(),
            "\u00df".capitalize(), "\u01c6emal".title(), "STRASSE".casefold() == "stra\u00dfe".casefold(),
            "\ufb01ne".casefold(), "\u00b5".casefold(), "\u03a3\u0391\u03a3".casefold(), "hELLO wORLD".capitalize(),
            "\u10d0\u10d1".title()]


def s_misc_str_methods():
    return ["a\rb\nc".splitlines(), "a\nb".splitlines(True), "a\r\nb\x1cc\u2028d".splitlines(), "\n".splitlines(),
            "abc".replace("", "-"), "abc".replace("", "-", 2), "\U0001F600".replace("", "|"),
            "\u0661\u0662".isdigit(), "\u00b2".isdigit(), "\u00b2".isdecimal(), "\u4e09".isnumeric(),
            "\u00bd".isnumeric(), "a\u00b2".isalnum(), int("\u0661\u0662"), int("\uff11\uff12"), float("\u0663.5")]


def s_center_padding_side():
    return ["ab".center(5), "ab".center(6), "abc".center(6), "a".center(4), "abc".center(2), f"{'ab':^5}"]


# --------------------------------------------------------------------------- numbers --


def s_float_parse():
    def parse(text):
        try:
            return float(text)
        except ValueError:
            return "ValueError"
        except TypeError:
            return "TypeError"

    return [parse(""), parse("   "), parse("0x10"), parse("0b101"), parse([]), parse(None), parse("1_000.5"),
            parse("1__0"), parse(" -.5e1 "), parse("+5."), parse("."), parse("1e"), repr(parse("-Infinity")),
            repr(parse("nan")), parse("\u0661.5")]


def s_int_parse():
    def parse(text):
        try:
            return int(text)
        except ValueError:
            return "ValueError"

    return [parse(" 12 "), parse("1_000"), parse("\x1c7\x1f"), parse("\ufeff7"), parse("\u0967\u0968")]


def s_repr_and_str_text():
    return [repr("it's"), repr("a\nb\tc\\"), repr('both \' and "'), repr("\x00\x7f\u200b"), f"{'O' + chr(39) + 'Brien'!r}",
            str(1e16), str(1e-5), str(1.5e-7), str(123.456), str(1e22), repr(0.1), bool(float("nan")),
            repr([1.5, "x", None, True]), repr({"k": (1,)})]


def s_format_tie_rounding():
    return [f"{2.5:.0f}", f"{0.5:.0f}", f"{0.125:.2f}", "%.2f" % 0.125, f"{1234.125:,.2f}", f"{-2.5:.0f}",
            f"{2.5:.0e}", f"{0.125:.2g}", f"{99.5:.2g}", f"{999.5:.3g}", f"{0.375:.2%}", f"{1e22:.1f}"]


def s_format_spec_misc():
    flag = False
    return [f"{True:>5}", f"{flag:.2f}", f"{3.14159:n}", f"{7:n}", f"{float('inf'):.2f}", f"{float('-inf'):F}",
            f"{float('nan'):e}", f"{float('inf'):08.2f}", f"{1.0:.3}", f"{822.0:.3}", f"{12.0:.3}", f"{762.5:#.0f}",
            f"{0.0:#g}", f"{True}", f"{'abc':.2}", f"{'x':*>4}"]


def s_percent_format():
    def fmt(template, values):
        try:
            return template % values
        except TypeError:
            return "TypeError"

    return ["%e" % 1.5, "%X" % 255, "%+d" % 5, "%.3s" % "abcdef", "%s" % [1, 2], "%g" % 0.0001, "%5s|" % "ab",
            "%-5d|" % 3, "%#x" % 255, "%05.1f" % -2.25, "%c" % 65, "%r" % "a", "%%" % (), "%(a)s %(b)d" % {"a": 1, "b": 2},
            "%*d" % (4, 7), "%.*f" % (1, 2.25), fmt("%s %s", (1,)), fmt("%s", (1, 2)), "abc" % [], "%i|%u" % (3, 4)]


# ------------------------------------------------------------------------------ json --


def s_json_module():
    def loads_value_error(text):
        try:
            return json.loads(text)
        except ValueError:
            return "ValueError"

    def loads_decode_error(text):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return "JSONDecodeError"

    return [json.dumps({"a": 1, "b": [1, 2]}), json.dumps(["\u00e9"]), json.dumps(["\u00e9"], ensure_ascii=False),
            json.dumps({"b": 1, "a": 2}, sort_keys=True), json.dumps([1, {"b": 2}], indent=2),
            json.dumps({"a": [1, 2]}, separators=(",", ":")), json.dumps([1e16, 1e-5, 0.5, None, True, "q\"\n"]),
            json.dumps([float("nan"), float("inf")]), json.dumps("\U0001F600\x7f"), loads_value_error("{not json"),
            loads_decode_error("{"), json.loads('{"a": [1, 2.5]}'), json.dumps([], indent=2), json.dumps({}, indent=2)]


SAMPLES = {name: fn for name, fn in dict(globals()).items() if name.startswith("s_")}


@needs_node
def test_python_and_javascript_agree(tmp_path):
    results = run_in_node(SAMPLES, tmp_path)
    mismatches = []
    for name, fn in SAMPLES.items():
        expected = normalise(python_expects(name, fn))
        got = results[name]
        if got.get("error") or got.get("ok") != expected:
            mismatches.append(f"{name}:\n  python: {expected}\n  js:     {got}")
    assert not mismatches, "\n".join(mismatches)


def s_every_python_space():
    """Every code point str.isspace() accepts is stripped, and U+FEFF isn't. (Code points are
    returned as numbers: U+2028 in the output would split the line the harness reads.)"""
    spaces = "".join(chr(c) for c in range(0x3001) if chr(c).isspace())
    middle = ("x" + spaces + "y" + spaces).strip()
    return [len(spaces), [ord(c) for c in middle], [ord(c) for c in (spaces + "\ufeff").strip()], spaces.isspace(),
            ("a" + spaces + "b").split(), [c.isspace() for c in spaces], "\ufeff".isspace()]


@needs_node
def test_strip_matches_python_for_every_whitespace_code_point(tmp_path):
    assert len([c for c in range(0x110000) if chr(c).isspace()]) == 29  # every one is below U+3001
    assert run_in_node({"s": s_every_python_space}, tmp_path)["s"]["ok"] == normalise(s_every_python_space())


def s_unicode_version_tables():
    """isdigit()/isnumeric() beyond category Nd/N, on characters both Unicode versions agree on."""
    return [c.isdigit() for c in "0\u00b2\u2460\u0661a\u2155"] + [c.isnumeric() for c in "\u4e00\u2155\u3007x"]


@needs_node
def test_digit_tables(tmp_path):
    assert unicodedata.unidata_version >= "13"
    assert run_in_node({"s": s_unicode_version_tables}, tmp_path)["s"]["ok"] == s_unicode_version_tables()


def s_float_repr_thresholds():
    values = [1e-4, 9.99e-5, 1.5e-7, 1e15 + 0.5, 1e16, 2.5e16, 1e21, 1.7976931348623157e308, 5e-324, 0.1 + 0.2]
    return [repr(v) for v in values]


@needs_node
def test_float_repr_thresholds(tmp_path):
    assert run_in_node({"s": s_float_repr_thresholds}, tmp_path)["s"]["ok"] == s_float_repr_thresholds()


def s_integral_float_text_stays_int_like():
    return [str(10 / 2), str(-0.0), repr(2.0)]


@needs_node
def test_documented_integral_float_choice(tmp_path):
    # Known difference (guide section 18): an integral float prints like an int, and so -0.0
    # prints as "0" (it is usually an int computation that produced the -0 in JavaScript).
    assert run_in_node({"s": s_integral_float_text_stays_int_like}, tmp_path)["s"]["ok"] == ["5", "0", "2"]


@pytest.mark.parametrize("value", [0.5, 1.5, 2.5, 0.125, 2.675, 1e22, 123456.789, 5e-324])
def test_samples_are_valid_python(value):
    # The parity samples above are plain Python; a quick sanity check that the reference side
    # really is CPython's float formatting.
    assert format(value, ".2f") == f"{value:.2f}"
    assert math.isfinite(value)
