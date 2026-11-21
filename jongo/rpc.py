"""Server functions: Python that browser code calls like a normal (async) function.

    @server
    def add_todo(title: str) -> dict:
        return Todo.create(title=title).to_dict()

    # inside a @component event handler:
    todo = await add_todo(draft.value)

Arguments are validated against the type hints before your function runs, and a
parameter annotated with a model class receives the instance for the id the
browser sent. A first parameter named ``request`` receives the current request.

The hints Jongo checks: ``str``, ``int``, ``float``, ``bool``, ``None``, ``date``,
``datetime``, ``time`` (ISO 8601 text), ``UUID``, ``Decimal``, ``bytes`` (base64 text), an
``Enum`` (by value), ``Literal``, unions, ``list``/``tuple``/``set``/``dict`` of those, a
dataclass, a ``TypedDict``, a ``db.Model`` and a ``forms.Schema``; ``Any`` (or no hint)
accepts any JSON. A function annotated with anything else is refused when it is defined
(``J405``), because its argument would otherwise arrive unchecked.
"""
from __future__ import annotations

import asyncio
import base64
import collections.abc
import dataclasses
import datetime
import decimal
import enum
import functools
import inspect
import math
import re
import sys
import types
import typing
import uuid

from .errors import HTTPError, JongoError, NotFound

SERVER_FUNCTIONS: dict[str, "ServerFunction"] = {}


