"""Assemble the browser bundle: the runtime plus every component compiled from Python.

Compilation starts from the registered components and follows the names they
use. Each Python object becomes one JS "unit": components compile to
components, helper functions compile too, ``@server`` functions become RPC
stubs, stylesheets and constants are inlined as data.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import re
import types
from dataclasses import dataclass
from pathlib import Path

from .. import vdom
from ..errors import CompileError, JongoError, ServerError
from ..rpc import ServerFunction
from ..styles import StyleSheet
from ..vdom import COMPONENTS, Component, Tag, VNode, serialize, to_json_data
from .transpile import transpile_function

RUNTIME_DIR = Path(__file__).resolve().parent
JONGO_DIR = RUNTIME_DIR.parent
BROWSER_MODULES = frozenset(("math", "random", "json", "time"))

BOOT_FOOTER = """
if (typeof window !== "undefined") {
  window.jongo = { navigate: $navigate, refresh: $refresh, components: $COMPONENTS };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", $start);
  else $start();
}
"""


def runtime_source() -> str:
    return "\n".join((RUNTIME_DIR / name).read_text() for name in ("pyrt.js", "dom.js"))


def _special_objects() -> dict[int, str]:
    from .. import html

    special = {
        vdom.state: "$state",
        vdom.effect: "$effect",
        vdom.ref: "$ref",
        vdom.navigate: "$navigate",
        vdom.refresh: "$refresh",
        vdom.form_values: "$form_values",
        vdom.js: "globalThis",
        vdom.h: "$hfn",
        vdom.raw: "$raw",
        vdom.fragment: "$fragment",
        ServerError: "ServerError",
    }
    for tag in html.TAGS.values():
        special[tag] = f"$h({json.dumps(tag.name)})"
    return {id(obj): js for obj, js in special.items()}


@dataclass
class _Unit:
    name: str
    code: str | None = None


def _fail(message, hint=None):
    raise CompileError(message, hint=hint)


class Bundler:
    def __init__(self, roots=()):
        self.roots = [Path(r).resolve() for r in roots]
        self.units: dict[int, _Unit] = {}
        self.order: list[_Unit] = []
        self._pinned: list = []  # keep compiled objects alive so their ids stay unique
        self._names: set[str] = set()
        self._special = _special_objects()

    # -- units -----------------------------------------------------------------

    def _reserve(self, base: str) -> str:
        base = re.sub(r"\W", "_", base)
        name, n = base, 1
        while name in self._names:
            n += 1
            name = f"{base}_{n}"
        self._names.add(name)
        return name

    def _add(self, obj, base: str, make_code) -> str:
        unit = self.units.get(id(obj))
        if unit is not None:
            return unit.name
        unit = _Unit(self._reserve(base))
        self.units[id(obj)] = unit  # registered first so recursive references resolve
        self._pinned.append(obj)
        try:
            unit.code = make_code(unit.name)
        except BaseException:
            del self.units[id(obj)]
            raise
        self.order.append(unit)
        return unit.name

    def _allowed(self, path: Path) -> bool:
        path = path.resolve()
        if path.is_relative_to(JONGO_DIR):
            return True
        if "site-packages" in path.parts or ".venv" in path.parts:
            return False
        return any(path.is_relative_to(root) for root in self.roots)

    # -- name resolution (called by the transpiler) ----------------------------------

    def resolve(self, name: str, obj, fail=_fail) -> str:
        special = self._special.get(id(obj))
        if special is not None:
            return special
        if obj is None or isinstance(obj, (bool, int, float, str)):
            return json.dumps(obj)
        if isinstance(obj, Component):
            return self.component(obj)
        if isinstance(obj, ServerFunction):
            return self._add(
                obj, f"rpc__{obj.fn.__name__}", lambda _: f"$rpc({json.dumps(obj.id)}, {json.dumps(obj.refresh)})"
            )
        if isinstance(obj, Tag):
            return f"$h({json.dumps(obj.name)})"
        if isinstance(obj, StyleSheet):
            return self._add(obj, f"styles__{name}", lambda _: json.dumps(obj.classes))
        if isinstance(obj, types.ModuleType):
            if obj.__name__ in BROWSER_MODULES:
                return f"$modules.{obj.__name__}"
            fail(
                f"module {obj.__name__!r} isn't available in browser code",
                "use it inside a @server function, or reach browser APIs through js.*",
            )
        module = getattr(obj, "__module__", None)
        if callable(obj) and module in BROWSER_MODULES and not isinstance(obj, type):
            return f"$modules.{module}.{obj.__name__}"
        if isinstance(obj, type):
            if hasattr(obj, "_meta") and hasattr(obj, "objects"):
                fail(
                    f"{name} is a database model, which only exists on the server",
                    f"load {name} rows in the page function and pass them as props, or call a @server function",
                )
            fail(f"class {name} can't be used in browser code", "use dicts, or move this logic into a @server function")
        if inspect.isfunction(obj):
            return self.function(name, obj, fail)
        if callable(obj):
            fail(f"{name} ({type(obj).__name__}) can't run in browser code", "call it inside a @server function")
        return self.constant(name, obj, fail)

    def component(self, comp: Component) -> str:
        return self._add(
            comp,
            f"{comp.fn.__module__}__{comp.name}",
            lambda _: f"$component({json.dumps(comp.id)}, {transpile_function(comp.fn, comp.name, self.resolve)})",
        )

    def function(self, name, fn, fail=_fail) -> str:
        if fn.__name__ == "<lambda>":
            fail(f"{name} is a lambda defined outside the component", "define it with def so it can be compiled")
        source = inspect.getsourcefile(fn)
        if not source or not self._allowed(Path(source)):
            fail(
                f"{name}() comes from {fn.__module__!r}, which can't run in the browser",
                "call it inside a @server function instead",
            )
        return self._add(fn, f"{fn.__module__}__{fn.__name__}", lambda js_name: transpile_function(fn, js_name, self.resolve))

    def constant(self, name, obj, fail=_fail) -> str:
        try:
            if isinstance(obj, VNode):
                code = f"$build({json.dumps(serialize(obj))})"
            else:
                code = json.dumps(to_json_data(obj, name))
                if '"$v"' in code:
                    code = f"$revive({code})"
        except JongoError as exc:
            fail(f"{name} can't be used in browser code: {exc}")
        return self._add(obj, f"const__{name}", lambda _: code)

    # -- output ------------------------------------------------------------------------

    def add_components(self, components=None):
        if components is None:
            components = sorted(COMPONENTS.values(), key=lambda c: c.id)
        for comp in components:
            self.component(comp)

    def render(self, footer: str = "") -> str:
        units = "\n\n".join(f"const {unit.name} = {unit.code};" for unit in self.order)
        return (
            '(function () {\n"use strict";\n'
            + runtime_source()
            + "\n\n// ---- compiled from Python -----------------------------------------\n\n"
            + units
            + "\n"
            + footer
            + "\n})();\n"
        )


def build_bundle(roots=(), components=None) -> tuple[str, str]:
    """Return ``(javascript, build_hash)`` for every registered component."""
    bundler = Bundler(roots)
    bundler.add_components(components)
    js = bundler.render(BOOT_FOOTER)
    return js, hashlib.sha1(js.encode()).hexdigest()[:12]
