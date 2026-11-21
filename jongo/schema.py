"""The app's contract as data: JSON Schema for every type, and an OpenAPI document.

    from jongo import json_schema, openapi

    json_schema(list[int])     # {"type": "array", "items": {"type": "integer"}}
    openapi(app)               # an OpenAPI 3.1 document: every @server function, page and route

    jongo openapi [-o openapi.json]      # the same from the command line

A Jongo app has no hand-written API description: the type hints on ``@server`` functions
and the fields on models already are one. This module reads them from the live registries
(the same ones ``jongo context`` reads) and writes them in the two formats other tools
speak. Nothing here is declared twice, so it can't drift from the code, and
:mod:`jongo.mcp` uses the same schemas to describe an app's functions to an agent.
"""
from __future__ import annotations

import collections.abc
import dataclasses
import datetime
import decimal
import enum
import inspect
import json
import sys
import types
import typing
import uuid
from typing import Any

__all__ = ["json_schema", "model_schema", "form_schema", "function_schema", "openapi"]

_PRIMITIVES = {
    bool: {"type": "boolean"},
    int: {"type": "integer"},
    float: {"type": "number"},
    str: {"type": "string"},
    bytes: {"type": "string", "contentEncoding": "base64",
            "pattern": "^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$"},
    datetime.datetime: {"type": "string", "format": "date-time"},
    datetime.date: {"type": "string", "format": "date"},
    datetime.time: {"type": "string", "format": "time"},
    decimal.Decimal: {"type": "number"},
    uuid.UUID: {"type": "string", "format": "uuid"},
}
_UNIONS = (typing.Union, getattr(types, "UnionType", typing.Union))
_ARRAYS = (list, set, frozenset, collections.abc.Sequence, collections.abc.MutableSequence,
           collections.abc.Iterable, collections.abc.Collection, collections.abc.Set, collections.abc.MutableSet)
_SETS = (set, frozenset, collections.abc.Set, collections.abc.MutableSet)
_MAPPINGS = (dict, collections.abc.Mapping, collections.abc.MutableMapping)
#: An integer dict key, as JSON carries it (object keys are text) and coerce() reads it.
_INT_KEY = {"pattern": "^\\s*-?[0-9]+\\s*$"}
_CONVERTER_SCHEMAS = {
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "uuid": {"type": "string", "format": "uuid"},
    "slug": {"type": "string", "pattern": "^[-a-zA-Z0-9_]+$"},
    "str": {"type": "string"},
    "path": {"type": "string"},
}


def _is_model(hint) -> bool:
    return isinstance(hint, type) and hasattr(hint, "_meta") and hasattr(hint, "objects")


def _is_typeddict(hint) -> bool:
    return (isinstance(hint, type) and issubclass(hint, dict) and hasattr(hint, "__total__")
            and hasattr(hint, "__annotations__"))


