"""Server functions: Python that browser code calls like a normal (async) function.

    @server
    def add_todo(title: str) -> dict:
        return Todo.create(title=title).to_dict()

    # inside a @component event handler:
    todo = await add_todo(draft.value)

Arguments are validated against the type hints before your function runs, and a
parameter annotated with a model class receives the instance for the id the
browser sent. A first parameter named ``request`` receives the current request.
"""
from __future__ import annotations

import asyncio
import collections.abc
import dataclasses
import functools
import inspect
import re
import types
import typing

from .errors import HTTPError, NotFound

SERVER_FUNCTIONS: dict[str, "ServerFunction"] = {}


class ServerFunction:
    def __init__(self, fn, *, login_required=False, admin_required=False, refresh=False):
        functools.update_wrapper(self, fn)
        self.fn = fn
        self.id = f"{fn.__module__}.{fn.__qualname__}".replace(".<locals>", "")
        self.login_required = login_required or admin_required
        self.admin_required = admin_required
        self.refresh = refresh
        self.signature = inspect.signature(fn)
        params = list(self.signature.parameters.values())
        self.wants_request = bool(params) and params[0].name == "request"
        self._hints = None
        SERVER_FUNCTIONS[self.id] = self

    def __call__(self, *args, **kwargs):
        return self.fn(*args, **kwargs)

    def __repr__(self):
        return f"<server function {self.id}>"

    @property
    def hints(self) -> dict:
        if self._hints is None:
            try:
                self._hints = typing.get_type_hints(self.fn)
            except Exception:
                self._hints = {}
        return self._hints

    def invoke(self, request, args, kwargs):
        """Validate browser-supplied arguments and call the function."""
        user = getattr(request, "user", None)
        if self.login_required and not user:
            raise HTTPError(401, "Please log in first")
        if self.admin_required and not getattr(user, "is_admin", False):
            raise HTTPError(403, "Admins only")
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            raise HTTPError(400, "malformed server function call")
        call_args = [request, *args] if self.wants_request else list(args)
        try:
            bound = self.signature.bind(*call_args, **kwargs)
        except TypeError as exc:
            raise HTTPError(400, f"{self.fn.__name__}(): {exc}") from None

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
            result = asyncio.run(_await(result))
        return result


async def _await(awaitable):
    return await awaitable


def server(fn=None, *, login_required=False, admin_required=False, refresh=False):
    """Expose a function to browser code.

    ``refresh=True`` re-runs the current page's loader after each successful call,
    so data rendered from the page updates without extra code.
    """

    def wrap(f):
        return ServerFunction(f, login_required=login_required, admin_required=admin_required, refresh=refresh)

    return wrap(fn) if fn is not None else wrap


# ---------------------------------------------------------------------------
# Argument coercion


def _label(hint) -> str:
    return getattr(hint, "__name__", None) or str(hint).replace("typing.", "")


def _bad(name, hint, value):
    raise HTTPError(400, f"argument {name!r} must be {_label(hint)}, got {type(value).__name__}")


def _is_model(hint) -> bool:
    return isinstance(hint, type) and hasattr(hint, "_meta") and hasattr(hint, "objects")


def coerce(value, hint, name="value"):
    """Check ``value`` (decoded JSON) against a type hint, converting where it's safe."""
    if hint is typing.Any or hint is inspect.Parameter.empty or hint is object:
        return value
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)

    union_types = (typing.Union, getattr(types, "UnionType", typing.Union))
    if origin in union_types:
        if value is None and type(None) in args:
            return None
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
        if value in args:
            return value
        raise HTTPError(400, f"argument {name!r} must be one of {', '.join(map(repr, args))}")
    if hint is None or hint is type(None):
        if value is None:
            return None
        _bad(name, hint, value)
    if hint is bool:
        if isinstance(value, bool):
            return value
        _bad(name, hint, value)
    if hint is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
            return int(value)
        _bad(name, hint, value)
    if hint is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                pass
        _bad(name, hint, value)
    if hint is str:
        if isinstance(value, str):
            return value
        _bad(name, hint, value)

    container = origin or hint
    if container in (list, tuple, set, frozenset, collections.abc.Sequence, collections.abc.Iterable):
        if not isinstance(value, list):
            _bad(name, hint, value)
        if container is tuple and args and args[-1] is not Ellipsis:
            if len(args) != len(value):
                raise HTTPError(400, f"argument {name!r} must have {len(args)} items")
            return tuple(coerce(v, t, f"{name}[{i}]") for i, (v, t) in enumerate(zip(value, args)))
        inner = args[0] if args else typing.Any
        items = [coerce(v, inner, f"{name}[{i}]") for i, v in enumerate(value)]
        return items if container in (list, collections.abc.Sequence, collections.abc.Iterable) else container(items)
    if container in (dict, collections.abc.Mapping):
        if not isinstance(value, dict):
            _bad(name, hint, value)
        value_type = args[1] if len(args) == 2 else typing.Any
        return {k: coerce(v, value_type, f"{name}[{k!r}]") for k, v in value.items()}
    if _is_model(hint):
        pk = value.get("id") if isinstance(value, dict) else value
        pk = coerce(pk, int, name)
        try:
            return hint.objects.get(id=pk)
        except Exception as exc:
            if type(exc).__name__ == "DoesNotExist" or "DoesNotExist" in {c.__name__ for c in type(exc).__mro__}:
                raise NotFound(f"{hint.__name__} #{pk} doesn't exist") from None
            raise
    if dataclasses.is_dataclass(hint) and isinstance(hint, type):
        if not isinstance(value, dict):
            _bad(name, hint, value)
        field_hints = typing.get_type_hints(hint)
        known = {f.name for f in dataclasses.fields(hint)}
        try:
            return hint(**{k: coerce(v, field_hints.get(k, typing.Any), f"{name}.{k}") for k, v in value.items() if k in known})
        except TypeError as exc:
            raise HTTPError(400, f"argument {name!r}: {exc}") from None
    return value
