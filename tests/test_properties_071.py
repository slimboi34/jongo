"""Property and differential tests from the machine-driven test of 0.7.0.

The test run fuzzed the compiler (CPython against the compiled code under Node), forms
(server against browser), the ORM (against a pure-Python oracle) and the HTTP caps. These
are its invariants, kept as regression tests: each draws a fixed set of cases from a seeded
generator, so a run is deterministic and fast, and each Node check is one process. The
defects it found are fixed in 0.7.1; their minimal cases are in the error-parity table below.
"""
from __future__ import annotations

import importlib.util
import io
import json
import math
import random
import re
import shutil
import subprocess
import uuid

import pytest

from jongo import db, forms
from jongo.compiler.bundle import Bundler, runtime_source
from jongo.db import connection
from jongo.errors import JongoError
from jongo.http import Request

from conftest import use_test_database

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _node(script: str) -> list:
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------- compiler differential --


def _run_exprs_in_node(exprs: list[str], tmp_path) -> list:
    """Compile each expression as the body of a module-level function and evaluate them all in
    one Node process: ``{"ok": repr}`` or ``{"err": exception name}`` per expression."""
    name = f"gen_{uuid.uuid4().hex}"
    path = tmp_path / f"{name}.py"
    path.write_text("import math, json\n" + "".join(f"def e{i}():\n    return ({e})\n" for i, e in enumerate(exprs)))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bundler = Bundler(roots=[tmp_path])
    names = [bundler.resolve(f"e{i}", getattr(module, f"e{i}")) for i in range(len(exprs))]
    units = "\n".join(f"const {u.name} = {u.code};" for u in bundler.order)
    calls = "\n".join(f"try {{ out.push({{ok: $repr({n}())}}); }} catch (e) {{ out.push({{err: String(e && e.name)}}); }}"
                      for n in names)
    return _node('(function(){"use strict";\n' + runtime_source() + "\n" + units
                 + "\nconst out=[];\n" + calls + "\nconsole.log(JSON.stringify(out));\n})();")


def _py_result(expr: str) -> dict:
    try:
        return {"ok": repr(eval(expr, {"math": math, "json": json}))}  # noqa: S307
    except Exception as exc:  # noqa: BLE001
        return {"err": type(exc).__name__}


def _assert_agree(exprs: list[str], tmp_path):
    browser = _run_exprs_in_node(exprs, tmp_path)
    wrong = [f"{e}: python {_py_result(e)} browser {b}" for e, b in zip(exprs, browser) if b != _py_result(e)]
    assert not wrong, "\n".join(wrong)


#: Parity-sensitive paths the fuzzer stressed; all must agree.
AGREE = [
    "7 // 2", "-7 // 2", "7 % 3", "-7 % 3", "0.1 % 1", "divmod(-7, 2)",
    "2 ** 10", "pow(3, -1, 7)", "~5", "~(2 ** 40)", "round(2.675, 2)",
    "round(1e22, 1)", "sorted([3, 1, 2])", "sorted(['b', 'A', 'a'])", "[1, 2] + [3]",
    "'a,b,,c'.split(',')", "'  x  '.strip()", "' '.join(['a', 'b'])", "'%.2f' % 0.125",
    "f'{2.5:.0f}'", "f'{1234567:,}'", "'abcabc'.find('c', 3)", "len('\U0001F600')",
    "(1, 2) == [1, 2]", "{1, 2} < {1, 2, 3}", "[1] < [1, 2]", "str({'k': 1, 'n': [1, 2]})",
    "repr((1,))", "'ab'.startswith(('x', 'a'))", "list(enumerate('ab', 1))",
    "json.dumps({'b': 1, 'a': 2}, sort_keys=True)", "max([[1, 2], [1, 3]])",
    "'aAbB'.swapcase()", "'HeLLo'.casefold()", "int('ff', 16)", "float('1_000.5')",
    "'x'.rjust(4, '*')", "[c for c in 'abc']", "{k: v for k, v in [('a', 1)]}",
]


@needs_node
def test_compiler_agrees_with_cpython_on_curated_expressions(tmp_path):
    _assert_agree(AGREE, tmp_path)