def json_schema(hint, *, output: bool = False, models: dict | None = None) -> dict:
    """The JSON Schema (2020-12) for a Python type hint, as Jongo's ``@server`` boundary reads it.

    The mapping mirrors what :func:`jongo.rpc.coerce` accepts, so a value that satisfies
    the schema is a value the function will receive (two rules it doesn't spell out: an
    integer must fit in 64 bits, and a ``Literal``/``Enum`` value must have the exact type,
    so ``1.0`` isn't ``1``). ``coerce`` is more lenient in the other direction: it also
    reads numbers sent as text.

    Parameters:
        hint: a type or annotation: ``int``, ``list[str]``, ``dict[str, float]``,
            ``Optional[int]``, ``Literal["a", "b"]``, ``date``/``datetime``/``time``, ``UUID``,
            ``Decimal``, ``bytes``, an ``Enum``, a dataclass, a ``TypedDict``, a ``db.Model``
            class, or a ``forms.Schema``.
        output: describe a *returned* value instead of an argument. It only changes
            models: an argument annotated with a model is the row's integer id, while a
            returned model is the object ``to_dict()`` produces.
        models: when given, model classes met along the way are collected into it by name,
            and a returned model becomes a ``$ref`` to ``#/components/schemas/<Name>``
            (how :func:`openapi` builds its ``components``).

    Returns:
        A JSON Schema dict; ``{}`` (anything) for an unannotated or unknown type.

    Example::

        json_schema(int | None)        # {"anyOf": [{"type": "integer"}, {"type": "null"}]}
        json_schema(Todo)              # {"type": "integer", "description": "id of a Todo"}
        json_schema(Todo, output=True) # {"type": "object", "properties": {"id": ..., "title": ...}, ...}
    """
    if hint is None or hint is type(None):
        return {"type": "null"}
    if hint is typing.Any or hint is inspect.Parameter.empty or hint is object:
        return {}
    if getattr(hint, "__jongo_schema__", False):
        return form_schema(hint)
    for primitive, schema in _PRIMITIVES.items():
        if hint is primitive:
            return dict(schema)
    if isinstance(hint, type) and issubclass(hint, enum.Enum):  # sent and checked by value
        return {"title": hint.__name__, "enum": [member.value for member in hint]}
    supertype = getattr(hint, "__supertype__", None)  # typing.NewType
    if supertype is not None:
        return json_schema(supertype, output=output, models=models)
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)
    if origin in _UNIONS:
        options, seen = [], []
        for arg in args:
            option = json_schema(arg, output=output, models=models)
            if option not in seen:
                seen.append(option)
                options.append(option)
        return options[0] if len(options) == 1 else {"anyOf": options}
    if origin is typing.Literal:
        return {"enum": list(args)}
    if origin is typing.Annotated:
        return json_schema(args[0], output=output, models=models)
    container = origin or hint
    if container is tuple:
        if args and args[-1] is not Ellipsis:
            return {"type": "array", "prefixItems": [json_schema(a, output=output, models=models) for a in args],
                    "minItems": len(args), "maxItems": len(args)}
        items = json_schema(args[0], output=output, models=models) if args else {}
        return {"type": "array", "items": items} if items else {"type": "array"}
    if container in _ARRAYS:
        out: dict[str, Any] = {"type": "array"}
        if args:
            out["items"] = json_schema(args[0], output=output, models=models)
        if container in _SETS:
            out["uniqueItems"] = True
        return out
    if container in _MAPPINGS:
        out = {"type": "object"}
        if len(args) == 2:
            keys = _key_schema(args[0])
            if keys:
                out["propertyNames"] = keys
            if args[1] is not typing.Any:
                out["additionalProperties"] = json_schema(args[1], output=output, models=models)
        return out
    if _is_model(hint):
        if not output:
            return {"type": "integer", "description": f"id of a {hint.__name__}"}
        if models is not None:
            models.setdefault(hint.__name__, hint)
            return {"$ref": f"#/components/schemas/{hint.__name__}"}
        return model_schema(hint)
    if dataclasses.is_dataclass(hint) and isinstance(hint, type):
        try:
            hints = typing.get_type_hints(hint)
        except Exception:
            hints = {}
        properties, required = {}, []
        for field in dataclasses.fields(hint):
            properties[field.name] = json_schema(hints.get(field.name, typing.Any), output=output, models=models)
            if field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING:
                required.append(field.name)
        out = {"type": "object", "title": hint.__name__, "properties": properties}
        if required:
            out["required"] = required
        return out
    if _is_typeddict(hint):
        try:
            hints = typing.get_type_hints(hint)
        except Exception:
            hints = {}
        keys = getattr(hint, "__required_keys__", None)
        if keys is None:
            keys = frozenset(hints) if hint.__total__ else frozenset()
        out = {"type": "object", "title": hint.__name__,
               "properties": {key: json_schema(value, output=output, models=models) for key, value in hints.items()}}
        required = [key for key in hints if key in keys]
        if required:
            out["required"] = required
        return out
    return {}


def _key_schema(hint) -> dict | None:
    """``propertyNames`` for a ``dict[K, V]``: what the keys (always text in JSON) must look like."""
    if hint is int:
        return dict(_INT_KEY)
    if hint is str or hint is typing.Any or hint is object:
        return None
    schema = json_schema(hint)
    if schema.get("type") == "string" or (schema.get("enum") and all(isinstance(v, str) for v in schema["enum"])):
        return schema
    return None


