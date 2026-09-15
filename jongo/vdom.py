"""The virtual DOM shared by the server renderer and the browser runtime.

A page is a tree of VNodes. On the server the tree renders to an HTML string, and
the same tree is serialised to JSON so the browser can hydrate it with the
compiled components. Every rule here (prop names, class lists, style dicts) is
mirrored exactly in ``compiler/runtime.js``.
"""
from __future__ import annotations

import contextvars
import dataclasses
import datetime
import decimal
import functools
import html as _html
import inspect
import re
import uuid

from .errors import JongoError

VOID_TAGS = frozenset("area base br col embed hr img input link meta source track wbr".split())
RAW_TEXT_TAGS = frozenset(("script", "style"))
UNITLESS_CSS = frozenset(
    "opacity z-index flex flex-grow flex-shrink font-weight line-height order zoom scale "
    "grid-row grid-column aspect-ratio".split()
)


class VNode:
    """One node of a UI tree: an element (``type`` is a tag name) or a component."""

    __slots__ = ("type", "props", "children", "key")

    def __init__(self, type, props=None, children=None, key=None):
        self.type = type
        self.props = props if props is not None else {}
        self.children = children if children is not None else []
        self.key = key

    @property
    def is_component(self) -> bool:
        return isinstance(self.type, Component)

    def __repr__(self):
        name = self.type.name if self.is_component else self.type
        return f"<VNode {name} props={dict(self.props)!r} children={len(self.children)}>"

    def __str__(self):
        return render_to_string(self)

    __html__ = __str__


# ---------------------------------------------------------------------------
# Props and children normalisation (mirrored in runtime.js)

_KEEP_PROPS = frozenset(("style", "ref", "value", "checked", "selected", "inner_html"))


def prop_name(name: str) -> str:
    """Map a Python keyword argument to its DOM name.

    ``class_`` -> ``class``, ``on_click`` -> ``on:click``, ``aria_label`` -> ``aria-label``.
    """
    if name.startswith("on_"):
        return "on:" + name[3:].replace("_", "").lower()
    if name in _KEEP_PROPS:
        return name
    if name in ("class_", "cls", "className"):
        return "class"
    return name.rstrip("_").replace("_", "-")


def class_names(value) -> str:
    """``["btn", {"active": True, "big": False}]`` -> ``"btn active"``."""
    if value is None or value is False or value is True:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(str(k) for k, v in value.items() if v)
    if isinstance(value, (list, tuple)):
        return " ".join(s for s in (class_names(v) for v in value) if s)
    return str(value)


_CAMEL = re.compile(r"(?<=[a-z0-9])([A-Z])")


def css_property(name: str) -> str:
    if name.startswith("--"):
        return name
    return _CAMEL.sub(r"-\1", name).replace("_", "-").lower()


def css_value(prop: str, value) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)) and prop not in UNITLESS_CSS and not prop.startswith("--"):
        return f"{value}px" if value != 0 else "0"
    return str(value)


def style_text(style) -> str:
    if style is None:
        return ""
    if isinstance(style, str):
        return style
    parts = []
    for key, value in style.items():
        if value is None or value is False:
            continue
        prop = css_property(key)
        parts.append(f"{prop}: {css_value(prop, value)}")
    return "; ".join(parts)


def flatten(children, out=None) -> list:
    """Flatten nested child lists, dropping ``None``/``True``/``False``."""
    if out is None:
        out = []
    for child in children:
        if child is None or child is True or child is False:
            continue
        if isinstance(child, (VNode, str)):
            out.append(child)
        elif isinstance(child, (int, float)):
            out.append(str(child))
        elif isinstance(child, (dict, bytes)):
            out.append(str(child))
        elif hasattr(child, "__iter__"):
            flatten(child, out)
        else:
            out.append(str(child))
    return out


# ---------------------------------------------------------------------------
# Tags and components


class Tag:
    """A callable HTML element factory: ``div("hi", class_="box")``."""

    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def __call__(self, *children, key=None, **props):
        return VNode(self.name, {prop_name(k): v for k, v in props.items()}, flatten(children), key)

    def __repr__(self):
        return f"<tag {self.name}>"


def h(tag: str, *children, key=None, **props) -> VNode:
    """Create an element by name, e.g. custom elements: ``h("my-widget", size=3)``."""
    return Tag(tag)(*children, key=key, **props)


def raw(markup: str, tag: str = "span") -> VNode:
    """Insert trusted HTML without escaping, wrapped in ``tag``."""
    return VNode(tag, {"inner_html": markup}, [])


def fragment(*children) -> list:
    return flatten(children)


COMPONENTS: dict[str, "Component"] = {}