#: The defects the run found in browser builtins (0.7.0), and their neighbours: the browser
#: must raise the exception CPython raises (or return what it returns).
ERROR_PARITY = [
    "ord('ab')", "ord('')", "ord(5)", "ord('\U0001F600')",
    "abs('a')", "abs([1, 2])", "abs(None)", "abs(True)", "abs(-2.5)",
    "8 >> -1", "8 << -1", "'a' >> -1", "8 >> 'a'", "8 >> 0", "1 << 40",
    "round(2.5, ndigits=1)", "round(2.675, ndigits=2)", "round(15, ndigits=-1)", "round(2.5, ndigits=None)",
    "round(float('inf'))", "round(float('-inf'))", "round(float('nan'))", "round(float('inf'), 1)",
    "round(float('nan'), 0)", "round('a')", "round(None)", "round(1.5, 'a')",
    "chr(-1)", "chr(0x110000)", "chr(2 ** 31)", "chr(-(2 ** 31) - 1)", "chr(1.5)", "chr('a')", "chr(65)", "chr(True)",
    "'a\\tb'.expandtabs()", "'a\\tb\\ncd\\te'.expandtabs(4)", "'\\tx'.expandtabs(tabsize=0)",
    "'ab\\t'.expandtabs(True)", "'x\\ty'.expandtabs(2.5)",
]


@needs_node
def test_browser_builtins_raise_what_cpython_raises(tmp_path):
    _assert_agree(ERROR_PARITY, tmp_path)


_ATOMS = ["0", "1", "2", "7", "-3", "10", "-1", "'a'", "'ab'", "'A'", "'\\t'", "'a,b,c'", "[1, 2, 3]", "[]",
          "(1, 2)", "True", "False", "None"]
_UNARY = ["len", "str", "bool", "repr", "abs", "ord", "chr", "round", "sorted", "list", "int", "min", "max"]
_BINARY = ["+", "-", "*", "//", "%", ">>", "<<", "==", "<", "&", "|"]
#: Non-integral floats only, and only at the top of an expression: an integral float prints
#: like an int in the browser (a documented difference, guide §18), wherever str() meets it.
_FLOATS = ["0.5", "2.5", "-1.5", "2.675", "1e-300", "-0.25", "float('inf')", "float('-inf')", "float('nan')"]


def _expr(rng: random.Random, depth: int) -> str:
    if depth == 0 or rng.random() < 0.3:
        return rng.choice(_ATOMS)
    k = rng.random()
    if k < 0.35:
        return f"{rng.choice(_UNARY)}({_expr(rng, depth - 1)})"
    if k < 0.65:
        return f"({_expr(rng, depth - 1)} {rng.choice(_BINARY)} {_expr(rng, depth - 1)})"
    if k < 0.75:
        return f"round({_expr(rng, depth - 1)}, ndigits={rng.choice(['0', '1', '-1', 'None'])})"
    if k < 0.85:
        return f"({_expr(rng, depth - 1)} if {_expr(rng, depth - 1)} else {_expr(rng, depth - 1)})"
    if k < 0.92:
        return f"[x for x in {rng.choice(['[1, 2, 3]', repr('abc')])}]"
    return f"{rng.choice(['-', ',', ' '])!r}.join(['a', 'b'])"


def _float_expr(rng: random.Random) -> str:
    x = rng.choice(_FLOATS + _ATOMS[:7])
    return rng.choice([f"abs({x})", f"round({x})", f"round({x}, {rng.randint(-2, 3)})",
                       f"round({x}, ndigits={rng.randint(-2, 3)})", f"({x} >> {rng.choice(['1', '-1', '0'])})",
                       f"chr({x})", f"ord(str({x}))"])


def _comparable(expr: str) -> bool:
    """Whether CPython's result can be compared as text: no integral float (documented), no
    integer past 2**53 (one number type in the browser, documented), and short."""
    try:
        value = eval(expr, {"math": math, "json": json})  # noqa: S307
    except Exception:  # noqa: BLE001
        return True
    text = repr(value)
    if isinstance(value, float) and (value.is_integer() or value == 0):
        return False
    return len(text) <= 200 and not re.search(r"\d{16}", text)