def model_schema(model) -> dict:
    """The JSON Schema of a model row as ``to_dict()`` returns it.

    ``id`` plus every field (a ForeignKey appears as ``<name>_id``), with ``maxLength``,
    ``enum`` for choices and ``null`` where the column allows it. Whatever the model's own
    ``to_dict()`` removes (``User`` drops the password hash) is left out here too.

    Example::

        model_schema(Todo)
        # {"type": "object", "title": "Todo", "properties": {"id": {"type": "integer"},
        #  "title": {"type": "string", "maxLength": 200}, "done": {"type": "boolean"}}, "required": [...]}
    """
    kinds = {"text": {"type": "string"}, "int": {"type": "integer"}, "float": {"type": "number"},
             "bool": {"type": "boolean"}, "datetime": {"type": "string", "format": "date-time"},
             "date": {"type": "string", "format": "date"}, "json": {}, "fk": {"type": "integer"}}
    properties: dict[str, dict] = {"id": {"type": "integer"}}
    hidden = _fields_to_dict_drops(model)
    for field in model._meta.fields:
        if field.attname in hidden:
            continue
        schema = dict(kinds.get(field.kind, {}))
        if getattr(field, "max_length", None):
            schema["maxLength"] = field.max_length
        if field.choices:
            schema["enum"] = [c[0] if isinstance(c, (list, tuple)) else c for c in field.choices]
        if field.kind == "fk":
            target = field.to if isinstance(field.to, str) else field.to.__name__
            schema["description"] = f"id of a {target}"
        if field.null and schema:
            schema = {"anyOf": [schema, {"type": "null"}]}
        properties[field.attname] = schema
    out = {"type": "object", "title": model.__name__, "properties": properties, "required": list(properties)}
    doc = vars(model).get("__doc__")
    if doc and doc.strip():
        out["description"] = doc.strip().splitlines()[0]
    return out


def _fields_to_dict_drops(model) -> set[str]:
    """Columns a model's own ``to_dict()`` leaves out, found by calling it on a blank row."""
    from .db.models import Model

    if model.to_dict is Model.to_dict:
        return set()
    try:
        shown = set(model.__new__(model).to_dict())
    except Exception:
        try:
            shown = set(model().to_dict())
        except Exception:
            return set()
    return {f.attname for f in model._meta.fields if f.attname not in shown}


def form_schema(schema) -> dict:
    """The JSON Schema for a :class:`jongo.forms.Schema`.

    Describes the typed form of the values (an ``Int`` field is an integer). The schema
    itself is more lenient at run time: it also accepts the text a browser form sends
    (``"42"``) and converts it.

    Example::

        form_schema(forms.Schema("Invite", email=forms.Email(), seats=forms.Int(min=1)))
        # {"type": "object", "title": "Invite", "properties": {"email": {"type": "string",
        #  "format": "email", "maxLength": 254, ...}, "seats": {"type": "integer", "minimum": 1}},
        #  "required": ["email", "seats"]}
    """
    properties, required = {}, []
    for name, rule in schema.items():
        kind = rule["type"]
        if kind == "int":
            prop: dict[str, Any] = {"type": "integer"}
        elif kind == "float":
            prop = {"type": "number"}
        elif kind == "bool":
            prop = {"type": "boolean"}
        elif kind == "choice":
            prop = {"enum": list(rule["choices"])}
        else:
            prop = {"type": "string"}
            if kind == "email":
                prop["format"] = "email"
            elif rule.get("secret"):
                prop["format"] = "password"
        for key, word in (("min_length", "minLength"), ("max_length", "maxLength"),
                          ("min", "minimum"), ("max", "maximum")):
            if key in rule:
                prop[word] = rule[key]
        if "pattern" in rule and kind != "email":
            prop["pattern"] = f"^(?:{rule['pattern']})$"
        prop["title"] = rule["label"]
        if rule.get("help"):
            prop["description"] = rule["help"]
        if "default" in rule:
            prop["default"] = rule["default"]
        properties[name] = prop
        if rule["required"]:
            required.append(name)
    out = {"type": "object", "title": getattr(schema, "__name__", "Schema"), "properties": properties}
    if required:
        out["required"] = required
    return out


