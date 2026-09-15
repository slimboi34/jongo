"""Parity tests: each sample runs natively in Python and compiled in Node, and the
results must match."""
from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

from jongo.compiler.bundle import Bundler
from jongo.errors import CompileError

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run_in_node(functions: dict, tmp_path: Path) -> dict:
    bundler = Bundler(roots=[Path(__file__).parent])
    names = {name: bundler.resolve(name, fn) for name, fn in functions.items()}
    calls = "\n".join(
        f"try {{ out[{json.dumps(n)}] = {{ ok: {js}() }}; }} "
        f"catch (e) {{ out[{json.dumps(n)}] = {{ error: String(e && e.name) + ': ' + String(e && e.message) }}; }}"
        for n, js in names.items()
    )
    script = bundler.render("const out = {};\n" + calls + "\nconsole.log(JSON.stringify(out));")
    path = tmp_path / "bundle.js"
    path.write_text(script)
    proc = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# Samples (plain Python, compiled as-is)


def s_truthiness():
    return [bool(v) for v in [0, 1, "", "a", [], [1], {}, {"a": 1}, None, 0.0]]


def s_arithmetic():
    return [7 // 2, -7 // 2, 7 % 3, -7 % 3, 2**10, 7 / 2, abs(-3), round(2.5), round(3.14159, 2), divmod(17, 5)]


def s_strings():
    name = "  Jongo Framework  "
    s = name.strip()
    return [
        s.upper(), s.lower(), s.split(), s.replace("o", "0"), "-".join(["a", "b", "c"]),
        s.startswith("Jon"), s[1:4], s[-1], s[::-1], "ab" * 3, f"{s!r} has {len(s)} chars",
        f"{3.14159:.2f}", f"{1234567:,}", f"{42:>6}", f"{'x':*^5}", "{} and {name}".format(1, name="two"),
        "%s is %d" % ("x", 5), s.title(), "a,b,,c".split(","), s.find("Frame"), "abc".zfill(6),
        "a b  c".split(None, 1), f"{0.256:.1%}",
    ]


def s_lists():
    xs = [5, 3, 8, 1]
    xs.append(9)
    xs.extend([2])
    xs.insert(0, 7)
    popped = xs.pop()
    xs.remove(8)
    ys = sorted(xs, reverse=True)
    zs = sorted(["bb", "a", "ccc"], key=len)
    a, *rest = xs
    return [
        xs, popped, ys, zs, a, rest, xs[-2:], [1, 2] + [3], [0] * 3, 3 in xs, 10 not in xs, len(xs),
        sum(xs), min(xs), max(xs, key=lambda v: -v), list(reversed(xs)), xs.index(3), xs.count(3),
    ]


def s_dicts():
    d = {"a": 1, "b": 2}
    d["c"] = 3
    d.update({"d": 4}, e=5)
    removed = d.pop("a")
    keys = list(d.keys())
    items = [[k, v] for k, v in d.items()]
    merged = {**d, "z": 26}
    inverted = {v: k for k, v in d.items()}
    counts = {}
    for word in "the cat the hat".split():
        counts[word] = counts.get(word, 0) + 1
    return [
        d, removed, keys, items, merged, d.get("missing", "fallback"), "b" in d, len(d), inverted,
        d == {"b": 2, "c": 3, "d": 4, "e": 5}, counts,
    ]


def s_control_flow():
    out = []
    for i in range(10):
        if i % 2 == 0:
            continue
        if i > 7:
            break
        out.append(i)
    else:
        out.append("no-break")
    n = 0
    while n < 3:
        n += 1
    else:
        out.append("while-else")
    for _ in []:
        pass
    else:
        out.append("empty-else")
    label = "big" if n > 2 else "small"
    grade = None
    score = 72
    if score >= 90:
        grade = "A"
    elif score >= 70:
        grade = "C"
    else:
        grade = "F"
    return [out, n, label, 1 < n <= 3, grade, not out, n is not None]


def s_exceptions():
    out = []
    try:
        [][1]
    except IndexError:
        out.append("index")
    try:
        {}["k"]
    except KeyError:
        out.append("key")
    try:
        raise ValueError("bad value")
    except (TypeError, ValueError) as e:
        out.append(str(e))
    finally:
        out.append("finally")
    try:
        int("nope")
    except ValueError:
        out.append("int")
    try:
        pass
    except Exception:
        out.append("never")
    else:
        out.append("else")
    try:
        try:
            raise KeyError("inner")
        except ValueError:
            out.append("wrong")
    except KeyError:
        out.append("outer caught")
    return out


def _helper(a, b=10, *rest, scale=1, **extra):
    return {"a": a, "b": b, "rest": list(rest), "scale": scale, "extra": extra}


def s_calls():
    return [
        _helper(1), _helper(1, 2, 3, 4, scale=2, flag=True), _helper(a=5, b=6),
        _helper(*[1, 2, 3]), _helper(1, **{"scale": 3}),
    ]


def s_closures():
    def make(start):
        count = start

        def inc():
            nonlocal count
            count += 1
            return count

        return inc

    c = make(5)
    c()
    doubled = [lambda i=i: i * 2 for i in range(3)]
    return [c(), [f() for f in doubled]]


def s_comprehensions():
    matrix = [[1, 2], [3, 4]]
    flat = [x for row in matrix for x in row if x != 2]
    pairs = {k: v for k, v in zip("abc", range(3))}
    remainders = sorted({x % 3 for x in range(10)})
    total = sum(x * x for x in range(5))
    walrus = [y for x in range(5) if (y := x * 10) > 20]
    return [flat, pairs, remainders, total, walrus, any(x > 3 for x in flat), all([])]


def s_builtins():
    return [
        int("42"), float("2.5"), str(None), str(True), repr("hi"), list(enumerate(["a", "b"], 1)),
        list(zip([1, 2], ["x", "y"])), isinstance(3, int), isinstance("s", (int, str)), chr(65), ord("A"),
        list(range(10, 0, -3)), str([1, "a", None]), bool([]), sorted([3, 1, 2]),
        list(map(lambda x: x + 1, [1, 2])), list(filter(None, [0, 1, 2])), min([4, 2, 8]), max(3, 9, 4),
    ]


def s_modules():
    return [math.floor(2.7), math.sqrt(16), round(math.pi, 3), json.loads('{"a": [1, 2]}')]


SAMPLES = {name: fn for name, fn in dict(globals()).items() if name.startswith("s_")}


def normalise(value):
    return json.loads(json.dumps(value))


@needs_node
def test_python_and_javascript_agree(tmp_path):
    results = run_in_node(SAMPLES, tmp_path)
    mismatches = []
    for name, fn in SAMPLES.items():
        expected = normalise(fn())
        got = results[name]
        if got.get("error") or got.get("ok") != expected:
            mismatches.append(f"{name}:\n  python: {expected}\n  js:     {got}")
    assert not mismatches, "\n".join(mismatches)


def s_loop_closures():
    handlers = []
    for i in range(3):
        handlers.append(lambda: i)
    return [h() for h in handlers]


@needs_node
def test_loop_closures_capture_each_item_in_js(tmp_path):
    # Deliberate improvement over Python's late binding: event handlers created
    # in a loop see their own item.
    assert run_in_node({"s": s_loop_closures}, tmp_path)["s"]["ok"] == [0, 1, 2]


def uses_open():
    return open("secrets.txt").read()


def uses_class():
    return Path("x")


def test_unsupported_builtin_is_a_clear_error():
    with pytest.raises(CompileError, match="open"):
        Bundler().resolve("uses_open", uses_open)


def test_stdlib_class_is_a_clear_error():
    with pytest.raises(CompileError, match="Path"):
        Bundler(roots=[Path(__file__).parent]).resolve("uses_class", uses_class)
