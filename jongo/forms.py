"""Form schemas: validation rules written once, enforced in the browser and on the server.

    from jongo import forms

    Invite = forms.Schema(
        "Invite",
        email=forms.Email(max_length=200),
        role=forms.Choice(["member", "admin"], default="member"),
        seats=forms.Int(min=1, max=50, required=False),
    )

    @server
    def send_invite(request, data: Invite) -> dict:   # validated; ``data`` is the cleaned dict
        return Invitation.create(**data).to_dict()    # safe: only the schema's fields are in it

    @component
    def InviteForm():
        errors = state({})

        async def submit(event):
            values = form_values(event)
            found = forms.validate(Invite, values)     # the same function, compiled to JavaScript
            errors.set(found)
            if not found:
                try:
                    await send_invite(values)
                except ServerError as exc:             # what only the server can know
                    errors.set(exc.errors or {})

        return form(forms.Field(schema=Invite, name="email", error=errors.value.get("email")),
                    forms.Field(schema=Invite, name="role", error=errors.value.get("role")),
                    button("Send"), on_submit=submit)

There is one copy of the rules. A :class:`Schema` is plain data (a dict of rules), so it
is inlined into the browser bundle as JSON; :func:`validate` and :func:`check` are plain
Python that Jongo's compiler turns into JavaScript, so the browser runs the very code the
server runs. The server never trusts the browser's answer: a ``@server`` parameter
annotated with a schema is validated again before the function is called.

What keeps the two sides in step:

* Every message is rendered once, in Python, when the schema is built, so no number or
  string formatting happens in the browser.
* Patterns are rewritten to a form Python's ``re`` and JavaScript's ``RegExp`` read the
  same way (``\\d`` becomes ``[0-9]``, and so on); a pattern that can't be made portable is
  an error when the schema is defined (``J115``), not a surprise in production.
* ``tests/test_forms.py`` runs every rule through CPython and through the compiled
  JavaScript in Node and requires identical results.
"""
from __future__ import annotations

import re
import sys
import warnings

from .errors import JongoError
from .html import input_, label, option, select, span, textarea
from .vdom import component

__all__ = [
    "Schema", "Text", "Email", "Int", "Float", "Bool", "Choice",
    "validate", "check", "attrs", "matches", "Field", "portable_pattern",
    "EMAIL_PATTERN", "SCHEMAS",
]

#: An address with one ``@`` and a dot in the domain. Deliberately loose: the only real
#: test of an email address is sending to it.
EMAIL_PATTERN = r"[^@ \t\n\r\f\v]+@[^@ \t\n\r\f\v]+[.][^@ \t\n\r\f\v]+"

_INT = r"[ \t\n\r\f\v]*-?[0-9]+[ \t\n\r\f\v]*"
_FLOAT = r"[ \t\n\r\f\v]*-?(?:[0-9]+[.]?[0-9]*|[.][0-9]+)(?:[eE][-+]?[0-9]+)?[ \t\n\r\f\v]*"
#: Whole numbers above this can't be held exactly by a JavaScript number, so they are
#: refused on both sides instead of being silently rounded in the browser.
_MAX_SAFE = 9007199254740991

_TRUE = ["on", "true", "1", "yes"]
_FALSE = ["", "off", "false", "0", "no"]

_TYPES = ("text", "email", "int", "float", "bool", "choice")

#: Every schema defined in this process, by ``id()``: the registry ``jongo context`` reads,
#: like ``SERVER_FUNCTIONS`` and ``COMPONENTS``.
SCHEMAS: dict[int, "Schema"] = {}

#: Model fields :meth:`Schema.from_model` leaves out unless ``only=`` names them: a form built
#: from ``auth.User`` must not let a visitor set ``is_admin`` or write a password hash.
PRIVILEGED_FIELDS = frozenset(("is_admin", "is_staff", "is_superuser", "password"))


# ----------------------------------------------------------------------------- patterns --

