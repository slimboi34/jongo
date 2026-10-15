"""URL patterns like ``/posts/<int:id>`` or ``/posts/<id>`` with ``id: int`` in the signature."""
from __future__ import annotations

import inspect
import re
import typing
import urllib.parse
import uuid

from .errors import HTTPError, NotFound, RouteError, URLBuildError

CONVERTERS = {
    "str": (r"[^/]+", str),
    "int": (r"-?\d+", int),
    "float": (r"-?\d+(?:\.\d+)?", float),
    "slug": (r"[-a-zA-Z0-9_]+", str),
    "path": (r".+", str),
    "uuid": (r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", uuid.UUID),
}
_ANNOTATION_CONVERTERS = {int: "int", float: "float", uuid.UUID: "uuid"}
_PARAM = re.compile(r"<(?:(\w+):)?(\w+)>")


def _where(handler) -> dict:
    """``file``/``line`` of a handler's definition, for errors about its route."""
    code = getattr(getattr(handler, "__wrapped__", handler), "__code__", None)
    return {"file": code.co_filename, "line": code.co_firstlineno} if code else {}


class Route:
    def __init__(self, path: str, handler, *, methods=("GET",), kind="route", name=None, options=None):
        if not path.startswith("/"):
            raise RouteError(f"route paths must start with '/': {path!r}", code="J202",
                             hint=f"write it as {'/' + path!r}", **_where(handler))
        self.path = path
        self.handler = handler
        self.methods = {m.upper() for m in methods}
        if "GET" in self.methods:
            self.methods.add("HEAD")
        self.kind = kind
        self.name = name or getattr(handler, "__name__", path)
        self.options = options or {}
        self.params: dict[str, typing.Callable] = {}

        try:
            hints = typing.get_type_hints(handler)
        except Exception:
            hints = {}
        pattern, last = ["^"], 0
        for match in _PARAM.finditer(path):
            converter, param = match.group(1), match.group(2)
            if converter is None:
                converter = _ANNOTATION_CONVERTERS.get(hints.get(param), "str")
            if converter not in CONVERTERS:
                raise RouteError(
                    f"unknown converter {converter!r} in {path!r}", code="J201",
                    hint=f"use one of {', '.join(CONVERTERS)} (e.g. <int:{param}>), or annotate the parameter",
                    **_where(handler),
                )
            regex, convert = CONVERTERS[converter]
            pattern.append(re.escape(path[last : match.start()]))
            pattern.append(f"(?P<{param}>{regex})")
            self.params[param] = convert
            last = match.end()
        tail = path[last:]
        if tail.endswith("/") and len(path) > 1:
            tail = tail[:-1]
        pattern.append(re.escape(tail))
        pattern.append("/?$" if path != "/" else "$")
        self.regex = re.compile("".join(pattern))
        self.accepts = set(inspect.signature(handler).parameters)
        # Specificity for match ordering: catch-all (path:) last, then fewer params,
        # then more static characters — so a literal /o/special beats a dynamic /o/<thing>.
        self._npathconv = sum(1 for m in _PARAM.finditer(path) if m.group(1) == "path")
        self._nparams = len(self.params)
        self._nstatic = len(re.sub(r"<[^>]+>", "", path))

    @property
    def specificity(self):
        return (self._npathconv, self._nparams, -self._nstatic)

    def match(self, path: str) -> dict | None:
        m = self.regex.match(path)
        if not m:
            return None
        try:
            return {name: self.params[name](value) for name, value in m.groupdict().items()}
        except ValueError:
            return None

    def url(self, **params) -> str:
        def replace(match):
            name = match.group(2)
            if name not in params:
                raise URLBuildError(f"missing URL parameter {name!r} for route {self.name!r}", code="J204",
                                    hint=f"pass {name}=... to url_for({self.name!r}, ...)")
            return urllib.parse.quote(str(params.pop(name)), safe="" if match.group(1) != "path" else "/")

        url = _PARAM.sub(replace, self.path)
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return url

    def __repr__(self):
        return f"<Route {'|'.join(sorted(self.methods))} {self.path} -> {self.name}>"


class Router:
    def __init__(self):
        self.routes: list[Route] = []
        self._match_order: list[Route] = []

    def add(self, route: Route) -> Route:
        self.routes.append(route)
        # Try more-specific routes first; registration order (self.routes) is kept for url_for.
        self._match_order = sorted(self.routes, key=lambda r: r.specificity)
        return route

    def match(self, method: str, path: str) -> tuple[Route, dict]:
        allowed = set()
        for route in self._match_order:
            params = route.match(path)
            if params is None:
                continue
            if method in route.methods:
                return route, params
            allowed |= route.methods
        if allowed:
            error = HTTPError(405, f"{method} isn't allowed here", code="J206")
            error.allowed = sorted(allowed)
            raise error
        raise NotFound(f"No page at {path}")

    def url_for(self, name: str, **params) -> str:
        for route in self.routes:
            if route.name == name:
                return route.url(**params)
        raise URLBuildError(f"no route named {name!r}", code="J203",
                            hint="`jongo routes` lists the route names; a route is named after its function")