class ServerFunction:
    def __init__(self, fn, *, login_required=False, admin_required=False, refresh=False, rate_limit=None):
        from .ratelimit import parse as _parse_rate

        functools.update_wrapper(self, fn)
        #: The parsed ``rate_limit=`` (a :class:`jongo.ratelimit.RateLimit`), or None.
        self.rate_limit = _parse_rate(rate_limit)
        self.fn = fn
        self.id = f"{fn.__module__}.{fn.__qualname__}".replace(".<locals>", "")
        self.login_required = login_required or admin_required
        self.admin_required = admin_required
        self.refresh = refresh
        self.signature = inspect.signature(fn)
        params = list(self.signature.parameters.values())
        self.wants_request = bool(params) and params[0].name == "request"
        self._hints = None
        existing = SERVER_FUNCTIONS.get(self.id)
        # Factory/closure-built server functions share {module}.{qualname}; without this the
        # registry would silently keep only the last, so calling endpoint A would run B.
        if existing is not None and existing.fn is not fn and "<locals>" in fn.__qualname__:
            raise JongoError(
                f"two @server functions resolve to the same id {self.id!r}. They're built inside "
                f"another function, so they collide — move them to module level or give them distinct names.",
                code="J406", file=fn.__code__.co_filename, line=fn.__code__.co_firstlineno,
            )
        self._check_hints_now()
        SERVER_FUNCTIONS[self.id] = self

    def __call__(self, *args, **kwargs):
        return self.fn(*args, **kwargs)

    def __repr__(self):
        return f"<server function {self.id}>"

    @property
    def hints(self) -> dict:
        if self._hints is None:
            try:
                hints = typing.get_type_hints(self.fn)
            except Exception:
                hints = self._resolve_hints()
            self._require_checkable(hints, deferred_ok=False)
            self._hints = hints
        return self._hints

    def _check_hints_now(self) -> None:
        """Refuse an annotation Jongo can't check when the function is defined, not on a call.

        Hints that don't resolve yet (a forward reference to a model defined further down
        the module) are checked when they are first resolved instead.
        """
        try:
            hints = typing.get_type_hints(self.fn)
        except Exception:
            return
        self._require_checkable(hints, deferred_ok=True)

    def _require_checkable(self, hints: dict, *, deferred_ok: bool) -> None:
        for name in self.signature.parameters:
            if name not in hints or (self.wants_request and name == "request"):
                continue
            problem = _uncheckable(hints[name], deferred_ok=deferred_ok)
            if problem is None:
                continue
            what = f"{name}: {_label(hints[name])}"
            raise JongoError(
                f"@server {self.fn.__name__}(): can't check arguments of type {problem} (in {what}). "
                "Arguments from the browser are checked against these hints before the function runs, "
                "so it would receive whatever JSON was sent.",
                code="J405", file=self.fn.__code__.co_filename, line=self.fn.__code__.co_firstlineno,
                hint="annotate it with a type Jongo checks (str, int, float, bool, date, datetime, time, UUID, "
                     "Decimal, bytes, an Enum, a Literal, a list/tuple/set/dict of those, a dataclass, a TypedDict, "
                     "a db.Model or a forms.Schema), or with Any to take any JSON and check it yourself",
            )

    def _resolve_hints(self) -> dict:
        """Resolve annotations that ``get_type_hints`` could not, or refuse to run.

        Silently falling back to "no hints" would turn off the checks this boundary
        exists for: an unvalidated string would reach an ``int`` parameter, and a raw id
        would reach a parameter that is supposed to receive a loaded row. A function whose
        parameters cannot be resolved is a programming error, so it is reported as one.
        """
        from .db.models import models_registry

        models = {model.__name__: model for model in models_registry.values()}
        forms = sys.modules.get("jongo.forms")
        if forms is not None:  # form schemas defined where get_type_hints can't see, by their name
            models.update({schema.__name__: schema for schema in forms.SCHEMAS.values()
                           if schema.__name__ not in models})
        try:  # most often a model class defined somewhere get_type_hints can't see
            return typing.get_type_hints(self.fn, localns=models)
        except Exception:
            pass

        # Resolve one parameter at a time, so an unresolvable *return* annotation — which
        # is never used for validation — doesn't take the whole function down with it.
        namespace = {**vars(typing), **getattr(self.fn, "__globals__", {}), **models}
        hints, unresolved = {}, []
        for name, parameter in inspect.signature(self.fn).parameters.items():
            annotation = parameter.annotation
            if annotation is inspect.Parameter.empty:
                continue
            if not isinstance(annotation, str):
                hints[name] = annotation
                continue
            try:
                resolved = eval(annotation, namespace)  # noqa: S307 - the author's own source
            except Exception:
                unresolved.append(f"{name}: {annotation}")
                continue
            if isinstance(resolved, str):  # a quoted forward reference that still names nothing
                unresolved.append(f"{name}: {resolved}")
                continue
            hints[name] = resolved
        if unresolved:
            raise JongoError(
                f"@server {self.fn.__name__}(): can't resolve the type of "
                + ", ".join(unresolved)
                + ". Arguments from the browser are checked against these hints, so the "
                "function is not safe to call without them — define the annotated type "
                "where the function can see it (module level), or drop the annotation.",
                code="J405", file=self.fn.__code__.co_filename, line=self.fn.__code__.co_firstlineno,
            )
        return hints

    def _public_signature(self) -> str:
        """``(title: str, done: bool = False)`` — what the browser may send, for error hints."""
        parts = []
        for name, p in self.signature.parameters.items():
            if self.wants_request and name == "request":
                continue
            prefix = "*" if p.kind is p.VAR_POSITIONAL else "**" if p.kind is p.VAR_KEYWORD else ""
            text = prefix + name
            if name in self.hints:
                text += f": {_label(self.hints[name])}"
            if p.default is not p.empty:
                text += f" = {p.default!r}"
            parts.append(text)
        return f"({', '.join(parts)})"

    def invoke(self, request, args, kwargs):
        """Validate browser-supplied arguments and call the function."""
        user = getattr(request, "user", None)
        if self.login_required and not user:
            raise HTTPError(401, "Please log in first", code="J208")
        if self.admin_required and not getattr(user, "is_admin", False):
            raise HTTPError(403, "Admins only", code="J209")
        if self.rate_limit is not None:
            from .ratelimit import check as _check_rate

            _check_rate(request, self.rate_limit, f"@server {self.id}")
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            raise HTTPError(400, 'malformed server function call: "args" must be a list and "kwargs" an object',
                            code="J402")
        call_args = [request, *args] if self.wants_request else list(args)
        params = self.signature.parameters
        if not any(p.kind is p.VAR_KEYWORD for p in params.values()):
            unknown = [k for k in kwargs if k not in params or (self.wants_request and k == "request")
                       or params[k].kind is inspect.Parameter.POSITIONAL_ONLY]
            if unknown:  # name the typo, rather than the "missing argument" bind() would report first
                raise HTTPError(400, f"{self.fn.__name__}(): unexpected keyword argument(s) "
                                     f"{', '.join(map(repr, unknown))}", code="J404",
                                hint=f"the signature is {self.fn.__name__}{self._public_signature()}")
        try:
            bound = self.signature.bind(*call_args, **kwargs)
        except TypeError as exc:
            raise HTTPError(400, f"{self.fn.__name__}(): {exc}", code="J404",
                            hint=f"the signature is {self.fn.__name__}{self._public_signature()}") from None

        for name, value in list(bound.arguments.items()):
            if self.wants_request and name == "request" or name not in self.hints:
                continue
            hint = self.hints[name]
            param = self.signature.parameters[name]
            if param.kind is param.VAR_POSITIONAL:
                bound.arguments[name] = tuple(coerce(v, hint, name) for v in value)
            elif param.kind is param.VAR_KEYWORD:
                bound.arguments[name] = {k: coerce(v, hint, k) for k, v in value.items()}
            else:
                bound.arguments[name] = coerce(value, hint, name)

        result = self.fn(*bound.args, **bound.kwargs)
        if inspect.isawaitable(result):
            result = _run_sync(result)
        return result


