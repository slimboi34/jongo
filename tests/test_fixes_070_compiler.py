"""0.7.0 fixes in the compiler, the browser runtime (dom.js), forms and the vdom.

Each test names the defect it guards against. The browser-side checks run dom.js in Node
against a small DOM stand-in (no browser needed); they skip when node isn't installed.
"""
from __future__ import annotations

import importlib.util
import json
import math
import shutil
import subprocess
import sys
import textwrap
from math import isclose  # noqa: F401 - used by a sample that must not compile
from pathlib import Path

import pytest

import json as json_module
import math as math_module
import random as random_module
import time as time_module
from conftest import use_test_database
from jongo import Jongo, auth, component, db, forms, rpc, state, styles, vdom
from jongo.compiler import bundle as bundle_mod
from jongo.compiler.bundle import BROWSER_MODULE_MEMBERS, Bundler, check_syntax, js_unit_name, runtime_source
from jongo.compiler.scope import analyse_function
from jongo.compiler.transpile import RUNTIME_METHODS
from jongo.errors import CompileError, JongoError
from jongo.html import a, button, div, img, li, span, ul
from test_compiler import needs_node, run_in_node

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


def run_js(body: str, tmp_path: Path, prelude: str = "") -> object:
    """Run ``body`` inside the runtime's scope (so $-helpers are visible); it must call
    ``done(value)``. ``prelude`` runs first, at the top level (DOM stand-ins)."""
    script = tmp_path / "t.js"
    script.write_text(
        prelude + "\n(async function () {\n\"use strict\";\n" + runtime_source()
        + "\nconst done = (v) => console.log('@@' + JSON.stringify(v));\n" + body
        + "\n})().catch((e) => { console.log('@@' + JSON.stringify({ crashed: String(e && e.stack) })); });\n"
    )
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("@@")), None)
    assert line is not None, proc.stdout + proc.stderr
    value = json.loads(line[2:])
    assert not (isinstance(value, dict) and "crashed" in value), value
    return value


# A DOM good enough for $mount/$patch/$setProp: moving or removing a subtree that holds the
# focused element blurs it, as browsers do; moveBefore (when present) keeps focus.
DOM = r"""
class N {
  constructor(t) { this.nodeType = t; this.parentNode = null; this.childNodes = []; }
  get firstChild() { return this.childNodes[0] || null; }
  get nextSibling() { if (!this.parentNode) return null; const s = this.parentNode.childNodes; return s[s.indexOf(this) + 1] || null; }
  get isConnected() { let n = this; while (n) { if (n === document) return true; n = n.parentNode; } return false; }
  contains(x) { while (x) { if (x === this) return true; x = x.parentNode; } return false; }
  _detach(node) { const s = node.parentNode.childNodes; s.splice(s.indexOf(node), 1); node.parentNode = null; }
  _attach(node, before) {
    node.parentNode = this;
    if (before == null) this.childNodes.push(node); else this.childNodes.splice(this.childNodes.indexOf(before), 0, node);
  }
  insertBefore(node, before) {
    if (node.parentNode) { if (node.contains(document.activeElement)) { document.activeElement.blurs++; document.activeElement = document.body; } this._detach(node); }
    this._attach(node, before); return node;
  }
  removeChild(node) { if (node.contains(document.activeElement)) document.activeElement = document.body; this._detach(node); return node; }
  appendChild(node) { return this.insertBefore(node, null); }
}
class T extends N { constructor(v) { super(3); this.nodeValue = String(v); } }
class E extends N {
  constructor(tag) {
    super(1); this.localName = tag; this.tagName = tag.toUpperCase(); this.attrs = {}; this.blurs = 0;
    const props = {}; this.style = { props, setProperty: (k, v) => { props[k] = v; }, removeProperty: (k) => { delete props[k]; } };
    this.selectionStart = null; this.selectionEnd = null; this.selectionDirection = null;
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener() {}
  focus() { document.activeElement = this; }
  setSelectionRange(a, b, d) { this.selectionStart = a; this.selectionEnd = b; this.selectionDirection = d; }
}
const document = new N(9);
document.body = new E("body"); document.appendChild(document.body); document.activeElement = document.body;
document.createElement = (t) => new E(t);
document.createElementNS = (ns, t) => new E(t);
document.createTextNode = (v) => new T(v);
document.getElementById = () => null;
document.cookie = "";
"""