_SHORTHAND = {"d": "0-9", "w": "A-Za-z0-9_", "s": " \\t\\n\\r\\f\\v"}
_NEGATED = {"D": "d", "W": "w", "S": "s"}
#: Characters a JavaScript ``u``-mode pattern lets you escape with a backslash.
_SYNTAX = set("^$\\.*+?()[]{}|/")
_PYTHON_ONLY = (
    (r"\(\?P[<=]", "named groups (?P<name>...)"),
    (r"\(\?[aiLmsux-]+[:)]", "inline flags like (?i)"),
    (r"\(\?[>#(]", "atomic groups, comments and conditionals"),
    (r"(?<!\\)(?:\\\\)*\\[AZzGNpP]", r"\A, \Z, \z, \N{...} and \p{...}"),
    (r"(?<!\\)(?:[*+?]|\})\+", "possessive quantifiers (*+, ++, ?+)"),
    (r"\(\?<[A-Za-z_]", "named groups (?<name>...)"),
    (r"(?<!\\)(?:\\\\)*\\[aU]", r"\a and \U escapes"),
    (r"(?<!\\)(?:\\\\)*\\0[0-9]", r"octal escapes like \012"),
)
#: A quantifier in braces: {m}, {m,} or {m,n}. Any other brace outside [...] (a literal {, a
#: lone }, Python's {,n}) is read differently, or refused, by JavaScript's Unicode mode.
_BRACE_QUANTIFIER = re.compile(r"\{[0-9]+(?:,[0-9]*)?\}")


def portable_pattern(pattern: str) -> str:
    """Rewrite a regular expression so Python and JavaScript read it the same way.

    ``\\d``, ``\\w`` and ``\\s`` mean different things in the two engines once the text
    isn't ASCII, and ``.`` stops at different line terminators. This spells them out as
    explicit character classes, and drops backslashes JavaScript's Unicode mode rejects
    (``\\@`` becomes ``@``).

    Parameters:
        pattern: a regular expression, matched against the whole value.

    Returns:
        The portable pattern; this is what the schema stores and both sides run.

    Raises:
        JongoError: code ``J115`` when the pattern uses syntax only one engine has (named
            groups, inline flags, ``\\A``/``\\Z``, ``\\a``, ``\\U``, octal escapes, possessive
            quantifiers, ``{,n}``, an unescaped literal ``{`` or ``}``), or doesn't compile.

    Example::

        portable_pattern(r"\\d{3}-\\w+")    # '[0-9]{3}-[A-Za-z0-9_]+'
    """
    if not isinstance(pattern, str):
        raise JongoError(f"a pattern must be a string, got {type(pattern).__name__}", code="J115")
    for probe, what in _PYTHON_ONLY:
        if re.search(probe, pattern):
            raise JongoError(
                f"the pattern {pattern!r} uses {what}, which Python and JavaScript don't share",
                code="J115", hint="write it with plain groups and character classes, e.g. [A-Za-z] instead of (?i)",
            )
    out, in_class, i = [], False, 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            nxt = pattern[i + 1]
            i += 2
            if nxt in _SHORTHAND:
                out.append(_SHORTHAND[nxt] if in_class else f"[{_SHORTHAND[nxt]}]")
            elif nxt in _NEGATED:
                if in_class:
                    raise JongoError(
                        f"the pattern {pattern!r} uses \\{nxt} inside [...], which can't be written portably",
                        code="J115", hint=f"move \\{nxt} out of the brackets or list the characters you want",
                    )
                out.append(f"[^{_SHORTHAND[_NEGATED[nxt]]}]")
            elif nxt.isalnum() or nxt in _SYNTAX or (in_class and nxt in "-"):
                out.append("\\" + nxt)
            else:
                out.append(nxt)  # \@ or \<space>: an identity escape JavaScript's u-mode refuses
            continue
        if ch == "[" and not in_class:
            in_class = True
            out.append(ch)
            i += 1
            if i < len(pattern) and pattern[i] == "^":
                out.append("^")
                i += 1
            if i < len(pattern) and pattern[i] == "]":  # a leading ] is a literal in Python only
                out.append("\\]")
                i += 1
            continue
        if ch == "]" and in_class:
            in_class = False
        elif ch in "{}" and not in_class:
            quantifier = _BRACE_QUANTIFIER.match(pattern, i) if ch == "{" else None
            if quantifier is None:
                what = "a {,n} quantifier" if pattern.startswith("{,", i) else f"a literal {ch}"
                raise JongoError(
                    f"the pattern {pattern!r} uses {what}, which Python and JavaScript don't read the same way",
                    code="J115", hint=f"escape a literal brace as \\{ch}, and write {{,n}} as {{0,n}}",
                )
            out.append(quantifier.group())
            i = quantifier.end()
            continue
        elif ch == "." and not in_class:
            out.append("[^\\n\\r]")
            i += 1
            continue
        elif ch == "[" and in_class:
            out.append("\\[")  # a literal [ inside a class must be escaped for JavaScript
            i += 1
            continue
        out.append(ch)
        i += 1
    result = "".join(out)
    try:
        re.compile(result, re.ASCII)
    except re.error as exc:
        raise JongoError(f"the pattern {pattern!r} isn't a valid regular expression: {exc}", code="J115",
                         hint="fix the expression; it is matched against the whole value") from None
    return result