class Component:
    """A UI function that runs on the server (SSR) and compiles to the browser."""

    def __init__(self, fn):
        if inspect.iscoroutinefunction(fn):
            raise JongoError(f"@component {fn.__name__} can't be async; load data in the page or a @server function")
        functools.update_wrapper(self, fn)
        self.fn = fn
        self.name = fn.__name__
        self.id = f"{fn.__module__}.{fn.__qualname__}".replace(".<locals>", "")
        params = inspect.signature(fn).parameters
        self.takes_children = "children" in params
        self.takes_kwargs = any(p.kind is p.VAR_KEYWORD for p in params.values())
        COMPONENTS[self.id] = self

    def __call__(self, *children, key=None, **props):
        return VNode(self, props, flatten(children), key)

    def call(self, props, children):
        kwargs = dict(props)
        if self.takes_children:
            kwargs["children"] = list(children)
        elif children:
            if not self.takes_kwargs:
                raise TypeError(f"<{self.name}> was given children but has no `children` parameter")
            kwargs["children"] = list(children)
        return self.fn(**kwargs)

    def __repr__(self):
        return f"<component {self.id}>"


def component(fn) -> Component:
    """Mark a function as a UI component.

    The body is plain Python. It renders on the server for the first paint and is
    compiled to JavaScript so it can re-render in the browser when its state changes.
    """
    return Component(fn)


# ---------------------------------------------------------------------------
# Hooks (server side: render once, never re-render)

_rendering: contextvars.ContextVar = contextvars.ContextVar("jongo_rendering", default=None)


def _require_render(hook: str):
    if _rendering.get() is None:
        raise JongoError(f"{hook}() can only be called while a @component is rendering")


class State:
    """A reactive value. Assigning ``.value`` re-renders the component in the browser."""

    __slots__ = ("_value",)

    def __init__(self, value):
        self._value = value

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, new):
        self._value = new

    def set(self, new):
        self._value = new

    def update(self, fn):
        self._value = fn(self._value)

    def __repr__(self):
        return f"State({self._value!r})"


class Ref:
    __slots__ = ("current",)

    def __init__(self, current=None):
        self.current = current


def state(initial=None) -> State:
    """Component state: ``count = state(0)`` then ``count.value += 1`` in a handler."""
    _require_render("state")
    if callable(initial) and not isinstance(initial, (Component, Tag)):
        initial = initial()
    return State(initial)


def effect(fn, deps=None) -> None:
    """Run ``fn`` in the browser after render. ``deps=[]`` runs it once on mount.

    If ``fn`` returns a function, it's called before the next run and on unmount.
    Effects never run during server rendering.
    """
    _require_render("effect")


def ref(initial=None) -> Ref:
    """A mutable box; pass as ``ref=`` to an element to get its DOM node in ``.current``."""
    _require_render("ref")
    return Ref(initial)


def _browser_only(name):
    def fn(*args, **kwargs):
        raise JongoError(f"{name}() only runs in the browser: call it from an event handler or effect()")

    fn.__name__ = name
    return fn


navigate = _browser_only("navigate")
refresh = _browser_only("refresh")
form_values = _browser_only("form_values")