def function_schema(fn) -> dict:
    """The JSON Schema of a ``@server`` function's arguments, as one object keyed by name.

    ``request`` is left out (the server supplies it); a parameter without a default is
    required. This is the ``kwargs`` half of a server call's body, and the ``inputSchema``
    of the function as an MCP tool.

    Parameters:
        fn: a function decorated with ``@server``.

    Example::

        @server
        def add_todo(request, title: str, done: bool = False) -> dict: ...

        function_schema(add_todo)
        # {"type": "object", "properties": {"title": {"type": "string"},
        #  "done": {"type": "boolean", "default": False}}, "required": ["title"],
        #  "additionalProperties": False}
    """
    try:
        hints = fn.hints
    except Exception:
        hints = {}
    properties, required, open_ended = {}, [], False
    for name, parameter in fn.signature.parameters.items():
        if name == "request" and fn.wants_request:
            continue
        if parameter.kind is parameter.VAR_KEYWORD:
            open_ended = True
            continue
        if parameter.kind is parameter.VAR_POSITIONAL:
            continue
        prop = json_schema(hints.get(name, typing.Any))
        if parameter.default is not parameter.empty:
            default = parameter.default
            try:
                json.dumps(default, allow_nan=False)
            except (TypeError, ValueError):
                pass  # a default that isn't JSON (an object, a sentinel) is simply not advertised
            else:
                prop = {**prop, "default": default}
        else:
            required.append(name)
        properties[name] = prop
    out: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        out["required"] = required
    out["additionalProperties"] = bool(open_ended)
    return out


# -------------------------------------------------------------------------------- openapi --

_ERROR = {
    "type": "object",
    "description": "How Jongo reports a failed server call. `code` is stable; `jongo explain <code>` explains it.",
    "properties": {
        "error": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "description": "the exception's class name"},
                "message": {"type": "string"},
                "code": {"type": "string", "pattern": "^J[0-9]{3}$"},
                "hint": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "request_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "errors": {"type": "object", "additionalProperties": {"type": "string"},
                           "description": "per-field messages, for a validation error (J304)"},
            },
            "required": ["type", "message", "code"],
        }
    },
    "required": ["error"],
}


def _error_response(description: str) -> dict:
    return {"description": description,
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}


def _handler_hints(handler) -> dict:
    """A page or route handler's type hints, also resolving model and schema names it can't see."""
    try:
        return typing.get_type_hints(handler)
    except Exception:
        pass
    from .db.models import models_registry

    names: dict = {model.__name__: model for model in models_registry.values()}
    forms = sys.modules.get("jongo.forms")
    if forms is not None:
        names.update({schema.__name__: schema for schema in forms.SCHEMAS.values() if schema.__name__ not in names})
    try:
        return typing.get_type_hints(handler, localns=names)
    except Exception:
        return {}


def _first_line(obj) -> str | None:
    doc = inspect.getdoc(obj)
    return doc.strip().splitlines()[0] if doc and doc.strip() else None


def _rest_of_doc(obj) -> str | None:
    doc = inspect.getdoc(obj)
    if not doc:
        return None
    rest = "\n".join(doc.strip().splitlines()[1:]).strip()
    return rest or None