# ------------------------------------------------------------------------- dom.js: URLs --

URLS = [
    "data:image/png;base64,AAAA", "DATA:image/jpeg,xx", "data:,hello", "data:text/html,<b>x</b>",
    " data:image/svg+xml,<svg/>", "data:image/gif;base64,R0lG", "javascript:alert(1)", "JaVa\tScRiPt:x",
    "vbscript:x", "https://ok.example/a:b", "/relative/path", "mailto:a@b.co", "no-colon", "data:",
]


@needs_node
def test_data_urls_no_longer_crash_and_match_the_server(tmp_path):
    # dom.js used s.split(":", 1)[1], which is undefined in JavaScript: every data: URL threw,
    # so one stored data: avatar killed hydration for every viewer.
    expected = [vdom._is_dangerous_url(u) for u in URLS]
    got = run_js(
        f"const urls = {json.dumps(URLS)};\n"
        "const verdicts = urls.map((u) => { try { return $dangerousUrl(u); } catch (e) { return 'THROWS ' + e.message; } });\n"
        "const root = document.createElement('div');\n"
        "const v = $h('div')($h('img')(new KW({ src: 'data:image/png;base64,AAAA' })), $h('a')('x', new KW({ href: 'data:,x' })));\n"
        "$mount(v, root, null, false, 0);\n"
        "done([verdicts, v.c[0].d.attrs.src, v.c[1].d.attrs.href === undefined]);",
        tmp_path, DOM)
    assert got == [expected, "data:image/png;base64,AAAA", True]


# ----------------------------------------------------------------- dom.js: navigation --

NAV_PRELUDE = DOM + r"""
const navLog = [];
globalThis.window = { location: { href: "http://127.0.0.1:10321/page", origin: "http://127.0.0.1:10321",
  reload() { navLog.push("reload"); } }, scrollTo() {} };
globalThis.history = { pushState(s, t, u) { navLog.push(["push", u]); }, replaceState() {} };
const errors = [];
console.error = (e) => errors.push(String(e && e.message !== undefined ? e.message : e));
"""


@needs_node
def test_navigate_refuses_script_urls(tmp_path):
    # redirect(user_value) from a @server function, or navigate(user_value), ran javascript:
    # URLs in the app's origin: their origin is "null", so they went to location.href.
    got = run_js(
        r"""
const hrefs = [];
globalThis.fetch = async (url) => { navLog.push(["fetch", url]); return { url, headers: { get: () => null } }; };
for (const bad of ["javascript:alert(1)", " JavaScript:alert(1)", "java\tscript:alert(1)", "data:text/html,<script>x</script>",
                   "vbscript:msgbox", "javascript://%0aalert(1)"]) {
  const result = await $navigate(bad);
  hrefs.push([result, window.location.href]);
}
await $navigate("https://elsewhere.example/x");
const offsite = window.location.href;
window.location.href = "http://127.0.0.1:10321/page";
await $navigate("/next");
done([hrefs, offsite, navLog, errors.length]);
""", tmp_path, NAV_PRELUDE)
    hrefs, offsite, log, error_count = got
    assert all(h == [None, "http://127.0.0.1:10321/page"] for h in hrefs)
    assert offsite == "https://elsewhere.example/x"
    assert ["fetch", "http://127.0.0.1:10321/next"] in log
    assert error_count == 6, "each refusal is reported"


@needs_node
def test_a_server_redirect_to_javascript_is_not_followed(tmp_path):
    got = run_js(
        r"""
globalThis.fetch = async () => ({ status: 200, json: async () => ({ redirect: "javascript:document.title='XSS'" }) });
const stub = $rpc("app.finish");
const result = await stub("javascript:...");
done([result, window.location.href, errors]);
""", tmp_path, NAV_PRELUDE)
    assert got[0] is None and got[1] == "http://127.0.0.1:10321/page"
    assert "only http and https" in got[2][0]


# ------------------------------------------------------------------ dom.js: live() cap --

LIVE_PRELUDE = DOM + r"""
const sources = [];
const warnings = [];
const reports = [];
const timers = [];
console.warn = (...a) => warnings.push(a.join(" "));
console.error = (e) => reports.push(String(e && e.message !== undefined ? e.message : e));
globalThis.setTimeout = (f, ms) => { timers.push(ms); };
class FakeSource { constructor(url) { this.url = url; sources.push(this); } close() { this.closed = true; } }
globalThis.window = { EventSource: FakeSource };
globalThis.EventSource = FakeSource;
"""


