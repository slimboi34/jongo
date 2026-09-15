"""Styles as Python dicts, scoped to generated class names.

    styles = css(
        card={
            "padding": 16,                  # numbers become px
            "border_radius": "12px",        # snake_case becomes kebab-case
            ":hover": {"background": "#f6f6f6"},
            "& h2": {"margin": 0},
            "@media (max-width: 600px)": {"padding": 8},
        },
    )

    div(h2("Hi"), class_=styles.card)       # -> class="card-1a2b3c"

Every stylesheet is collected into /_jongo/app.css. ``styles.card`` works the
same in server rendering and in browser code.
"""
from __future__ import annotations

import hashlib
import json
import sys

from .vdom import css_property, css_value

_REGISTRY: dict[str, "StyleSheet | GlobalStyle"] = {}


def rule_css(selector: str, declarations: dict) -> str:
    own, nested = [], []
    for key, value in declarations.items():
        if isinstance(value, dict):
            if key.startswith("@"):
                nested.append(f"{key} {{\n{rule_css(selector, value)}\n}}")
            elif "&" in key:
                nested.append(rule_css(key.replace("&", selector), value))
            elif key.startswith(":"):
                nested.append(rule_css(selector + key, value))
            else:
                nested.append(rule_css(f"{selector} {key}", value))
        elif value is not None and value is not False:
            prop = css_property(key)
            own.append(f"  {prop}: {css_value(prop, value)};")
    out = []
    if own:
        out.append(selector + " {\n" + "\n".join(own) + "\n}")
    out.extend(nested)
    return "\n".join(out)


class StyleSheet:
    def __init__(self, rules: dict, module: str):
        payload = json.dumps(rules, sort_keys=True, default=str)
        digest = hashlib.sha1(f"{module}:{payload}".encode()).hexdigest()[:6]
        self.rules = rules
        self.module = module
        self.classes = {name: f"{name.replace('_', '-')}-{digest}" for name in rules}
        _REGISTRY[f"sheet:{module}:{digest}"] = self

    def __getattr__(self, name):
        if name.startswith("__") or name in ("rules", "module", "classes"):
            raise AttributeError(name)
        try:
            return self.classes[name]
        except KeyError:
            raise AttributeError(f"stylesheet has no class {name!r}") from None

    def __getitem__(self, name):
        return self.classes[name]

    def to_dict(self) -> dict:
        return dict(self.classes)

    def css(self) -> str:
        return "\n".join(rule_css("." + cls, self.rules[name]) for name, cls in self.classes.items())

    def __repr__(self):
        return f"<StyleSheet {', '.join(self.classes)}>"


class GlobalStyle:
    def __init__(self, text: str, module: str):
        self.text = text
        digest = hashlib.sha1(f"{module}:{text}".encode()).hexdigest()[:8]
        _REGISTRY[f"global:{module}:{digest}"] = self

    def css(self) -> str:
        return self.text


def css(rules: dict | None = None, **named) -> StyleSheet:
    """Create a scoped stylesheet. Each keyword becomes a class name."""
    module = sys._getframe(1).f_globals.get("__name__", "")
    return StyleSheet({**(rules or {}), **named}, module)


def global_css(rules: dict | str) -> GlobalStyle:
    """Unscoped CSS: ``global_css({"body": {"margin": 0}})`` or a raw CSS string."""
    module = sys._getframe(1).f_globals.get("__name__", "")
    text = rules if isinstance(rules, str) else "\n".join(rule_css(sel, decl) for sel, decl in rules.items())
    return GlobalStyle(text, module)


def collect_css() -> str:
    return "\n\n".join(item.css() for item in _REGISTRY.values())