async def _await(awaitable):
    return await awaitable


def _run_sync(awaitable):
    """Run an awaitable to completion, whether or not an event loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_await(awaitable))
    # Inside a running loop (async host): run in a fresh loop on a worker thread.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(_await(awaitable))).result()


def server(fn=None, *, login_required=False, admin_required=False, refresh=False, rate_limit=None):
    """Expose a function to browser code.

    Parameters:
        login_required: refuse callers who aren't signed in (401, ``J208``).
        admin_required: refuse callers who aren't admins (403, ``J209``).
        refresh: re-run the current page's loader after each successful call, so data
            rendered from the page updates without extra code.
        rate_limit: a cap per caller, e.g. ``"5/minute"`` (see :mod:`jongo.ratelimit`). Each
            call counts against the client address and, when signed in, the user as well.
            Over the limit the function doesn't run and the browser gets a 429 (``J213``).

    A parameter annotated with a :class:`jongo.forms.Schema` is validated against it and
    arrives as the cleaned dict. ``date``/``datetime``/``time`` arrive parsed from ISO 8601
    text, ``UUID`` and ``Decimal`` from text (``Decimal`` also from a number), ``bytes`` from
    base64 and an ``Enum`` from its value. A hint Jongo can't check is refused here (``J405``).

    Example::

        @server(login_required=True, rate_limit="5/minute")
        def send_invite(request, email: str) -> dict:
            return Invite.create(email=email).to_dict()
    """

    def wrap(f):
        return ServerFunction(f, login_required=login_required, admin_required=admin_required, refresh=refresh,
                              rate_limit=rate_limit)

    return wrap(fn) if fn is not None else wrap


# ---------------------------------------------------------------------------
# Argument coercion


def _label(hint) -> str:
    return getattr(hint, "__name__", None) or str(hint).replace("typing.", "")


def _bad(name, hint, value):
    raise _arg_error(f"argument {name!r} must be {_label(hint)}, got {type(value).__name__}",
                     f"send {name} as {_label(hint)} (convert it in the browser before the call)")


def _arg_error(message, hint=None):
    return HTTPError(400, message, code="J403", hint=hint)


def _is_model(hint) -> bool:
    return isinstance(hint, type) and hasattr(hint, "_meta") and hasattr(hint, "objects")


def _is_typeddict(hint) -> bool:
    return (isinstance(hint, type) and issubclass(hint, dict) and hasattr(hint, "__total__")
            and hasattr(hint, "__annotations__"))


def _is_enum(hint) -> bool:
    return isinstance(hint, type) and issubclass(hint, enum.Enum)


_UNIONS = (typing.Union, getattr(types, "UnionType", typing.Union))
#: Containers that arrive as a JSON array, and what each becomes.
_AS_LIST = (list, collections.abc.Sequence, collections.abc.MutableSequence, collections.abc.Iterable,
            collections.abc.Collection)
_AS_SET = {set: set, frozenset: frozenset, collections.abc.Set: frozenset, collections.abc.MutableSet: set}
_SET_TYPES = tuple(_AS_SET)  # membership by identity: an annotation needn't be hashable
_MAPPINGS = (dict, collections.abc.Mapping, collections.abc.MutableMapping)
#: Types checked by a branch of their own in coerce().
_SCALARS = (bool, int, float, str, bytes, datetime.date, datetime.datetime, datetime.time, uuid.UUID,
            decimal.Decimal)

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_ISO_TIME = r"(\d{2}:\d{2})(?::(\d{2})(?:[.,](\d+))?)?([Zz]|[+-]\d{2}:?\d{2})?"
_ISO_TIME_RE = re.compile(_ISO_TIME, re.ASCII)
_ISO_DATETIME = re.compile(r"(\d{4}-\d{2}-\d{2})[Tt ]" + _ISO_TIME, re.ASCII)
_DECIMAL = re.compile(r"\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*", re.ASCII)
_BASE64 = re.compile(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")
#: The most digits (and the largest exponent) a Decimal argument may have: the same bound
#: Python puts on int() of a string, so converting one can't cost the server unbounded work.
_DECIMAL_DIGITS = 4300


def _iso_time(found) -> str:
    """The time part of an ISO 8601 match, in the subset ``fromisoformat`` reads on every Python."""
    hours_minutes, seconds, fraction, offset = found
    text = hours_minutes + ":" + (seconds or "00")
    if fraction:
        text += "." + fraction[:6].ljust(6, "0")
    if offset:
        if offset in ("Z", "z"):
            offset = "+00:00"
        elif ":" not in offset:
            offset = offset[:3] + ":" + offset[3:]
        text += offset
    return text


def _parse_temporal(value, hint):
    """A date, datetime or time from ISO 8601 text, or None if the text isn't one."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        if hint is datetime.date:
            return datetime.date.fromisoformat(value) if _ISO_DATE.fullmatch(value) else None
        if hint is datetime.datetime:
            found = _ISO_DATETIME.fullmatch(value)
            if found:
                return datetime.datetime.fromisoformat(f"{found.group(1)}T{_iso_time(found.groups()[1:])}")
            return None
        found = _ISO_TIME_RE.fullmatch(value)
        return datetime.time.fromisoformat(_iso_time(found.groups())) if found else None
    except ValueError:  # the right shape but not a real moment: month 13, hour 25
        return None