@needs_node
def test_a_refused_live_stream_stops_and_says_why(tmp_path):
    # One EventSource carries every channel; the server refuses the whole stream past 20
    # channels (403), and the runtime used to retry that forever without a word.
    got = run_js(
        r"""
$BOOT.dev = true;
globalThis.fetch = async (url) => ({ status: 403,
  text: async () => "<html><body><p>403</p><h1>A connection may subscribe to at most 20 channels.</h1></body></html>" });
for (let i = 0; i < 21; i++) $liveAdd("row:" + i, () => {});
await new Promise((r) => queueMicrotask(r));
await new Promise((r) => queueMicrotask(r));
const first = sources.length;
await sources[0].onerror();
done([first, warnings.length, reports, timers, sources.length, $LIVE.refused !== null]);
""", tmp_path, LIVE_PRELUDE)
    opened, warned, reports, timers, total, refused = got
    assert opened == 1 and warned == 1, "a dev warning before connecting with more than 20 channels"
    assert len(reports) == 1 and "HTTP 403" in reports[0] and "at most 20 channels" in reports[0]
    assert timers == [] and total == 1 and refused, "no retry loop after a refusal"


@needs_node
def test_a_dropped_live_stream_still_reconnects(tmp_path):
    got = run_js(
        r"""
let aborted = 0;
globalThis.AbortController = class { constructor() { this.signal = {}; } abort() { aborted++; } };
globalThis.fetch = async () => ({ status: 200, text: async () => "" });
$liveAdd("room:1", () => {});
await new Promise((r) => queueMicrotask(r));
await sources[0].onerror();
done([warnings.length, reports.length, timers, aborted]);
""", tmp_path, LIVE_PRELUDE)
    assert got == [0, 0, [500], 1]


# ------------------------------------------------------------ dom.js: keyed reorder focus --

REORDER = r"""
const root = document.createElement("div");
document.body.appendChild(root);
const rows = (keys) => $h("ul")(...keys.map((k) => $h("li")($h("input")(new KW({ id: "in-" + k })), new KW({ key: k }))));
const v1 = rows(["a", "b", "c"]);
$mount(v1, root, null, false, 0);
const input = v1.c[0].c[0].d;
input.focus();
input.selectionStart = 2; input.selectionEnd = 4; input.selectionDirection = "backward";
const v2 = rows(["c", "b", "a"]);
$patch(v1, v2, root, false, 0);
done([v2.c[2].c[0].d === input, document.activeElement === input, [input.selectionStart, input.selectionEnd, input.selectionDirection],
      v2.c.map((li) => li.c[0].d.attrs.id), input.blurs]);
"""


@needs_node
def test_keyed_reorder_keeps_focus_and_caret(tmp_path):
    got = run_js(REORDER, tmp_path, DOM)
    same, focused, selection, order, blurs = got
    assert same and focused, "the input was moved, not remounted, and still has focus"
    assert selection == [2, 4, "backward"]
    assert order == ["in-c", "in-b", "in-a"]
    assert blurs == 1  # this DOM blurs on insertBefore, like browsers without moveBefore


@needs_node
def test_keyed_reorder_uses_move_before_where_available(tmp_path):
    prelude = DOM + "\nN.prototype.moveBefore = function (node, ref) { this._detach(node); this._attach(node, ref); };\n"
    same, focused, selection, order, blurs = run_js(REORDER, tmp_path, prelude)
    assert same and focused and blurs == 0 and order == ["in-c", "in-b", "in-a"]


# ------------------------------------------------- SSR and browser write the same text --


def Panel(ids=(), meta=None, ratio=0.0):
    return div(span("a", data_ids=list(ids), data_pair=tuple(ids), data_meta=meta, data_ratio=ratio), span(ratio),
               span(1e16))


PanelComponent = component(Panel)