def matches(pattern, text) -> bool:
    """True when ``text`` matches ``pattern`` from start to end.

    This is the one primitive that exists twice: here it is ``re.fullmatch``; in the
    browser the compiler substitutes ``new RegExp("^(?:pattern)$", "u")``. Give it
    patterns from :func:`portable_pattern` and the two agree.

    Example::

        matches("[0-9]+", "2024")     # True
        matches("[0-9]+", "2024 ")    # False: the whole value must match
    """
    return re.fullmatch(pattern, text, re.ASCII) is not None


# -------------------------------------------------------------------------------- rules --


def _number(value, what: str):
    """Bounds are stored as ints when they are whole, so 5.0 and 5 print identically."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JongoError(f"{what} must be a number, got {value!r}", code="J115")
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _rule(kind: str, *, label=None, required=True, default=None, help=None, messages=None, **rules) -> dict:
    rule = {"type": kind, "label": label, "required": bool(required)}
    if default is not None:
        rule["default"] = default
    if help:
        rule["help"] = str(help)
    for key, value in rules.items():
        if value is not None:
            rule[key] = value
    if messages:
        unknown = set(messages) - {"required", "type", "min_length", "max_length", "min", "max", "pattern",
                                   "choices", "same_as"}
        if unknown:
            raise JongoError(f"unknown message key(s) {', '.join(sorted(unknown))}", code="J115",
                             hint="messages are keyed by rule: required, type, min_length, max_length, min, "
                                  "max, pattern, choices, same_as")
        rule["messages"] = {str(k): str(v) for k, v in messages.items()}
    return rule


def Text(*, required: bool = True, min_length: int | None = None, max_length: int | None = None,
         pattern: str | None = None, same_as: str | None = None, strip: bool = True, multiline: bool = False,
         secret: bool = False, label: str | None = None, default: str | None = None, help: str | None = None,
         messages: dict | None = None) -> dict:
    """A text field.

    Parameters:
        required: an empty value is an error (default True).
        min_length, max_length: bounds on the length, counted in characters after stripping.
        pattern: a regular expression the whole value must match (made portable with
            :func:`portable_pattern`).
        same_as: the name of another field this one must equal (a password confirmation).
        strip: ignore leading and trailing whitespace (default True).
        multiline: render as a ``<textarea>`` in :class:`Field`.
        secret: render as ``<input type="password">`` (and never strip it: spaces in a
            password are part of the password).
        label: the human name used in messages and by :class:`Field`; defaults to the
            field name, capitalised.
        default: the cleaned value when the field is left empty (a field with a default is
            never "required": empty means "use the default").
        help: a line of guidance :class:`Field` shows under the control.
        messages: replace a default message, keyed by rule: ``{"pattern": "Use AB-123."}``.

    Returns:
        The rule, a plain dict (schemas are data).

    Example::

        forms.Text(min_length=2, max_length=40)
        forms.Text(min_length=8, secret=True)       # a password
        forms.Text(pattern=r"[A-Z]{2}-\\d{3}", messages={"pattern": "Use the form AB-123."})
    """
    rule = _rule("text", label=label, required=required, default=default, help=help, messages=messages,
                 min_length=min_length, max_length=max_length, same_as=same_as,
                 pattern=portable_pattern(pattern) if pattern is not None else None)
    rule["strip"] = bool(strip) and not secret
    if multiline:
        rule["multiline"] = True
    if secret:
        rule["secret"] = True
    for key in ("min_length", "max_length"):
        if key in rule and (isinstance(rule[key], bool) or not isinstance(rule[key], int) or rule[key] < 0):
            raise JongoError(f"{key} must be a non-negative whole number, got {rule[key]!r}", code="J115")
    if "min_length" in rule and "max_length" in rule and rule["min_length"] > rule["max_length"]:
        raise JongoError("min_length is greater than max_length", code="J115")
    return rule


def Email(*, required: bool = True, max_length: int | None = 254, label: str | None = None,
          default: str | None = None, help: str | None = None, messages: dict | None = None) -> dict:
    """An email address: text that must look like ``name@host.tld``.

    The check is deliberately loose (one ``@``, a dot in the domain, no spaces), because
    the only real test of an address is sending to it.

    Example::

        forms.Email()
        forms.Email(required=False, max_length=120)
    """
    rule = Text(required=required, max_length=max_length, label=label, default=default, help=help,
                messages=messages)
    rule["type"] = "email"
    rule["pattern"] = EMAIL_PATTERN
    return rule


def Int(*, required: bool = True, min: int | None = None, max: int | None = None, label: str | None = None,
        default: int | None = None, help: str | None = None, messages: dict | None = None) -> dict:
    """A whole number. Accepts a number or its text (``"42"``, as a form sends it).

    Parameters:
        min, max: inclusive bounds.

    Example::

        forms.Int(min=1, max=50)
    """
    rule = _rule("int", label=label, required=required, default=default, help=help, messages=messages,
                 min=_number(min, "min"), max=_number(max, "max"))
    _check_bounds(rule)
    return rule


def Float(*, required: bool = True, min: float | None = None, max: float | None = None,
          label: str | None = None, default: float | None = None, help: str | None = None,
          messages: dict | None = None) -> dict:
    """A number, whole or fractional. Accepts a number or its text (``"3.5"``, ``"1e3"``).

    Example::

        forms.Float(min=0, max=1)
    """
    rule = _rule("float", label=label, required=required, default=default, help=help, messages=messages,
                 min=_number(min, "min"), max=_number(max, "max"))
    _check_bounds(rule)
    return rule


def _check_bounds(rule: dict) -> None:
    if "min" in rule and "max" in rule and rule["min"] > rule["max"]:
        raise JongoError("min is greater than max", code="J115")


def Bool(*, required: bool = False, label: str | None = None, default: bool | None = None,
         help: str | None = None, messages: dict | None = None) -> dict:
    """A yes/no value, rendered as a checkbox.

    Accepts ``True``/``False`` and what a form sends (``"on"`` when ticked, nothing when
    not). ``required=True`` means it must be ticked, as for accepting terms. ``default``
    is only the box's starting state: a submitted form without the field is ``False``,
    because that is how browsers send an unticked box.

    Example::

        forms.Bool(required=True, label="I accept the terms")
    """
    return _rule("bool", label=label, required=required, default=default, help=help, messages=messages)


def Choice(choices, *, required: bool = True, label: str | None = None, default=None, help: str | None = None,
           messages: dict | None = None) -> dict:
    """One value from a fixed list, rendered as a ``<select>``.

    Parameters:
        choices: the allowed values: ``["member", "admin"]``, or ``(value, label)`` pairs
            such as ``[("m", "Member"), ("a", "Admin")]``. Values are strings or whole numbers.

    The cleaned value is the choice itself (an int stays an int even though a form sends text).

    Example::

        forms.Choice(["member", "admin"], default="member")
        forms.Choice([(1, "Low"), (2, "High")])
    """
    values, labels = [], []
    for item in choices:
        value, text = (item[0], item[1]) if isinstance(item, (tuple, list)) and len(item) == 2 else (item, item)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise JongoError(f"a choice must be a string or a whole number, got {value!r}", code="J115")
        values.append(value)
        labels.append(str(text))
    if not values:
        raise JongoError("Choice() needs at least one choice", code="J115")
    if len({str(v) for v in values}) != len(values):
        raise JongoError("two choices have the same text, so a form couldn't tell them apart", code="J115")
    return _rule("choice", label=label, required=required, default=default, help=help, messages=messages,
                 choices=values, choice_labels=labels)


def _default_messages(name: str, rule: dict, labels: dict) -> dict:
    """Every message this rule can produce, rendered now so the browser formats nothing."""
    what = rule["label"]
    kind = rule["type"]
    out = {"required": "This box must be ticked." if kind == "bool" else f"{what} is required."}
    if kind == "int":
        out["type"] = f"{what} must be a whole number."
    elif kind == "float":
        out["type"] = f"{what} must be a number."
    elif kind == "bool":
        out["type"] = f"{what} must be yes or no."
    else:
        out["type"] = f"{what} must be text."
    if "min_length" in rule:
        n = rule["min_length"]
        out["min_length"] = f"{what} must be at least {n} character{'' if n == 1 else 's'}."
    if "max_length" in rule:
        n = rule["max_length"]
        out["max_length"] = f"{what} must be at most {n} character{'' if n == 1 else 's'}."
    if "min" in rule:
        out["min"] = f"{what} must be at least {rule['min']}."
    if "max" in rule:
        out["max"] = f"{what} must be at most {rule['max']}."
    if "pattern" in rule:
        out["pattern"] = "Enter a valid email address." if kind == "email" else f"{what} isn't in the right format."
    if "choices" in rule:
        out["choices"] = f"{what} must be one of: {', '.join(rule['choice_labels'])}."
    if "same_as" in rule:
        out["same_as"] = f"{what} must match {labels.get(rule['same_as'], rule['same_as']).lower()}."
    return out


def _default_label(name: str) -> str:
    text = name.replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else name


class Schema(dict):
    """A named set of field rules: one definition for the browser, the server and the docs.

    A schema is a ``dict`` of ``field name -> rule`` (the rules come from :func:`Text`,
    :func:`Email`, :func:`Int`, :func:`Float`, :func:`Bool` and :func:`Choice`), so it can
    be used as a prop, read in a component and inlined into the browser bundle as data.

    Parameters:
        name: optional first positional argument; names the schema in ``jongo context``,
            the OpenAPI document and MCP tool schemas (default ``"Schema"``).
        **fields: the rules, by field name.

    Use it four ways:

    * ``forms.validate(schema, values)`` in a component, for errors as the user types or
      on submit, in the browser;
    * ``data: schema`` on a ``@server`` parameter, to have the call validated and receive
      the cleaned dict;
    * ``schema.clean(values)`` (or ``schema(values)``) anywhere on the server;
    * ``forms.Field(schema=schema, name=...)`` to render a labelled control.

    Example::

        Signup = forms.Schema(
            "Signup",
            email=forms.Email(),
            password=forms.Text(min_length=8),
            confirm=forms.Text(same_as="password", label="Confirmation"),
            terms=forms.Bool(required=True, label="The terms"),
        )
        Signup.validate({"email": "x"})     # {"email": "Enter a valid email address.", ...}
        Signup.clean(request.form)          # the cleaned dict, or raises db.ValidationError
    """

    #: How ``@server`` argument checking and the schema exporters recognise a schema.
    __jongo_schema__ = True

    def __init__(self, *name, **fields):
        if len(name) > 1 or (name and not isinstance(name[0], str)):
            raise JongoError("Schema takes one optional positional argument, its name", code="J115",
                             hint='forms.Schema("Signup", email=forms.Email())')
        super().__init__()
        self.__name__ = name[0] if name else "Schema"
        self.__qualname__ = self.__name__
        # Where it was defined, for `jongo context`: the first caller outside this module.
        frame = sys._getframe(1)
        while frame is not None and frame.f_globals.get("__name__") == __name__:
            frame = frame.f_back
        self.__module__ = frame.f_globals.get("__name__", "") if frame is not None else ""
        self.file = frame.f_code.co_filename if frame is not None else None
        self.line = frame.f_lineno if frame is not None else None
        SCHEMAS[id(self)] = self
        labels = {}
        for field, rule in fields.items():
            if not isinstance(rule, dict) or rule.get("type") not in _TYPES:
                raise JongoError(
                    f"{self.__name__}.{field} isn't a field rule (got {type(rule).__name__})", code="J115",
                    hint="use forms.Text(), forms.Email(), forms.Int(), forms.Float(), forms.Bool() or forms.Choice()",
                )
            rule = dict(rule)
            rule["label"] = rule.get("label") or _default_label(field)
            if "default" in rule and rule["type"] != "bool":
                rule["required"] = False  # leaving it empty means "use the default", never an error
            labels[field] = rule["label"]
            self[field] = rule
        for field, rule in self.items():
            other = rule.get("same_as")
            if other is not None and other not in self:
                raise JongoError(f"{self.__name__}.{field}: same_as={other!r} names a field that isn't in the schema",
                                 code="J115")
            default = rule.get("default")
            messages = _default_messages(field, rule, labels)
            messages.update(rule.get("messages") or {})
            rule["messages"] = messages
            if default is not None and check(rule, default, None):
                raise JongoError(f"{self.__name__}.{field}: the default {default!r} doesn't pass the field's own rules",
                                 code="J115")

    # A Schema is used as a type annotation (``data: Signup``), and ``typing`` only accepts
    # annotations that are callable; calling one cleans a value, which is also just handy.
    def __call__(self, values) -> dict:
        return self.clean(values)

    def __repr__(self) -> str:
        return f"<Schema {self.__name__}: {', '.join(self) or 'no fields'}>"

    def __hash__(self) -> int:  # identity: schemas are module-level definitions
        return id(self)

    def __eq__(self, other) -> bool:
        return self is other

    def __ne__(self, other) -> bool:
        return self is not other

    def validate(self, values) -> dict:
        """Check ``values`` and return ``{field: message}`` for every field that fails.

        An empty dict means the values are valid. Fields not in the schema are ignored.
        The same check runs in the browser through :func:`validate`.
        """
        return validate(self, values if values is not None else {})

    def clean(self, values) -> dict:
        """Validate ``values`` and return them cleaned: typed, stripped, defaults applied.

        Only the schema's own fields are returned, so a browser can't slip in extra
        keys (an ``is_admin`` next to the email) for a later ``Model.create(**data)``.

        Parameters:
            values: a dict (``form_values(event)``, ``request.form``, a JSON body).

        Returns:
            ``{field: value}`` for every field: text stripped, ``Int`` as int, ``Float``
            as float, ``Bool`` as bool, ``Choice`` as the declared choice. An optional
            field left empty is its ``default``, else ``None`` (``""`` for text, ``False``
            for a bool).

        Raises:
            jongo.db.ValidationError: with ``.errors`` mapping fields to messages. From a
                ``@server`` function the browser receives it as a 400 whose ``errors``
                the component can show (code ``J304``).

        Example::

            data = Signup.clean({"email": " a@b.co ", "password": "long enough", ...})
            data["email"]      # "a@b.co"
        """
        from .db import ValidationError

        if not isinstance(values, dict):
            raise ValidationError({"__all__": f"{self.__name__} expects an object with fields, "
                                              f"got {type(values).__name__}."})
        errors = validate(self, values)
        if errors:
            raise ValidationError(errors)
        return {name: _convert(rule, values.get(name)) for name, rule in self.items()}

    def defaults(self) -> dict:
        """The starting values for a form: each field's ``default``, else ``""``/``False``.

        Example::

            values = state(Signup.defaults())
        """
        return {name: rule["default"] if "default" in rule else (False if rule["type"] == "bool" else "")
                for name, rule in self.items()}

    @classmethod
    def from_model(cls, model, *, only=None, exclude=(), name: str | None = None) -> "Schema":
        """Build a schema from a ``db.Model``: its fields' rules become the form's rules.

        ``max_length``, ``choices``, ``blank``/``null`` and the field type are read from the
        model, so the form can't drift from the table. Fields that aren't ``editable``,
        ``auto_now`` timestamps and types a form can't express (JSON) are left out.

        Privileged fields (``is_admin``, ``is_staff``, ``is_superuser``, ``password``: see
        :data:`PRIVILEGED_FIELDS`) are left out too unless ``only=`` names them, so a schema
        built from ``auth.User`` can't make a visitor an admin. Prefer ``only=``: a form should
        list what it lets people change.

        Parameters:
            model: the model class.
            only: field names to include (default: every editable field except the privileged
                ones). A field named here is included even if it is privileged.
            exclude: field names to leave out.
            name: the schema's name (default: the model's name + ``"Form"``).

        Returns:
            A new :class:`Schema`. Add or replace fields by building another from it:
            ``forms.Schema("Edit", **{**base, "notes": forms.Text(required=False)})``.

        Example::

            class Invite(db.Model):
                email = db.Text(max_length=200, unique=True)
                role = db.Text(max_length=20, default="member", choices=["member", "admin"])

            InviteForm = forms.Schema.from_model(Invite, only=["email", "role"])
        """
        from .db import fields as dbf

        rules = {}
        skipped = []
        for field in model._meta.fields:
            if only is not None and field.name not in only:
                continue
            if field.name in exclude or not getattr(field, "editable", True):
                continue
            if only is None and field.name in PRIVILEGED_FIELDS:
                skipped.append(field.name)
                continue
            required = bool(field.required)
            common = {"required": required, "label": field.label, "help": getattr(field, "help", None) or None}
            default = None if callable(field.default) or field.default is dbf.NOTHING else field.default
            key = field.attname if isinstance(field, dbf.ForeignKey) else field.name
            if field.choices and isinstance(field, (dbf.Text, dbf.Int)):
                pairs = [(c[0], c[1]) if isinstance(c, (tuple, list)) else (c, c) for c in field.choices]
                rule = Choice(pairs, **common)
            elif isinstance(field, dbf.Text):
                rule = Text(max_length=getattr(field, "max_length", None), **common)
            elif isinstance(field, dbf.ForeignKey):
                rule = Int(min=1, **common)
            elif isinstance(field, dbf.Int):
                rule = Int(**common)
            elif isinstance(field, dbf.Float):
                rule = Float(**common)
            elif isinstance(field, dbf.Bool):
                rule = Bool(label=field.label, help=common["help"])
            else:
                continue  # DateTime, Date, JSON: no portable rule yet
            if default is not None:
                probe = {**rule, "label": field.label}
                probe["messages"] = _default_messages(key, probe, {})
                if not check(probe, default, None):  # a default the form's own rules accept
                    rule["default"] = default
            rules[key] = rule
        if skipped:
            from .diagnostics import declared_production

            if not declared_production():
                warnings.warn(
                    f"Schema.from_model({model.__name__}) left out {', '.join(skipped)}: privileged fields are only "
                    "included when only=[...] names them", stacklevel=2)
        return cls(name or f"{model.__name__}Form", **rules)


def _convert(rule: dict, value):
    """Turn a value that has passed :func:`check` into its Python type (server only)."""
    kind = rule["type"]
    if kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in _TRUE
        return bool(value)  # absent means unticked: that is how a form sends an empty checkbox
    if value is None or (isinstance(value, str) and value.strip() == ""):
        if "default" in rule:
            return rule["default"]
        return "" if kind in ("text", "email") else None
    if kind == "int":
        if not isinstance(value, str):
            return int(value)
        text = value.strip()  # leading zeros are dropped first: int() refuses over 4300 digits
        digits = (text[1:] if text[:1] == "-" else text).lstrip("0") or "0"
        return -int(digits) if text[:1] == "-" else int(digits)
    if kind == "float":
        return float(value.strip()) if isinstance(value, str) else float(value)
    if kind == "choice":
        for choice in rule["choices"]:
            if str(choice) == str(value).strip():
                return choice
    return value.strip() if rule.get("strip", True) else value


# ------------------------------------------------- the part that also runs in the browser --
#
# Everything below is compiled to JavaScript when a component uses it, so it sticks to
# what both sides do identically: dict and list access, comparisons, len(), str methods.
# No formatting (messages are pre-rendered), no regular expressions except matches().


def check(rule, value, values=None):
    """Check one value against one rule; return the error message, or ``""`` if it passes.

    Runs on the server and, compiled, in the browser: use it for as-you-type feedback.

    Parameters:
        rule: a field's rule, ``schema["email"]``.
        value: what the user entered: text from an input, or a number/bool from JSON.
        values: all the form's values, needed only for ``same_as`` rules.

    Returns:
        The message for the first rule the value breaks, or ``""``.

    Example::

        input_(on_input=lambda e: error.set(forms.check(Signup["email"], e.target.value)))
    """
    kind = rule["type"]
    messages = rule["messages"]
    if kind == "bool":
        truthy = False
        falsy = value is None
        if isinstance(value, bool):
            truthy = value
            falsy = not value
        elif isinstance(value, str):
            lowered = value.strip().lower()
            truthy = lowered in _TRUE
            falsy = lowered in _FALSE
        if not truthy and not falsy:
            return messages["type"]
        if rule["required"] and not truthy:
            return messages["required"]
        return ""

    if value is None or (isinstance(value, str) and value.strip() == ""):
        if rule["required"]:
            return messages["required"]
        return ""

    if kind == "int" or kind == "float":
        number = None
        if isinstance(value, bool):
            return messages["type"]
        if isinstance(value, str):
            text = value.strip()
            if kind == "int" and matches(_INT, text):
                # More than 16 significant digits is past what a JavaScript number holds (and
                # thousands of digits would make Python's int() raise), so it isn't a number here.
                negative = text[:1] == "-"
                digits = (text[1:] if negative else text).lstrip("0") or "0"
                if len(digits) <= 16:
                    number = -int(digits) if negative else int(digits)
            elif kind == "float" and matches(_FLOAT, text):
                number = float(text)
        elif isinstance(value, (int, float)):
            if kind == "float" or value % 1 == 0:
                number = value
        if number is None or number != number:  # NaN (from JSON or Python) compares false with every bound
            return messages["type"]
        if kind == "int" and (number > _MAX_SAFE or number < -_MAX_SAFE):
            return messages["type"]
        if kind == "float" and (number > 1e308 or number < -1e308):
            return messages["type"]
        if "min" in rule and number < rule["min"]:
            return messages["min"]
        if "max" in rule and number > rule["max"]:
            return messages["max"]
        return ""

    if kind == "choice":
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return messages["choices"]
        wanted = value.strip() if isinstance(value, str) else str(value)
        for choice in rule["choices"]:
            if str(choice) == wanted:
                return ""
        return messages["choices"]

    if not isinstance(value, str):
        return messages["type"]
    text = value.strip() if rule["strip"] else value
    size = len(text)
    if "min_length" in rule and size < rule["min_length"]:
        return messages["min_length"]
    if "max_length" in rule and size > rule["max_length"]:
        return messages["max_length"]
    if "pattern" in rule and not matches(rule["pattern"], text):
        return messages["pattern"]
    if "same_as" in rule and values is not None:
        other = values.get(rule["same_as"])
        if isinstance(other, str) and rule["strip"]:
            other = other.strip()
        if other != text:
            return messages["same_as"]
    return ""


def validate(schema, values):
    """Check every field of ``schema``; return ``{field: message}`` for the ones that fail.

    The same function on both sides of the wire: called on the server it runs as Python,
    and used in a ``@component`` it is compiled to JavaScript with the schema inlined as
    data. An empty dict means the values are valid.

    Parameters:
        schema: a :class:`Schema`.
        values: a dict of the form's values, e.g. ``form_values(event)``.

    Returns:
        A dict of error messages by field name; empty when everything passes.

    Example::

        async def submit(event):
            values = form_values(event)
            found = forms.validate(Signup, values)
            errors.set(found)
            if not found:
                await sign_up(values)
    """
    errors = {}
    for name in schema:
        message = check(schema[name], values.get(name), values)
        if message:
            errors[name] = message
    return errors


def attrs(schema, name):
    """The HTML attributes for a field's control, derived from its rule.

    Gives native browser behaviour (the right keyboard on a phone, ``required``,
    ``maxlength``, number steppers) from the same definition: ``name``, ``type``,
    ``required``, ``minlength``, ``maxlength``, ``min``, ``max``, ``step``, ``inputmode``,
    ``autocomplete``. Patterns are not emitted as a ``pattern`` attribute: they are
    enforced by :func:`validate`, which runs the identical expression on both sides.

    Example::

        input_(**forms.attrs(Signup, "email"), value=email.value)
    """
    rule = schema[name]
    kind = rule["type"]
    out = {"name": name}
    if kind == "email":
        out["type"] = "email"
        out["autocomplete"] = "email"
    elif kind == "int":
        out["type"] = "number"
        out["step"] = "1"
        out["inputmode"] = "numeric"
    elif kind == "float":
        out["type"] = "number"
        out["step"] = "any"
        out["inputmode"] = "decimal"
    elif kind == "bool":
        out["type"] = "checkbox"
    elif kind == "text":
        out["type"] = "password" if rule.get("secret") else "text"
    if rule["required"]:
        out["required"] = True
    if "min_length" in rule:
        out["minlength"] = rule["min_length"]
    if "max_length" in rule:
        out["maxlength"] = rule["max_length"]
    if "min" in rule:
        out["min"] = rule["min"]
    if "max" in rule:
        out["max"] = rule["max"]
    return out


@component
def Field(schema, name, value=None, error=None, on_input=None, on_change=None):
    """A labelled control for one schema field, with its error message.

    Renders ``<label class="jongo-field">`` holding the label text, the right control for
    the rule (``<input>``, ``<textarea>``, ``<select>`` for a :func:`Choice`, a checkbox
    for a :func:`Bool`), the field's ``help`` text and, when ``error`` is set, the message
    in ``<span class="jongo-field-error" role="alert">``. It is unstyled: style the two
    classes with ``css()``/``global_css()``.

    Props:
        schema: the :class:`Schema`.
        name: which field.
        value: the current value. Omit it for an uncontrolled field read with
            ``form_values(event)`` on submit.
        error: the message to show (e.g. ``errors.value.get("email")``).
        on_input, on_change: event handlers passed to the control.

    Example::

        forms.Field(schema=Signup, name="email", error=errors.value.get("email"))
    """
    rule = schema[name]
    kind = rule["type"]
    props = attrs(schema, name)
    if error:
        props["aria_invalid"] = "true"
    if on_input is not None:
        props["on_input"] = on_input
    if on_change is not None:
        props["on_change"] = on_change

    if kind == "bool":
        if value is not None:
            props["checked"] = value is True or value == "on"
        elif rule.get("default") is True:
            props["checked"] = True
        control = input_(**props)
    elif kind == "choice":
        chosen = "" if value is None else str(value)
        if chosen == "" and "default" in rule:
            chosen = str(rule["default"])
        choices = rule["choices"]
        labels = rule["choice_labels"]
        options = [] if chosen != "" and (rule["required"] or "default" in rule) else [option("", value="")]
        for i in range(len(choices)):
            options.append(option(labels[i], value=str(choices[i]), selected=str(choices[i]) == chosen))
        props.pop("type", None)
        control = select(options, **props)
    else:
        if value is not None:
            props["value"] = value
        if "default" in rule:
            props["placeholder"] = str(rule["default"])  # left empty, this is what the field becomes
        if rule.get("multiline"):
            props.pop("type", None)
            control = textarea(**props)
        else:
            control = input_(**props)

    return label(
        control if kind == "bool" else None,
        span(rule["label"], class_="jongo-field-label"),
        None if kind == "bool" else control,
        span(rule["help"], class_="jongo-field-help") if rule.get("help") else None,
        span(error, class_="jongo-field-error", role="alert") if error else None,
        class_="jongo-field",
    )