@needs_node
def test_compiler_differential_small_expressions(tmp_path):
    """A seeded sample from a small grammar (the fuzzer's, narrowed): CPython and the browser
    must agree, value or exception type."""
    rng = random.Random(71)
    exprs = []
    while len(exprs) < 400:
        e = _float_expr(rng) if rng.random() < 0.25 else _expr(rng, 3)
        if _comparable(e):
            exprs.append(e)
    _assert_agree(exprs, tmp_path)


# ---------------------------------------------------------------------- forms parity --


def _js(value) -> str:
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if value != value else ("Infinity" if value > 0 else "-Infinity")
    return json.dumps(value)


SCHEMA = forms.Schema(
    "S", n=forms.Int(min=1, max=50, required=False), price=forms.Float(min=0, max=1e6, required=False),
    email=forms.Email(required=False), name=forms.Text(min_length=2, max_length=10),
    code=forms.Text(pattern=r"[A-Z]+]?", required=False), level=forms.Choice([2, "a", 0]),
    agree=forms.Bool(),
)


def _values(rng: random.Random, count: int) -> list:
    chars = "aZ09 .-@_\t\né \U0001F600]"
    special = [None, True, False, 0, 2, 2.0, -0.0, 0.0, 2.5, 50, 51, 1e16, 2 ** 53 + 1, float("inf"),
               float("-inf"), float("nan"), "", " ", "2", "2.0", " 2 ", "a", "A]", "AB", "x@y.co", "on", "off", "1e3"]
    out = list(special)
    while len(out) < count:
        k = rng.random()
        if k < 0.4:
            out.append("".join(rng.choice(chars) for _ in range(rng.randint(0, 8))))
        elif k < 0.6:
            out.append(rng.randint(-5, 60))
        elif k < 0.85:
            out.append(rng.choice([rng.uniform(-100, 100), float(rng.randint(-3, 3)), rng.random() * 1e-5]))
        else:
            out.append(rng.choice([True, False, None]))
    return out


@needs_node
def test_forms_check_agrees_server_and_browser():
    """forms.check gives the same verdict in CPython and in the compiled browser code, for every
    rule kind, on a seeded sample of awkward values (Choice with 2.0 was 0.7.0's defect)."""
    values = _values(random.Random(71), 250)
    bundler = Bundler()
    check = bundler.resolve("check", forms.check)
    units = "\n".join(f"const {u.name} = {u.code};" for u in bundler.order)
    rules = {field: dict(rule) for field, rule in SCHEMA.items()}
    script = ('(function(){"use strict";\n' + runtime_source() + "\n" + units
              + f"\nconst rules = {json.dumps(rules)};\nconst values = [{', '.join(_js(v) for v in values)}];\n"
              + "const out = [];\nfor (const v of values) { const row = {};\n"
              + f"  for (const f of Object.keys(rules)) row[f] = {check}(rules[f], v, {{}});\n  out.push(row); }}\n"
              + "console.log(JSON.stringify(out));\n})();")
    browser = _node(script)
    wrong = []
    for value, row in zip(values, browser):
        for field, rule in SCHEMA.items():
            if forms.check(rule, value, {}) != row[field]:
                wrong.append(f"{field} {value!r}: python {forms.check(rule, value, {})!r} browser {row[field]!r}")
    assert not wrong, "\n".join(wrong[:20])


def _patterns(rng: random.Random, count: int) -> list[str]:
    alphabet = ["a", "b", "0", "[", "]", "^", "-", "(", ")", "|", "*", "+", "?", ".", "\\d", "\\]", "\\[", "{2}", "A-Z",
                "(?=a)", "(?!b)", "(?<=a)", "\\b", "\\B", "$"]
    out = [r"[A-Z]+]", r"a]", r"(a|b])", r"\d]", r"[]a]", r"[^]a]"]
    while len(out) < count:
        out.append("".join(rng.choice(alphabet) for _ in range(rng.randint(1, 7))))
    return out