@needs_node
def test_attribute_and_number_text_match_the_server(tmp_path):
    # The server writes str(value); the browser wrote String(value) ("1,2", "[object Object]",
    # "10000000000000000") and dropped inf props (sent as null).
    props = {"ids": [1, 2], "meta": {"k": 1}, "ratio": float("inf")}
    html = vdom.render_to_string(PanelComponent(**props))
    data = vdom.serialize(PanelComponent(**props))
    assert data["p"]["ratio"] == {"$v": {"num": "inf"}}
    json.dumps(data, allow_nan=False)  # still plain JSON
    assert vdom.revive(data["p"])["ratio"] == float("inf")
    bundler = Bundler(roots=[Path(__file__).parent])
    name = bundler.resolve("PanelComponent", PanelComponent)
    script = tmp_path / "panel.js"
    script.write_text(DOM + bundler.render(
        f"const root = document.createElement('div');\n"
        f"const tree = $build({json.dumps(data)});\n"
        f"$mount(tree, root, null, false, 0); void {name};\n"
        "const div_ = root.childNodes[0]; const s = div_.childNodes[0];\n"
        "console.log('@@' + JSON.stringify([s.attrs, div_.childNodes.slice(1).map((n) => n.childNodes[0].nodeValue)]));"))
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    attrs, texts = json.loads(next(l for l in proc.stdout.splitlines() if l.startswith("@@"))[2:])
    assert attrs == {"data-ids": "[1, 2]", "data-pair": "(1, 2)", "data-meta": "{'k': 1}", "data-ratio": "inf"}
    assert texts == ["inf", "1e+16"]
    for key, value in attrs.items():
        assert f'{key}="{value}"'.replace("'", "&#x27;") in html
    assert "<span>inf</span><span>1e+16</span>" in html


def test_non_finite_floats_stay_null_in_plain_json():
    # APIs, broadcasts and MCP results keep their old shape: only browser-bound data wraps them.
    assert vdom.to_json_data({"x": float("nan"), "y": [float("-inf")]}) == {"x": None, "y": [None]}
    assert vdom.to_json_data(float("-inf"), browser=True) == {"$v": {"num": "-inf"}}
    assert math.isnan(vdom.revive({"$v": {"num": "nan"}}))


@needs_node
def test_browser_revives_non_finite_floats(tmp_path):
    got = run_js("done([$revive({'$v': {num: 'inf'}}) === Infinity, $revive([{'$v': {num: '-inf'}}])[0] === -Infinity,"
                 " Number.isNaN($revive({a: {'$v': {num: 'nan'}}}).a), $revive({'$$v': 1})]);", tmp_path)
    assert got == [True, True, True, {"$v": 1}]


# --------------------------------------------------------------- style declarations --

STYLES = [
    ("color", "red"), ("color", "red; background: url(//evil.example/x)"), ("width", 10), ("opacity", 0.5),
    ("background", "url(a.png)"), ("background", "url(a.png"), ("font_family", '"Inter", sans-serif'),
    ("content", "'x"), ("color", "red}body{color:blue"), ("color", "</style><script>"), ("color", "\\72 ed"),
    ("--brand-color", "#fff"), ("color: red; x", "1"), ("marginTop", "1px"),
]


@needs_node
def test_style_values_are_one_declaration_on_both_sides(tmp_path):
    # style={...} joined values into the attribute unchecked, so a value with ";" added
    # declarations in SSR (the browser's setProperty refused it).
    expected = [vdom.css_declaration_ok(vdom.css_property(k), vdom.css_value(vdom.css_property(k), v)) for k, v in STYLES]
    assert expected[0] and not expected[1] and expected[4] and not expected[5] and expected[11] and not expected[12]
    got = run_js(f"const table = {json.dumps(STYLES)};\n"
                 "done(table.map(([k, v]) => { const p = $cssProp(k); return $cssOk(p, $cssValue(p, v)); }));", tmp_path)
    assert got == expected
    html = vdom.render_to_string(div(style={"color": "red; background: url(//evil.example/x)", "width": 10}))
    assert html == '<div style="width: 10px"></div>'


@needs_node
def test_browser_drops_the_same_style_values(tmp_path):
    got = run_js("const node = document.createElement('div');\n"
                 "$setProp(node, 'style', { color: 'red; background: blue', width: 10, '--x': '1' }, undefined, false);\n"
                 "done(node.style.props);", tmp_path, DOM)
    assert got == {"width": "10px", "--x": "1"}


# ----------------------------------------------------------- AttributeError on None --


@needs_node
def test_native_null_access_matches_attribute_error_and_type_error(tmp_path):
    got = run_js("""
const out = [];
for (const f of [() => null.name, () => undefined.x, () => { null.y = 1; }, () => (void 0)(), () => $b.int([])]) {
  try { f(); out.push('no error'); } catch (e) { out.push([$isexc(e, $b.AttributeError), $isexc(e, $b.TypeError)]); }
}
done(out);""", tmp_path)
    assert got == [[True, True], [True, True], [True, True], [False, True], [False, True]]