_TEMPORAL_EXAMPLES = {datetime.date: "2026-11-21", datetime.datetime: "2026-11-21T09:30:00Z",
                      datetime.time: "09:30:00"}


def _coerce_scalar(value, hint, name):
    """The new-in-0.7 scalar types: each arrives as JSON text (or a number, for Decimal)."""
    if hint in _TEMPORAL_EXAMPLES:
        if type(value) is hint:  # already parsed (a Python caller)
            return value
        parsed = _parse_temporal(value, hint)
        if parsed is None:
            raise _arg_error(f"argument {name!r} must be an ISO 8601 {hint.__name__}, got {_shown(value)}",
                             f"send {name} as text like {_TEMPORAL_EXAMPLES[hint]!r} (.isoformat() in Python)")
        return parsed
    if hint is uuid.UUID:
        if isinstance(value, uuid.UUID):
            return value
        if isinstance(value, str) and len(value) <= 64:
            try:
                return uuid.UUID(value)
            except ValueError:
                pass
        raise _arg_error(f"argument {name!r} must be a UUID, got {_shown(value)}",
                         f"send {name} as text like '12345678-1234-5678-1234-567812345678'")
    if hint is decimal.Decimal:
        result = None
        if isinstance(value, decimal.Decimal):
            result = value
        elif isinstance(value, int) and not isinstance(value, bool):
            result = decimal.Decimal(value)
        elif isinstance(value, float):
            result = decimal.Decimal(repr(value))  # 0.1 stays 0.1, not its binary expansion
        elif isinstance(value, str) and len(value) <= _DECIMAL_DIGITS and _DECIMAL.fullmatch(value):
            result = decimal.Decimal(value.strip())
        if result is None:
            raise _arg_error(f"argument {name!r} must be a decimal number, got {_shown(value)}",
                             f"send {name} as a number, or as text like '19.99'")
        if not result.is_finite():
            raise _arg_error(f"argument {name!r} must be a finite number", "NaN and Infinity aren't decimals")
        if len(result.as_tuple().digits) > _DECIMAL_DIGITS or abs(result.adjusted()) > _DECIMAL_DIGITS:
            raise _arg_error(f"argument {name!r} is out of range",
                             f"decimals may have at most {_DECIMAL_DIGITS} digits and an exponent within "
                             f"±{_DECIMAL_DIGITS}")
        return result
    # bytes
    if isinstance(value, bytes):
        return value
    if isinstance(value, str) and _BASE64.fullmatch(value):
        return base64.b64decode(value)
    raise _arg_error(f"argument {name!r} must be base64 text, got {_shown(value)}",
                     f"send {name} base64-encoded (btoa() in the browser, base64.b64encode() in Python)")


def _shown(value) -> str:
    if isinstance(value, str):
        return repr(value if len(value) <= 40 else value[:40] + "…")
    return type(value).__name__