def _request_body(sf, hints: dict) -> tuple[dict, bool]:
    """The JSON body of a call to ``sf``, and whether a body is required at all.

    The server binds ``{"args": [...], "kwargs": {...}}`` the way Python binds a call:
    ``args`` fill the parameters in order and ``kwargs`` name the rest. The browser sends
    its arguments in ``args``; another client may name them all in ``kwargs``, or mix the
    two. So neither key is required on its own: the ``anyOf`` lists each way of covering
    the required parameters (the first ``n`` by position, the rest by name).
    """
    by_name = function_schema(sf)
    properties = by_name["properties"]
    params = [(name, p) for name, p in sf.signature.parameters.items()
              if not (name == "request" and sf.wants_request)]
    positional = [name for name, p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    positional_only = {name for name, p in params if p.kind is p.POSITIONAL_ONLY}
    star_args = next((name for name, p in params if p.kind is p.VAR_POSITIONAL), None)
    # Parameters without a default come first among the positional ones, so they are a prefix.
    leading = [name for name, p in params
               if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty]
    keyword_only = [name for name, p in params if p.kind is p.KEYWORD_ONLY and p.default is p.empty]

    args: dict[str, Any] = {"type": "array",
                            "description": "positional arguments, in order: " + (", ".join(positional) or "none")}
    if positional:
        args["prefixItems"] = [properties[name] for name in positional]
    if star_args is not None:
        items = json_schema(hints.get(star_args, typing.Any))
        if items:
            args["items"] = items
    else:
        args["maxItems"] = len(positional)
    args["default"] = []
    kwargs: dict[str, Any] = {
        "type": "object", "description": "arguments by name",
        "properties": {name: schema for name, schema in properties.items() if name not in positional_only},
        "additionalProperties": by_name["additionalProperties"], "default": {},
    }
    body: dict[str, Any] = {"type": "object", "properties": {"args": args, "kwargs": kwargs}}

    shapes = []
    first = sum(1 for name in leading if name in positional_only)  # those can only go by position
    for n in range(first, len(leading) + 1):
        named = leading[n:] + keyword_only
        shape: dict[str, Any] = {"properties": {}, "required": []}
        if n:
            shape["properties"]["args"] = {"minItems": n}
            shape["required"].append("args")
        if named:
            shape["properties"]["kwargs"] = {"required": named}
            shape["required"].append("kwargs")
        shape["title"] = ("all by name" if not n else "all by position" if not named
                          else f"the first {n} by position, the rest by name")
        shapes.append(shape)
    required = bool(leading or keyword_only)
    if required:
        body["anyOf"] = shapes
    return body, required


def openapi(app, *, version: str = "0.0.0", server_url: str | None = None) -> dict:
    """An OpenAPI 3.1 document for the app, generated from the running code.

    It covers every ``@server`` function (as ``POST /_jongo/rpc/<id>`` with the JSON body
    browser code sends, the typed result and the error envelope), every page and every
    route (with path and query parameters), and a schema for each model the app returns.
    The framework's own routes and the admin are left out.

    Parameters:
        app: the Jongo app.
        version: the API version to put in ``info.version`` (an app has none of its own).
        server_url: when given, listed under ``servers``.

    Returns:
        The document as a dict, ready for ``json.dumps``.

    Example::

        import json
        from jongo import openapi

        Path("openapi.json").write_text(json.dumps(openapi(app, version="1.2.0"), indent=2))
    """
    from . import __version__
    from .introspect import _belongs_to_app
    from .routing import _ANNOTATION_CONVERTERS, _PARAM
    from .rpc import SERVER_FUNCTIONS

    models: dict = {}
    paths: dict[str, dict] = {}
    operation_ids: set[str] = set()

    def operation_id(base: str) -> str:
        name, n = base, 1
        while name in operation_ids:
            n += 1
            name = f"{base}_{n}"
        operation_ids.add(name)
        return name

    # -- server functions -------------------------------------------------------------------
    for sf in sorted(SERVER_FUNCTIONS.values(), key=lambda f: f.id):
        if not _belongs_to_app(sf.fn):
            continue
        try:
            hints = sf.hints
        except Exception:
            hints = {}
        body, body_required = _request_body(sf, hints)
        result = {"type": "object", "properties": {"ok": json_schema(hints.get("return", typing.Any),
                                                                     output=True, models=models)}}
        responses = {
            "200": {"description": "The function's return value under `ok` (or `redirect` when it "
                                   "returned redirect(url)).",
                    "content": {"application/json": {"schema": {"anyOf": [
                        result,
                        {"type": "object", "properties": {"redirect": {"type": "string"}}, "required": ["redirect"]},
                    ]}}}},
            "400": _error_response("The arguments failed validation (J402, J403, J404) or the function raised "
                                   "ValidationError (J304, with per-field `errors`)."),
            "403": _error_response("The CSRF check failed (J207), or the caller isn't allowed (J209, J210)."),
            "500": _error_response("The function raised (J407). In production only the code and request id are sent."),
        }
        if sf.login_required:
            responses["401"] = _error_response("Not signed in (J208).")
        if any(_is_model(h) for h in hints.values()):
            responses["404"] = _error_response("A model argument names a row that doesn't exist (J411).")
        if sf.rate_limit is not None:
            responses["429"] = _error_response(f"Over the rate limit of {sf.rate_limit} (J213); see Retry-After.")
        operation = {
            "operationId": operation_id(sf.fn.__name__),
            "tags": ["server functions"],
            "summary": _first_line(sf.fn) or sf.fn.__name__,
            "requestBody": {"required": body_required,
                            "content": {"application/json": {"schema": body}}},
            "responses": dict(sorted(responses.items())),
            "security": [{"csrf": [], "session": []}] if sf.login_required else [{"csrf": []}],
            "x-jongo": {"id": sf.id, "login_required": bool(sf.login_required),
                        "admin_required": bool(sf.admin_required), "refresh": bool(sf.refresh),
                        "rate_limit": str(sf.rate_limit) if sf.rate_limit is not None else None},
        }
        rest = _rest_of_doc(sf.fn)
        if rest:
            operation["description"] = rest
        paths[f"/_jongo/rpc/{sf.id}"] = {"post": operation}

    # -- pages and routes ---------------------------------------------------------------------
    for route in app.router.routes:
        if route.kind == "internal" or not _belongs_to_app(route.handler):
            continue
        hints = _handler_hints(route.handler)
        parameters, in_path = [], set()
        for match in _PARAM.finditer(route.path):
            converter = match.group(1) or _ANNOTATION_CONVERTERS.get(hints.get(match.group(2)), "str")
            in_path.add(match.group(2))
            parameters.append({"name": match.group(2), "in": "path", "required": True,
                               "schema": dict(_CONVERTER_SCHEMAS.get(converter, {"type": "string"}))})
        for name, parameter in inspect.signature(route.handler).parameters.items():
            if name == "request" or name in in_path or parameter.kind in (parameter.VAR_POSITIONAL,
                                                                          parameter.VAR_KEYWORD):
                continue
            schema = json_schema(hints.get(name, str))
            if parameter.default is not parameter.empty and isinstance(parameter.default, (bool, int, float, str)):
                schema = {**schema, "default": parameter.default}
            parameters.append({"name": name, "in": "query", "required": False, "schema": schema})
        path = _PARAM.sub(lambda m: "{" + m.group(2) + "}", route.path)
        options = route.options
        for method in sorted(route.methods - {"HEAD"}):
            if route.kind == "page":
                content = {"text/html": {"schema": {"type": "string"}}}
                description = "The rendered page. Sent with `X-Jongo-Nav: 1` it is the page's tree as JSON."
            elif "return" in hints:
                content = {"application/json": {"schema": json_schema(hints["return"], output=True, models=models)}}
                description = "The handler's return value."
            else:
                content = None
                description = "Whatever the handler returns: a Response, HTML, or JSON-able data."
            responses = {"200": {"description": description, **({"content": content} if content else {})}}
            if options.get("login_required"):
                responses["302" if route.kind == "page" else "401"] = {
                    "description": f"Not signed in: redirected to {app.login_url}." if route.kind == "page"
                    else "Not signed in (J208)."}
            if options.get("rate_limit") is not None:
                responses["429"] = {"description": f"Over the rate limit of {options['rate_limit']} (J213)."}
            security = []
            if method not in ("GET", "OPTIONS") and options.get("csrf", True):
                security.append({"csrf": [], **({"session": []} if options.get("login_required") else {})})
            elif options.get("login_required"):
                security.append({"session": []})
            operation = {
                "operationId": operation_id(route.name if len(route.methods - {"HEAD"}) == 1
                                            else f"{route.name}_{method.lower()}"),
                "tags": ["pages" if route.kind == "page" else "routes"],
                "summary": _first_line(route.handler) or options.get("title") or route.name,
                "responses": dict(sorted(responses.items())),
                "x-jongo": {"kind": route.kind, "name": route.name,
                            "login_required": bool(options.get("login_required")),
                            "admin_required": bool(options.get("admin_required")),
                            "rate_limit": str(options["rate_limit"]) if options.get("rate_limit") is not None
                            else None},
            }
            if parameters:
                operation["parameters"] = parameters
            if security:
                operation["security"] = security
            paths.setdefault(path, {})[method.lower()] = operation

    # -- components -----------------------------------------------------------------------------
    schemas: dict[str, dict] = {"Error": _ERROR}
    done: set[str] = set()
    while set(models) - done:  # a model's schema never references another, but stay general
        for name in sorted(set(models) - done):
            schemas[name] = model_schema(models[name])
            done.add(name)

    document: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "title": app.title,
            "version": str(version),
            "description": f"Generated by `jongo openapi` from the running code (jongo {__version__}). "
                           "Server functions are called with POST and a JSON body {\"args\": [...], "
                           "\"kwargs\": {...}}; arguments are validated against the function's type hints.",
        },
        "paths": dict(sorted(paths.items())),
        "components": {
            "schemas": dict(sorted(schemas.items())),
            "securitySchemes": {
                "csrf": {"type": "apiKey", "in": "header", "name": "X-CSRF-Token",
                         "description": "The value of the `jongo_csrf` cookie (GET any page to receive it), "
                                        "sent back as this header together with the cookie."},
                "session": {"type": "apiKey", "in": "cookie", "name": "jongo_session",
                            "description": "The signed session cookie set at sign-in."},
            },
        },
        "tags": [{"name": "server functions", "description": "Python functions the browser calls with await."},
                 {"name": "pages"}, {"name": "routes"}],
    }
    if server_url:
        document["servers"] = [{"url": server_url}]
    return document