# --------------------------------------------------------- compiler: module members --


def uses_gcd():
    return math_module.gcd(12, 18)


def uses_isclose_import():
    return isclose(0.1 + 0.2, 0.3)


def uses_sample():
    return random_module.sample([1, 2, 3], 2)


def uses_sleep():
    return time_module.sleep(0)


def uses_unknown_json():
    return json_module.load(None)


def uses_decode_error():
    try:
        return json_module.loads("{")
    except json_module.JSONDecodeError:
        return None


def uses_supported_members():
    return [math_module.floor(2.5), math_module.pi > 3, random_module.random() < 1, time_module.time() > 0,
            json_module.dumps([1])]


@pytest.mark.parametrize("fn", [uses_gcd, uses_isclose_import, uses_sample, uses_sleep, uses_unknown_json],
                         ids=lambda f: f.__name__)
def test_unsupported_browser_module_members_are_compile_errors(fn):
    # They used to compile and fail only in the user's browser ("not a function").
    with pytest.raises(CompileError) as info:
        Bundler(roots=[Path(__file__).parent]).resolve(fn.__name__, fn)
    assert info.value.code == "J103" and "isn't available in browser code" in info.value.message


def test_supported_members_and_json_decode_error_compile():
    bundler = Bundler(roots=[Path(__file__).parent])
    bundler.resolve("uses_supported_members", uses_supported_members)
    bundler.resolve("uses_decode_error", uses_decode_error)
    assert "$modules.json.JSONDecodeError" in bundler.render()


@needs_node
def test_python_tables_match_the_runtime(tmp_path):
    got = run_js("done({ str: Object.keys($STR), list: Object.keys($LIST), dict: Object.keys($DICT), set: Object.keys($SET),"
                 " tuple: [...$TUPLE_METHODS], modules: Object.fromEntries(Object.entries($modules).map(([k, v]) => [k, Object.keys(v)])) });",
                 tmp_path)
    for kind in ("str", "list", "dict", "set", "tuple"):
        assert set(got[kind]) == set(RUNTIME_METHODS[kind]), kind
    assert {k: set(v) for k, v in got["modules"].items()} == {k: set(v) for k, v in BROWSER_MODULE_MEMBERS.items()}


# ----------------------------------------------------- compiler: invalid JavaScript --


def int_literal_attribute():
    return str((7).numerator)


def int_literal_method():
    return (5).bit_length()


@pytest.mark.parametrize("fn", [int_literal_attribute, int_literal_method], ids=lambda f: f.__name__)
def test_attribute_of_a_number_literal_is_a_compile_error(fn):
    # (7).numerator compiled to "7.numerator", a syntax error that took down the whole bundle.
    with pytest.raises(CompileError) as info:
        Bundler(roots=[Path(__file__).parent]).resolve(fn.__name__, fn)
    assert info.value.code == "J104"


def test_unit_names_are_valid_identifiers():
    assert js_unit_name("14_shared_validation__Form") == "_14_shared_validation__Form"
    assert js_unit_name("my-app.views__List") == "my_app_views__List"
    assert js_unit_name("app__Counter") == "app__Counter"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


@pytest.fixture()
def registries():
    saved = [dict(r) for r in (db.models_registry, vdom.COMPONENTS, rpc.SERVER_FUNCTIONS, styles._REGISTRY)]
    db.models_registry.clear()
    use_test_database()
    yield
    db.close_connections()
    for registry, before in zip((db.models_registry, vdom.COMPONENTS, rpc.SERVER_FUNCTIONS, styles._REGISTRY), saved):
        registry.clear()
        registry.update(before)


@needs_node
def test_a_module_whose_name_starts_with_a_digit_compiles(tmp_path, registries):
    (tmp_path / "14_widgets.py").write_text(textwrap.dedent("""
        from jongo import component, state
        from jongo.html import button

        def label(n):
            return f"clicked {n}"

        @component
        def Counter():
            n = state(0)
            return button(label(n.value), on_click=lambda e: n.set(n.value + 1))
    """))
    module = _load(tmp_path / "14_widgets.py", "14_widgets")
    bundler = Bundler(roots=[tmp_path])
    bundler.add_components([module.Counter])
    js = bundler.render()
    assert "const _14_widgets__Counter = " in js and "const _14_widgets__label = " in js
    check_syntax(js)