def _uncheckable(hint, *, deferred_ok: bool, seen: set | None = None) -> str | None:
    """The part of ``hint`` coerce() can't check (a label for the error), or None if it checks it all.

    ``deferred_ok``: a dataclass or TypedDict whose own field hints don't resolve yet is
    accepted for now (it is checked again when the function's hints are first used).
    """
    if hint is typing.Any or hint is object or hint is inspect.Parameter.empty or hint is None \
            or hint is type(None):
        return None
    if isinstance(hint, (str, typing.ForwardRef)):  # a name that never resolved
        name = hint if isinstance(hint, str) else hint.__forward_arg__
        return f"{name!r} (a name that doesn't resolve)"
    if getattr(hint, "__jongo_schema__", False) or _is_model(hint) or _is_enum(hint):
        return None
    if any(hint is scalar for scalar in _SCALARS):
        return None
    supertype = getattr(hint, "__supertype__", None)  # typing.NewType
    if supertype is not None:
        return _uncheckable(supertype, deferred_ok=deferred_ok, seen=seen)
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if origin is typing.Annotated:
        return _uncheckable(args[0], deferred_ok=deferred_ok, seen=seen)
    if origin is typing.Literal:
        return None
    container = origin or hint
    if origin in _UNIONS or container is tuple or container in _AS_LIST or container in _SET_TYPES \
            or container in _MAPPINGS:
        for arg in args:
            if arg is Ellipsis:
                continue
            found = _uncheckable(arg, deferred_ok=deferred_ok, seen=seen)
            if found:
                return found
        return None
    is_dataclass = dataclasses.is_dataclass(hint) and isinstance(hint, type)
    if is_dataclass or _is_typeddict(hint):
        seen = set() if seen is None else seen
        if hint in seen:  # a recursive structure: already being checked
            return None
        seen.add(hint)
        try:
            field_hints = typing.get_type_hints(hint)
        except Exception:
            return None if deferred_ok else f"{hint.__name__} (its field types don't resolve)"
        names = [f.name for f in dataclasses.fields(hint)] if is_dataclass else list(field_hints)
        for field in names:
            found = _uncheckable(field_hints.get(field, typing.Any), deferred_ok=deferred_ok, seen=seen)
            if found:
                return found
        return None
    return _label(hint)