@needs_node
def test_portable_patterns_are_portable():
    """Every pattern portable_pattern() accepts compiles as the browser's u-mode RegExp and
    matches the same strings there as re.fullmatch does here (a lone ']' broke this in 0.7.0)."""
    accepted = []
    for pattern in _patterns(random.Random(71), 400):
        try:
            accepted.append(forms.portable_pattern(pattern))
        except JongoError:
            continue  # refused at definition: the other half of the guarantee
    samples = ["", "a", "b", "ab", "]", "a]", "A]", "AB]", "0", "0]", "[", "-", "^", "aa", "ba", "\n"]
    script = (f"const pats = {json.dumps(accepted)}; const samples = {json.dumps(samples)};\n"
              "const out = pats.map((p) => { try { const r = new RegExp('^(?:' + p + ')$', 'u');"
              " return samples.map((s) => r.test(s)); } catch (e) { return 'THROW ' + e.message; } });\n"
              "console.log(JSON.stringify(out));")
    browser = _node(script)
    wrong = []
    for pattern, got in zip(accepted, browser):
        want = [forms.matches(pattern, s) for s in samples]
        if got != want:
            wrong.append(f"{pattern!r}: python {want} browser {got}")
    assert not wrong, "\n".join(wrong[:20])


# ------------------------------------------------------------------------- ORM oracle --


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


def test_orm_filter_and_exclude_match_python_oracle(memory_db):
    class Row(db.Model):
        i = db.Int(null=True)
        t = db.Text(null=True)

    db.migrate()
    rng = random.Random(71)
    for _ in range(150):
        Row.objects.all().delete()
        stored = []
        for _ in range(rng.randint(0, 6)):
            obj = Row.create(i=rng.choice([None, rng.randint(-20, 20)]), t=rng.choice([None, "a", "A", "ab", "b", ""]))
            stored.append({"id": obj.id, "i": obj.i, "t": obj.t})
        lo = rng.randint(-20, 20)
        picks = rng.sample(range(-20, 21), 3) + [None] * rng.randint(0, 1)
        checks = {
            "i__gte": (lambda r: r["i"] is not None and r["i"] >= lo, lo),
            "i__in": (lambda r: r["i"] in picks if r["i"] is not None else None in picks, picks),
            "t__iexact": (lambda r: r["t"] is not None and r["t"].lower() == "a", "a"),
            "i__isnull": (lambda r: r["i"] is None, True),
        }
        for lookup, (keep, value) in checks.items():
            want_f = sorted(r["id"] for r in stored if keep(r))
            want_e = sorted(r["id"] for r in stored if not keep(r))
            assert sorted(o.id for o in Row.filter(**{lookup: value})) == want_f, (lookup, value, stored)
            assert sorted(o.id for o in Row.exclude(**{lookup: value})) == want_e, (lookup, value, stored)


def test_orm_float_in_matches_exact(memory_db):
    """filter(f__in=[x]) finds what filter(f=x) finds, for floats of every magnitude
    (0.7.0 inlined repr(x), which SQLite reads back as a different double for some)."""
    class Sample(db.Model):
        f = db.Float(null=True)

    db.migrate()
    rng = random.Random(71)
    values = [rng.choice([1, -1]) * rng.random() * 10.0 ** rng.randint(-320, 300) for _ in range(300)]
    values += [5e-324, 2.2250738585072014e-308, 1.7976931348623157e308, -2.901816873243615e-258, 1e-300]
    for v in values:
        Sample.create(f=v)
    by_exact = {v: sorted(o.id for o in Sample.filter(f=v)) for v in values}
    for v in values:
        assert sorted(o.id for o in Sample.filter(f__in=[v])) == by_exact[v], v
    chunk = values[:50]
    assert sorted(o.id for o in Sample.filter(f__in=chunk)) == sorted({i for v in chunk for i in by_exact[v]})


# -------------------------------------------------------------------------- HTTP caps --


def _multipart(n_parts: int) -> bytes:
    body = b"".join(b'--B\r\nContent-Disposition: form-data; name="f%d"\r\n\r\nv\r\n' % i for i in range(n_parts))
    return body + b"--B--\r\n"


@pytest.mark.parametrize("n,refused", [(10, False), (2000, True)])
def test_multipart_part_cap_is_enforced(n, refused):
    body = _multipart(n)
    request = Request({"REQUEST_METHOD": "POST", "PATH_INFO": "/x", "CONTENT_TYPE": "multipart/form-data; boundary=B",
                       "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body)})
    if refused:
        with pytest.raises(Exception) as info:
            request.form
        assert getattr(info.value, "status", None) == 413
    else:
        assert len(request.form) == n