PATTERNS = sorted((ROOT / "examples" / "patterns").glob("[0-9]*.py"))


@needs_node
@pytest.mark.parametrize("path", PATTERNS, ids=lambda p: p.stem)
def test_every_pattern_compiles_under_its_real_file_name(path, registries):
    module = _load(path, path.stem)
    components = [c for c in vdom.COMPONENTS.values() if c.fn.__module__ == path.stem]
    bundler = Bundler(roots=[path.parent])
    bundler.add_components(sorted(components, key=lambda c: c.id))
    check_syntax(bundler.render())
    assert module is not None


@needs_node
def test_check_syntax_names_the_broken_unit():
    js = "(function () {\nconst ok__unit = 1;\nconst app__Broken = 7.numerator;\n})();\n"
    with pytest.raises(CompileError) as info:
        check_syntax(js)
    assert info.value.code == "J100"
    assert "line 3" in info.value.message and "app__Broken" in info.value.message
    assert info.value.source_line == "const app__Broken = 7.numerator;"
    check_syntax("(function () { const fine = [1, 2]; })();")


@needs_node
def test_jongo_check_reports_a_bundle_that_is_not_javascript(tmp_path, monkeypatch, registries):
    from jongo import compiler, introspect

    app = Jongo("syntax", database=":memory:", root=str(tmp_path))
    monkeypatch.setattr(compiler, "build_bundle", lambda roots=(), components=None: ("const x = ;", "hash"))
    report = introspect.check(app)
    assert not report["ok"]
    problem = report["problems"][0]
    assert problem["kind"] == "compile" and problem["code"] == "J100"


def test_check_syntax_is_skipped_without_node(monkeypatch):
    monkeypatch.setattr(bundle_mod.shutil, "which", lambda name: None)
    check_syntax("this is not javascript")


# ------------------------------------------------------------------- scope analysis --


def test_a_loop_variable_rebound_by_nonlocal_is_not_block_scoped():
    import ast

    node = ast.parse(textwrap.dedent("""
        def f():
            out = []
            for i in range(3):
                def bump():
                    nonlocal i
                    i += 10
                bump()
                out.append(i)
            for j in range(3):
                out.append(lambda: j)
            return out
    """)).body[0]
    _, block_for = analyse_function(node)
    loops = [n for n in ast.walk(node) if isinstance(n, ast.For)]
    assert id(loops[0]) not in block_for and id(loops[1]) in block_for


def local_named_set():
    Set = "mine"  # noqa: N806
    return {x for x in [1, 2]}, Set


def test_comprehensions_use_a_runtime_private_set():
    bundler = Bundler(roots=[Path(__file__).parent])
    bundler.resolve("local_named_set", local_named_set)
    compiled = bundler.render().split("// ---- compiled from Python")[1]
    assert "new $PySet()" in compiled and "new Set()" not in compiled


# ------------------------------------------------------------------------------ forms --


@pytest.mark.parametrize("pattern", [
    r"x{,3}", r"a{", r"a}", r"{abc}", r"a{1, 3}", r"\a", r"\U0001F600", r"\012", r"\A[a-z]+", r"[a-z]+\Z",
    r"(?P<n>x)", r"(?P<n>x)(?P=n)", r"(?i)abc", r"(?i:abc)", r"a*+", r"a++", r"(?>a)",
])
def test_patterns_javascript_reads_differently_are_refused(pattern):
    # J115 only test-compiled with Python's re, so these threw SyntaxError in the browser.
    with pytest.raises(JongoError) as info:
        forms.portable_pattern(pattern)
    assert info.value.code == "J115"


ACCEPTED = [r"a{2}", r"a{2,}", r"a{2,5}", r"[{}]+", r"\{x\}", r"[a-z]{1,3}-\d{2}", r"(?:ab)+", r"x(?=y)", r"(?<=a)b",
            r"\x41\u00e9", r"[^\]]", r"\bword\b", r"(a)\1", r"\0", r"[\d\s]+", r"\$\.\*"]