def coerce(value, hint, name="value"):
    """Check ``value`` (decoded JSON) against a type hint, converting where it's safe.

    Raises a 400 ``HTTPError`` (``J403``) when the value doesn't fit. A hint this function
    has no rule for is passed through unchanged; ``@server`` refuses such hints when the
    function is defined (``J405``), so that only happens for page and route query parameters.
    """
    if hint is typing.Any or hint is inspect.Parameter.empty or hint is object:
        return value
    if getattr(hint, "__jongo_schema__", False):  # a jongo.forms.Schema: validate, return the cleaned dict
        if not isinstance(value, dict):
            raise _arg_error(f"argument {name!r} must be an object with the fields "
                             f"{', '.join(hint) or '(none)'}, got {type(value).__name__}",
                             f"send {name} as a dict, e.g. form_values(event)")
        return hint.clean(value)
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)

    if origin is typing.Annotated:
        return coerce(value, args[0], name)
    supertype = getattr(hint, "__supertype__", None)  # typing.NewType("UserId", int)
    if supertype is not None:
        return coerce(value, supertype, name)
    if origin in _UNIONS:
        if value is None and type(None) in args:
            return None
        # An option the value already is, exactly, wins over one it could be converted to:
        # int | str keeps "007" the text it was sent as, and int | float keeps 2.0 a float.
        for option in args:
            if type(value) is option and any(option is t for t in (str, int, float, bool)):
                return coerce(value, option, name)
        first_error = None
        for option in args:
            if option is type(None):
                continue
            try:
                return coerce(value, option, name)
            except HTTPError as exc:
                first_error = first_error or exc
        if first_error:
            raise first_error
        _bad(name, hint, value)
    if origin is typing.Literal:
        # Match by value AND exact type, so JSON false/1.0 don't satisfy Literal[0,1,2,3]
        # (Python's 0 == False, 1 == 1.0 would otherwise let the wrong type through).
        for option in args:
            if type(value) is type(option) and value == option:
                return value
        raise _arg_error(f"argument {name!r} must be one of {', '.join(map(repr, args))}",
                         "send exactly one of the allowed values, with the same type")
    if hint is None or hint is type(None):
        if value is None:
            return None
        _bad(name, hint, value)
    if hint is bool:
        if isinstance(value, bool):
            return value
        _bad(name, hint, value)
    if hint is int:
        result = None
        if isinstance(value, int) and not isinstance(value, bool):
            result = value
        elif isinstance(value, float) and value.is_integer():
            result = int(value)
        elif isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
            try:
                result = int(value)  # can raise on absurdly long strings (Python's digit-limit)
            except ValueError:
                raise _arg_error(f"argument {name!r} is too large") from None
        if result is None:
            _bad(name, hint, value)
        if not -(2**63) <= result < 2**63:  # bound numbers too, not just strings; SQLite stores 64-bit
            raise _arg_error(f"argument {name!r} is out of range", "integers must fit in 64 bits")
        return result
    if hint is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                result = float(value)
            except OverflowError:  # an int past float's range: 10**400
                raise _arg_error(f"argument {name!r} must be a finite number",
                                 "the number is too large for a float") from None
        elif isinstance(value, str):
            try:
                result = float(value)
            except ValueError:
                _bad(name, hint, value)
        else:
            _bad(name, hint, value)
        if not math.isfinite(result):  # inf/nan can't round-trip to valid JSON
            raise _arg_error(f"argument {name!r} must be a finite number")
        return result
    if hint is str:
        if isinstance(value, str):
            return value
        _bad(name, hint, value)
    if any(hint is scalar for scalar in _SCALARS):  # date, datetime, time, UUID, Decimal, bytes
        return _coerce_scalar(value, hint, name)
    if _is_enum(hint):
        if isinstance(value, hint):
            return value
        for member in hint:  # by value AND exact type, like Literal: JSON true is not 1
            if type(value) is type(member.value) and value == member.value:
                return member
        raise _arg_error(f"argument {name!r} must be one of {', '.join(repr(m.value) for m in hint)}",
                         f"send the value of a {hint.__name__} member, with the same type")

    container = origin or hint
    if container is tuple or container in _AS_LIST or container in _SET_TYPES:
        if not isinstance(value, list):
            _bad(name, hint, value)
        if container is tuple and args and args[-1] is not Ellipsis:
            if len(args) != len(value):
                raise _arg_error(f"argument {name!r} must have {len(args)} items")
            return tuple(coerce(v, t, f"{name}[{i}]") for i, (v, t) in enumerate(zip(value, args)))
        inner = args[0] if args else typing.Any
        items = [coerce(v, inner, f"{name}[{i}]") for i, v in enumerate(value)]
        if container is tuple:
            return tuple(items)
        return _AS_SET[container](items) if container in _SET_TYPES else items
    if container in _MAPPINGS:
        if not isinstance(value, dict):
            _bad(name, hint, value)
        key_type = args[0] if len(args) == 2 else typing.Any
        value_type = args[1] if len(args) == 2 else typing.Any
        return {
            coerce(k, key_type, f"{name} key {k!r}"): coerce(v, value_type, f"{name}[{k!r}]")
            for k, v in value.items()
        }
    if _is_model(hint):
        pk = value.get("id") if isinstance(value, dict) else value
        pk = coerce(pk, int, name)
        try:
            return hint.objects.get(id=pk)
        except Exception as exc:
            if type(exc).__name__ == "DoesNotExist" or "DoesNotExist" in {c.__name__ for c in type(exc).__mro__}:
                raise NotFound(f"{hint.__name__} #{pk} doesn't exist", code="J411") from None
            raise
    if dataclasses.is_dataclass(hint) and isinstance(hint, type):
        if not isinstance(value, dict):
            _bad(name, hint, value)
        field_hints = typing.get_type_hints(hint)
        known = {f.name for f in dataclasses.fields(hint)}
        try:
            return hint(**{k: coerce(v, field_hints.get(k, typing.Any), f"{name}.{k}") for k, v in value.items() if k in known})
        except TypeError as exc:
            raise _arg_error(f"argument {name!r}: {exc}") from None
    if _is_typeddict(hint):
        if not isinstance(value, dict):
            _bad(name, hint, value)
        field_hints = typing.get_type_hints(hint)
        required = getattr(hint, "__required_keys__", None)
        if required is None:
            required = frozenset(field_hints) if hint.__total__ else frozenset()
        missing = sorted(key for key in required if key not in value)
        if missing:
            raise _arg_error(f"argument {name!r} is missing {', '.join(map(repr, missing))}",
                             f"send {name} as an object with the keys of {hint.__name__}")
        # Only the declared keys, each checked: the same as for a dataclass.
        return {k: coerce(v, field_hints[k], f"{name}.{k}") for k, v in value.items() if k in field_hints}
    return value