class _BrowserGlobals:
    """``js.window``, ``js.localStorage``... compile to the browser's globals."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        raise JongoError(f"js.{name} only exists in the browser; use it in an event handler or effect()")

    def __repr__(self):
        return "<browser globals>"


js = _BrowserGlobals()


# ---------------------------------------------------------------------------
# Server rendering


def render_component(comp: Component, props, children):
    token = _rendering.set(comp)
    try:
        result = comp.call(props, children)
    except Exception as exc:
        if hasattr(exc, "add_note"):
            exc.add_note(f"  while rendering <{comp.name}> ({comp.id})")
        raise
    finally:
        _rendering.reset(token)
    if isinstance(result, (list, tuple)):
        raise JongoError(f"<{comp.name}> returned a list; components must return a single element (wrap it in div())")
    return result


def render_to_string(node) -> str:
    out: list[str] = []
    _render(node, out, None)
    return "".join(out)


def _render(node, out, select_value) -> bool:
    """Append HTML for ``node``; returns True when it emitted a bare text node."""
    if node is None or node is True or node is False:
        return False
    if isinstance(node, (list, tuple)):
        _render_children(flatten(node), out, select_value)
        return False
    if not isinstance(node, VNode):
        out.append(_html.escape(str(node), quote=False))
        return True
    if isinstance(node.type, Component):
        return _render(render_component(node.type, node.props, node.children), out, select_value)

    tag = node.type
    props = node.props
    if tag == "option" and select_value is not None and "selected" not in props:
        props = {**props, "selected": str(props.get("value")) == str(select_value)}
    out.append("<" + tag)
    _render_attrs(tag, props, out)
    out.append(">")
    if tag in VOID_TAGS:
        return False
    if "inner_html" in props:
        out.append(str(props["inner_html"] or ""))
    elif tag == "textarea" and props.get("value") is not None:
        out.append(_html.escape(str(props["value"]), quote=False))
    elif tag in RAW_TEXT_TAGS:
        for child in node.children:
            out.append(str(child).replace("</", "<\\/"))
    else:
        child_select = props.get("value") if tag == "select" else select_value
        _render_children(node.children, out, child_select)
    out.append(f"</{tag}>")
    return False


def _render_children(children, out, select_value):
    previous_text = False
    for child in children:
        start = len(out)
        if isinstance(child, str) and previous_text:
            out.append("<!---->")  # keeps adjacent text nodes separate for hydration
            start = len(out)
        emitted_text = _render(child, out, select_value)
        if len(out) > start:
            previous_text = emitted_text


def _render_attrs(tag, props, out):
    for name, value in props.items():
        if name.startswith("on:") or name in ("ref", "inner_html"):
            continue
        if tag == "textarea" and name == "value":
            continue
        if tag == "select" and name == "value":
            continue
        if value is None or value is False:
            continue
        if name == "class":
            value = class_names(value)
            if not value:
                continue
        elif name == "style":
            value = style_text(value)
            if not value:
                continue
        if value is True:
            out.append(" " + name)
        else:
            out.append(f' {name}="{_html.escape(str(value), quote=True)}"')


# ---------------------------------------------------------------------------
# JSON bridge between server and browser


class AttrDict(dict):
    """Dict with attribute access, so ``todo.title`` and ``todo["title"]`` both work
    on the server exactly like they do on browser objects."""

    __slots__ = ()

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name, value):
        self[name] = value

    def __delattr__(self, name):
        try:
            del self[name]
        except KeyError:
            raise AttributeError(name) from None


def to_json_data(value, where: str = "value"):
    """Convert ``value`` to plain JSON data the browser can receive."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, VNode):
        return {"$v": serialize(value)}
    if isinstance(value, dict):
        return {str(k): to_json_data(v, f"{where}[{k!r}]") for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_json_data(v, f"{where}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return to_json_data(value.to_dict(), where)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_json_data(dataclasses.asdict(value), where)
    if isinstance(value, State):
        return to_json_data(value.value, where)
    if callable(value):
        raise JongoError(
            f"{where} is a function ({getattr(value, '__name__', value)!r}). Functions can't be sent to the "
            "browser as data; reference them inside a @component instead"
        )
    if hasattr(value, "__iter__") and not isinstance(value, (bytes, bytearray)):
        return [to_json_data(v, f"{where}[{i}]") for i, v in enumerate(value)]
    raise JongoError(f"{where} ({type(value).__name__}) can't be sent to the browser; convert it to dict/list/str")


def serialize(node):
    """VNode tree -> JSON-able data: ``{"t": tag, "p": props, "c": children, "k": key}``
    for elements and ``{"C": component_id, ...}`` for components."""
    if isinstance(node, str):
        return node
    if not isinstance(node, VNode):
        raise JongoError(f"can't render {type(node).__name__} as UI")
    if isinstance(node.type, Component):
        data = {"C": node.type.id, "p": to_json_data(node.props, f"<{node.type.name}> props")}
    else:
        props = {}
        for name, value in node.props.items():
            if name.startswith("on:") or name == "ref":
                if value is None:
                    continue
                raise JongoError(
                    f"<{node.type} {name.replace('on:', 'on_')}=...> is outside any @component. Event handlers "
                    "and refs run in the browser, so move this markup into a @component function"
                )
            props[name] = to_json_data(value, f"<{node.type}> {name}")
        data = {"t": node.type, "p": props}
    if node.children:
        data["c"] = [serialize(c) for c in node.children]
    if node.key is not None:
        data["k"] = node.key
    return data


def build(data):
    """JSON tree (from :func:`serialize`) -> VNode tree with AttrDict props."""
    if isinstance(data, str):
        return data
    children = [build(c) for c in data.get("c", ())]
    props = revive(data.get("p") or {})
    if "C" in data:
        comp = COMPONENTS.get(data["C"])
        if comp is None:
            raise JongoError(f"unknown component {data['C']!r}")
        return VNode(comp, props, children, data.get("k"))
    return VNode(data["t"], props, children, data.get("k"))


def revive(value):
    if isinstance(value, dict):
        if len(value) == 1 and "$v" in value:
            return build(value["$v"])
        return AttrDict({k: revive(v) for k, v in value.items()})
    if isinstance(value, list):
        return [revive(v) for v in value]
    return value