@needs_node
def test_accepted_patterns_compile_in_javascript_unicode_mode(tmp_path):
    portable = [forms.portable_pattern(p) for p in ACCEPTED]
    got = run_js(f"const ps = {json.dumps(portable)};\n"
                 "done(ps.map((p) => { try { new RegExp('^(?:' + p + ')$', 'u'); return 'ok'; } catch (e) { return p + ': ' + e.message; } }));",
                 tmp_path)
    assert got == ["ok"] * len(ACCEPTED)


Prices = forms.Schema("Prices", amount=forms.Float(min=0, max=100), qty=forms.Int(min=-5, max=10 ** 15))


def s_non_finite_and_long_numbers():
    rule = Prices["amount"]
    qty = Prices["qty"]
    return [forms.check(rule, float("nan")), forms.check(rule, float("inf")), forms.check(rule, -float("inf")),
            [forms.check(rule, text) for text in ["nan", "inf", "-inf", "NaN", "Infinity", "1e999", " 5 "]],
            forms.check(qty, "1" * 4301), forms.check(qty, "0" * 5000 + "7"), forms.check(qty, "12345678901234567"),
            forms.check(qty, "-0000004"), forms.check(qty, "1000000000000001"), forms.check(qty, "  -0  ")]


@needs_node
def test_float_rejects_nan_and_int_handles_long_input_identically(tmp_path):
    # NaN compared false with min/max, so it passed every bound; a 4301-digit Int raised
    # ValueError (a 500) on the server while the browser answered normally.
    expected = s_non_finite_and_long_numbers()
    type_float, type_int = Prices["amount"]["messages"]["type"], Prices["qty"]["messages"]["type"]
    assert expected[:3] == [type_float] * 3 and expected[3][:6] == [type_float] * 6 and expected[3][6] == ""
    assert expected[4:] == [type_int, "", type_int, "", Prices["qty"]["messages"]["max"], ""]
    assert run_in_node({"s": s_non_finite_and_long_numbers}, tmp_path)["s"]["ok"] == expected


def test_clean_handles_long_zero_padded_ints():
    assert Prices.clean({"amount": "1", "qty": "0" * 5000 + "7"})["qty"] == 7
    assert Prices.clean({"amount": "1", "qty": "-0004"})["qty"] == -4
    with pytest.raises(db.ValidationError):
        Prices.clean({"amount": float("nan"), "qty": 1})


def test_from_model_leaves_privileged_fields_out_unless_named(registries, monkeypatch):
    monkeypatch.delenv("JONGO_ENV", raising=False)
    with pytest.warns(UserWarning, match="is_admin"):
        signup = forms.Schema.from_model(auth.User)
    assert "is_admin" not in signup and "password" not in signup and "username" in signup
    with pytest.raises(db.ValidationError):
        signup.clean({"is_admin": True})
    assert "is_admin" not in signup.clean({"username": "mallory", "is_admin": True})
    with pytest.warns(UserWarning):
        assert "is_admin" not in forms.Schema.from_model(auth.User, exclude=("email",))
    admin_form = forms.Schema.from_model(auth.User, only=["username", "is_admin"])
    assert list(admin_form) == ["username", "is_admin"]
    monkeypatch.setenv("JONGO_ENV", "production")
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        forms.Schema.from_model(auth.User)  # no warning on a deployed server


# -------------------------------------------------------------------- vdom style text --


def test_style_text_drops_injected_declarations():
    assert vdom.style_text({"color": "red", "width": 10, "--brand": "#fff"}) == "color: red; width: 10px; --brand: #fff"
    assert vdom.style_text({"color": "red; background: url(x)"}) == ""
    assert vdom.style_text({"background": "url('a.png')", "font_family": '"Inter", sans-serif'}) == (
        "background: url('a.png'); font-family: \"Inter\", sans-serif")
    assert vdom.style_text({"color": "red}a{color:blue", "x;y": "1", "z": "a\\b"}) == ""
    assert vdom.style_text("color: red; already: trusted") == "color: red; already: trusted"


def test_counter_component_still_renders():
    @component
    def Clicker():
        n = state(0)
        return button(f"{n.value}", on_click=lambda e: n.set(n.value + 1))

    try:
        assert vdom.render_to_string(Clicker()) == "<button>0</button>"
        assert vdom.render_to_string(ul(li(a("x", href="data:image/png;base64,AA")), img(src="javascript:x"))) == (
            '<ul><li><a href="data:image/png;base64,AA">x</a></li><img></ul>')
    finally:
        vdom.COMPONENTS.pop(Clicker.id, None)
