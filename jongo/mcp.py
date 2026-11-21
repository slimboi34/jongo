"""An MCP server for the app: let a coding agent inspect it and call it, with no glue code.

The Model Context Protocol is how agents such as Claude Code discover and call tools. A
Jongo app already knows everything such a tool list needs: its ``@server`` functions have
names, docstrings and type hints, and ``jongo context/check/doctor`` already answer in
JSON. This module serves that over MCP, two ways:

**From the project, over stdio** (what a coding agent on your machine uses)::

    jongo mcp                       # speaks MCP on stdin/stdout for the app in this directory
    claude mcp add myapp -- jongo mcp

**From the running server, over HTTP** (opt in, in ``app.py``)::

    app.mcp()                                   # POST /_jongo/mcp: the diagnostic tools
    app.mcp(tools=[add_todo, list_todos])       # ...plus these server functions
    app.mcp(tools="all", act_as=lambda request: User.get(username="agent"))

Tools an agent gets:

* ``jongo_context``, ``jongo_describe``, ``jongo_check``, ``jongo_doctor``, ``jongo_explain``,
  ``jongo_status``, ``jongo_openapi``: the same reports as the CLI commands, read from the
  live registries.
* ``jongo_get``: render a page or call a GET route in-process and return the result.
* One tool per exposed ``@server`` function, named after it. Its input schema is generated
  from the type hints (:func:`jongo.schema.function_schema`), and a call goes through the
  same checks as a call from the browser: arguments validated against the hints,
  ``login_required``/``admin_required`` enforced, rate limits counted.

A failed call comes back as a tool error carrying the Jongo error code and hint, so the
agent can run ``jongo_explain`` on it.

**Who may connect over HTTP.** The endpoint doesn't exist until ``app.mcp()`` is called.
Under ``jongo dev`` it accepts connections from this machine only, addressed as
``localhost`` or ``127.0.0.1``. In production it requires
``Authorization: Bearer $JONGO_MCP_TOKEN`` (at least 16 characters); without that variable
set it refuses everyone. A request carrying a browser ``Origin`` from another site, or (in
dev) a ``Host`` that isn't this machine, is refused (``J420``): together those stop a web
page from reaching a local server, directly or by DNS rebinding.

The transport is MCP's Streamable HTTP in its simplest form: each POST carries one
JSON-RPC message and gets one JSON response; the server pushes nothing, so there is no
session and no stream to keep open.
"""
from __future__ import annotations

import hmac
import io
import json
import os
import sys
import traceback
import urllib.parse
from typing import Any

from .errors import HTTPError, JongoError, app_location, classify, hint_for
from .http import _LOCAL_NAMES, IN_PROCESS_HOST, _hostname

__all__ = ["McpServer", "serve_stdio", "PROTOCOL_VERSIONS"]

#: Protocol revisions this server answers to, newest first. A client asking for one of
#: them gets it; any other request is answered with the newest.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

#: The longest page body ``jongo_get`` returns, in characters.
MAX_BODY = 20_000

_PARSE_ERROR, _INVALID_REQUEST, _METHOD_NOT_FOUND, _INVALID_PARAMS, _INTERNAL = -32700, -32600, -32601, -32602, -32603

_NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}


_LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


class _RpcError(Exception):
    """A JSON-RPC protocol error (as opposed to a tool that ran and failed)."""

    def __init__(self, number: int, message: str, code: str | None = None):
        super().__init__(message)
        self.number = number
        self.message = message
        self.code = code


def _local_environ(method: str, path: str) -> dict:
    path, _, query = path.partition("?")
    return {
        "REQUEST_METHOD": method, "PATH_INFO": path.encode("utf-8").decode("latin-1"), "QUERY_STRING": query,
        "SERVER_NAME": "mcp.local", "SERVER_PORT": "80", "SERVER_PROTOCOL": "HTTP/1.1", "HTTP_HOST": "mcp.local",
        "REMOTE_ADDR": "127.0.0.1", "wsgi.input": io.BytesIO(b""), "wsgi.url_scheme": "http",
        "wsgi.errors": io.StringIO(), "CONTENT_LENGTH": "0", "CONTENT_TYPE": "",
        IN_PROCESS_HOST: "mcp.local",  # made up in-process: the app's Host check lets it through
    }


class McpServer:
    """The MCP face of one app: its tool list, and the handler for each JSON-RPC message.

    You rarely build one yourself: ``jongo mcp`` and ``app.mcp()`` do. It is public so a
    test (or another transport) can drive it directly.

    Parameters:
        app: the Jongo app.
        tools: which ``@server`` functions to expose as tools: ``"all"`` for every one the
            app defines, a list of the functions themselves, or ``None`` for none (the
            ``jongo_*`` tools are always there).
        act_as: who a tool call runs as. A callable ``act_as(request) -> User | None``,
            or a user, or ``None`` for anonymous. Functions marked ``login_required`` fail
            with ``J208`` for an anonymous caller.
        detail: include exception messages, file and line in tool errors when the app's own
            code raises. Defaults to dev mode (and never when ``JONGO_ENV=production``);
            ``jongo mcp`` turns it on, since its caller is the developer.

    Example::

        server = McpServer(app, tools="all")
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        server.call("add_todo", {"title": "Write docs"})    # -> {"content": [...], "isError": False, ...}
    """

    def __init__(self, app, *, tools="all", act_as=None, detail: bool | None = None):
        self.app = app
        self.act_as = act_as
        self.detail = detail
        self._selection = tools
        if tools is not None and tools != "all":
            from .rpc import ServerFunction

            bad = [t for t in tools if not isinstance(t, ServerFunction)]
            if bad:
                raise JongoError(
                    f"app.mcp(tools=...) takes @server functions; got {', '.join(type(b).__name__ for b in bad)}",
                    code="J421", hint='pass the decorated functions themselves, or tools="all"',
                )
            self._selection = list(tools)

    # -- the tool list ---------------------------------------------------------------------

    def _functions(self) -> dict:
        """Exposed server functions by tool name (looked up live, so a reload is reflected)."""
        from .introspect import _belongs_to_app
        from .rpc import SERVER_FUNCTIONS

        if self._selection is None:
            chosen = []
        elif self._selection == "all":
            chosen = [f for f in SERVER_FUNCTIONS.values() if _belongs_to_app(f.fn)]
        else:
            chosen = list(self._selection)
        counts: dict[str, int] = {}
        for fn in chosen:
            counts[fn.fn.__name__] = counts.get(fn.fn.__name__, 0) + 1
        out = {}
        for fn in sorted(chosen, key=lambda f: f.id):
            name = fn.fn.__name__
            if counts[name] > 1 or name.startswith("jongo_"):  # two modules, one name: qualify both
                name = fn.id.replace(".", "_")
            out[name[:64]] = fn
        return out

    def tools(self) -> list[dict]:
        """Every tool as MCP describes one: ``name``, ``description``, ``inputSchema``, ``annotations``.

        Example::

            [t["name"] for t in McpServer(app).tools()]
            # ['jongo_context', 'jongo_describe', ..., 'add_todo', 'toggle_todo']
        """
        from .schema import function_schema

        out = [dict(tool) for tool in _BUILTIN_TOOLS]
        for name, fn in self._functions().items():
            doc = (fn.fn.__doc__ or "").strip()
            notes = []
            if fn.admin_required:
                notes.append("Admins only.")
            elif fn.login_required:
                notes.append("Requires a signed-in user.")
            if fn.rate_limit is not None:
                notes.append(f"Rate limit: {fn.rate_limit}.")
            description = " ".join(part for part in (doc or f"The app's server function {fn.id}.", *notes) if part)
            out.append({
                "name": name,
                "title": fn.fn.__name__.replace("_", " ").capitalize(),
                "description": description,
                "inputSchema": function_schema(fn),
                "annotations": {"readOnlyHint": False, "openWorldHint": False},
                "_meta": {"jongo/function": fn.id},
            })
        return out

    # -- calling ---------------------------------------------------------------------------

    def _request(self, request=None, method="POST", path="/_jongo/mcp"):
        """The request a tool runs with: the HTTP one, or a local stand-in over stdio."""
        from .http import Request

        if request is None:
            request = Request(_local_environ(method, path), self.app)
            self.app._load_session(request)
        actor = self.act_as
        if callable(actor):
            actor = actor(request)
        # Identity comes from act_as alone, never from a cookie: this endpoint is exempt from
        # the CSRF check, so a browser session must not carry any authority here.
        request.user = actor
        return request

    def call(self, name: str, arguments: dict | None = None, request=None) -> dict:
        """Run one tool and return an MCP tool result.

        Parameters:
            name: a tool name from :meth:`tools`.
            arguments: the tool's arguments by name.
            request: the HTTP request carrying the call, if there is one.

        Returns:
            ``{"content": [{"type": "text", "text": ...}], "structuredContent": {...},
            "isError": bool}``. A tool that fails returns ``isError: True`` with
            ``structuredContent["error"]`` holding ``code``, ``message`` and ``hint``; it
            does not raise.

        Raises:
            JongoError: code ``J421`` when no tool has that name.

        Example::

            result = server.call("jongo_explain", {"code": "J302"})
            result["structuredContent"]["title"]     # "Database schema is behind the models"
        """
        arguments = {} if arguments is None else arguments
        if not isinstance(arguments, dict):
            raise JongoError(f"the arguments of {name!r} must be an object", code="J422")
        builtin = _BUILTINS.get(name)
        function = None if builtin is not None else self._functions().get(name)
        if builtin is None and function is None:
            raise JongoError(f"no tool named {name!r}", code="J421",
                             hint="tools/list shows the tools; app functions appear only if the server "
                                  "exposes them (app.mcp(tools=...))")
        # Details only where the dev error page would show them: dev mode, and not a server
        # that JONGO_ENV declares to be production.
        detail = self.app._status_open() if self.detail is None else self.detail
        try:
            if builtin is not None:
                value = builtin(self, arguments, request)
            else:
                value = self._invoke(function, arguments, request)
        except Exception as exc:
            return self._failure(exc, detail)
        return _result(value)

    def _invoke(self, function, arguments: dict, request):
        from .http import Response
        from .vdom import to_json_data

        result = function.invoke(self._request(request), [], arguments)
        if isinstance(result, Response):
            location = result.headers.get("Location")
            if location:
                return {"redirect": location}
            raise HTTPError(500, "server functions can return data or redirect(), not other responses",
                            code="J408")
        return to_json_data(result, f"{function.fn.__name__}() result")

    def _failure(self, exc: Exception, detail: bool) -> dict:
        code, hint = classify(exc)
        expected = isinstance(exc, HTTPError) or type(exc).__name__ == "ValidationError"
        if code == "J000":
            code, hint = "J407", hint_for("J407")
        error: dict[str, Any] = {"code": code, "type": type(exc).__name__}
        if expected or detail:
            error["message"] = getattr(exc, "message", None) if isinstance(getattr(exc, "message", None), str) \
                else str(exc)
            error["hint"] = hint
        else:  # production, and the app's own code raised: say no more than a browser would be told
            error["message"] = "Server error"
        errors = getattr(exc, "errors", None)
        if isinstance(errors, dict):
            error["errors"] = {str(k): str(v) for k, v in errors.items()}
        if isinstance(exc, HTTPError):
            error["status"] = exc.status
        if getattr(exc, "retry_after", None):
            error["retry_after"] = exc.retry_after
        if detail and not expected:
            file, line = app_location(exc, self.app.root)
            if file:
                error["file"], error["line"] = file, line
            error["traceback"] = "".join(traceback.format_exception(exc))[-4000:]
        error["explain"] = f"jongo explain {code}"
        if not expected:
            try:
                self.app.monitor.record_error(exc, rid=None, method="MCP", path="/_jongo/mcp", root=self.app.root,
                                              code=code, hint=hint)
            except Exception:
                pass
        text = f"[{code}] {error['message']}" + (f"\nhint: {error['hint']}" if error.get("hint") else "")
        return {"content": [{"type": "text", "text": text}], "structuredContent": {"error": error}, "isError": True}

    # -- JSON-RPC --------------------------------------------------------------------------

    def handle(self, message, request=None) -> dict | None:
        """Answer one JSON-RPC message; returns the response, or None for a notification.

        Supports ``initialize``, ``ping``, ``tools/list``, ``tools/call``,
        ``resources/list`` and ``resources/read``. Anything else is "method not found".

        Example::

            server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "test", "version": "0"}}})
        """
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(
                message.get("method"), str):
            if isinstance(message, dict) and "method" not in message and ("result" in message or "error" in message):
                return None  # a response to something we never asked: ignore it
            return _error(message.get("id") if isinstance(message, dict) else None, _INVALID_REQUEST,
                          "not a JSON-RPC 2.0 request", "J422")
        is_notification = "id" not in message
        method, params = message["method"], message.get("params") or {}
        try:
            if not isinstance(params, dict):
                raise _RpcError(_INVALID_PARAMS, "params must be an object", "J422")
            result = self._dispatch(method, params, request)
        except _RpcError as exc:
            return None if is_notification else _error(message.get("id"), exc.number, exc.message, exc.code)
        except JongoError as exc:
            number = _INVALID_PARAMS if exc.code in ("J421", "J422") else _INTERNAL
            return None if is_notification else _error(message.get("id"), number, str(exc), exc.code)
        except Exception as exc:  # a bug here must not take the connection down
            return None if is_notification else _error(message.get("id"), _INTERNAL, f"{type(exc).__name__}: {exc}")
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": message["id"], "result": result}

    def _dispatch(self, method: str, params: dict, request):
        from . import __version__

        if method == "initialize":
            wanted = params.get("protocolVersion")
            return {
                "protocolVersion": wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False,
                                                                               "subscribe": False}},
                "serverInfo": {"name": f"jongo-{self.app.name}", "title": self.app.title, "version": __version__},
                "instructions": _INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method.startswith("notifications/"):
            return {}
        if method == "tools/list":
            return {"tools": self.tools()}
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                raise _RpcError(_INVALID_PARAMS, "tools/call needs the tool's name", "J422")
            return self.call(name, params.get("arguments"), request)
        if method == "resources/list":
            return {"resources": [dict(r) for r in _RESOURCES]}
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "prompts/list":
            return {"prompts": []}
        if method == "resources/read":
            uri = params.get("uri")
            for resource in _RESOURCES:
                if resource["uri"] == uri:
                    text = _read_resource(self, uri)
                    return {"contents": [{"uri": uri, "mimeType": resource["mimeType"], "text": text}]}
            raise _RpcError(-32002, f"no resource {uri!r}", "J421")
        raise _RpcError(_METHOD_NOT_FOUND, f"method {method!r} isn't supported")

    # -- HTTP ------------------------------------------------------------------------------

    def allowed(self, request) -> bool:
        """Whether an HTTP request may use the endpoint (see the module docstring)."""
        token = (os.environ.get("JONGO_MCP_TOKEN") or "").strip()
        supplied = request.headers.get("authorization") or ""
        if len(token) >= 16 and hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
            return True
        origin = request.headers.get("origin")
        if origin is not None and (origin == "null" or urllib.parse.urlsplit(origin).netloc != request.host):
            return False  # a web page on another site, reaching for a local server
        # Dev mode, no token: this machine only. Both ends are checked: the peer address, and
        # the Host the client asked for, because a page that rebinds its own domain to
        # 127.0.0.1 connects from loopback but still names its domain in Host.
        return (self.app._status_open() and request.remote_addr in _LOOPBACK
                and _hostname(request.host) in _LOCAL_NAMES)

    def http(self, request):
        """The route handler behind ``app.mcp()``: one JSON-RPC message in, one response out."""
        from .http import Response, json_response

        headers = {"Cache-Control": "no-store"}
        if not self.allowed(request):
            raise HTTPError(
                403, "The MCP endpoint needs the bearer token in production, and only answers this machine "
                     "under `jongo dev`.", code="J420")
        if request.method != "POST":
            return Response(b"", 405, {"Allow": "POST", **headers}, content_type=None)
        try:
            message = json.loads(request.body or b"null")
        except (ValueError, RecursionError):  # RecursionError: nested deeper than json's C decoder goes
            return json_response(_error(None, _PARSE_ERROR, "the body isn't valid JSON (or is nested too deeply)",
                                        "J422"), 400, headers)
        if isinstance(message, list):
            return json_response(_error(None, _INVALID_REQUEST, "send one JSON-RPC message per request "
                                                                 "(batches aren't supported)", "J422"), 400, headers)
        response = self.handle(message, request)
        if response is None:
            return Response(b"", 202, headers, content_type=None)
        return json_response(response, 200, headers)


# ------------------------------------------------------------------------- built-in tools --

_INSTRUCTIONS = (
    "This is a Jongo app: full-stack Python, where pages, @server functions, @component UI and db.Model "
    "tables live together. Read jongo_context first (the framework's rules and this app's map). After "
    "changing code, call jongo_check; if something fails, the error carries a code: call jongo_explain "
    "with it. jongo_get renders a page in-process. Any other tool is one of the app's own @server "
    "functions; its arguments are validated against its type hints exactly as a call from the browser is."
)


class _Text(str):
    """A result that is a document to read (the context pack), not data: no structured copy."""


def _result(value) -> dict:
    if isinstance(value, _Text):
        return {"content": [{"type": "text", "text": str(value)}], "isError": False}
    structured = value if isinstance(value, dict) else {"result": value}
    return {"content": [{"type": "text", "text": json.dumps(value, indent=2, default=str)}],
            "structuredContent": structured, "isError": False}


def _error(id_, number: int, message: str, code: str | None = None) -> dict:
    error: dict[str, Any] = {"code": number, "message": message}
    if code:
        error["data"] = {"code": code, "hint": hint_for(code), "explain": f"jongo explain {code}"}
    return {"jsonrpc": "2.0", "id": id_, "error": error}


def _t_context(server, args, request):
    from .introspect import context

    return _Text(context(server.app))


def _t_describe(server, args, request):
    from .introspect import describe

    described = describe(server.app)
    section = args.get("section")
    if section is None:
        return described
    if section not in described:
        raise HTTPError(400, f"no section {section!r}; choose one of {', '.join(described)}", code="J422")
    return {section: described[section]}


def _t_check(server, args, request):
    from .introspect import check

    return check(server.app)


def _t_doctor(server, args, request):
    from .diagnostics import doctor

    return doctor(server.app, production=True if args.get("production") else None)


def _t_explain(server, args, request):
    from .errors import explain

    entry = explain(str(args.get("code", "")))
    if entry is None:
        raise HTTPError(404, f"unknown error code {args.get('code')!r}", code="J422",
                        hint="codes look like J302; jongo_explain with no code lists them all")
    return entry


def _t_explain_or_list(server, args, request):
    from .errors import CATALOGUE, explain

    if not args.get("code"):
        return {"codes": [{"code": code, "title": explain(code)["title"]} for code in sorted(CATALOGUE)]}
    return _t_explain(server, args, request)


def _t_status(server, args, request):
    from .monitor import status

    return status(server.app)


def _t_openapi(server, args, request):
    from .schema import openapi

    return openapi(server.app)


def _t_get(server, args, request):
    from .http import Request

    path = args.get("path")
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise HTTPError(400, "path must be a path on this app, starting with /", code="J422")
    inner = Request(_local_environ("GET", path), server.app)
    inner.user = server._request(request).user
    response = server.app.handle(inner)
    streaming = _is_stream(response)
    if streaming:  # never read (an event stream doesn't end): close it so nothing it holds lingers
        close = getattr(response.body, "close", None)
        if close is not None:
            close()
    body = b"" if streaming else b"".join(c if isinstance(c, bytes) else c.encode() for c in response.iter_body())
    text = body.decode("utf-8", "replace")
    out = {"status": response.status, "content_type": response.headers.get("Content-Type"),
           "length": len(text), "truncated": len(text) > MAX_BODY, "body": text[:MAX_BODY]}
    if streaming:
        out["streaming"] = True
    if response.headers.get("Location"):
        out["location"] = response.headers["Location"]
    code = getattr(inner, "error_code", None)
    if code:
        out["code"] = code
    return out


def _is_stream(response) -> bool:
    return not isinstance(response.body, (str, bytes, bytearray))


_BUILTINS = {
    "jongo_context": _t_context,
    "jongo_describe": _t_describe,
    "jongo_check": _t_check,
    "jongo_doctor": _t_doctor,
    "jongo_explain": _t_explain_or_list,
    "jongo_status": _t_status,
    "jongo_openapi": _t_openapi,
    "jongo_get": _t_get,
}

_READ_ONLY = {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}

_BUILTIN_TOOLS = [
    {"name": "jongo_context", "title": "App context pack",
     "description": "The framework's rules plus this app's live map (models, server functions with "
                    "signatures, pages, components, channels) as Markdown. Read this before changing the app.",
     "inputSchema": _NO_ARGS, "annotations": _READ_ONLY},
    {"name": "jongo_describe", "title": "App map as JSON",
     "description": "The app as data: models with fields, server functions with parameters and input schemas, "
                    "pages, routes, components, channels and form schemas. Pass `section` for one part.",
     "inputSchema": {"type": "object", "additionalProperties": False, "properties": {"section": {
         "type": "string", "enum": ["app", "models", "server_functions", "pages", "routes", "components",
                                    "channels", "schemas"]}}},
     "annotations": _READ_ONLY},
    {"name": "jongo_check", "title": "Compile and check",
     "description": "Compile every component, resolve every server function's type hints, check channel guards "
                    "and plan migrations. Returns {ok, problems: [{kind, code, message, file, line, hint}], "
                    "counts, pending_migrations}. Call it after every change.",
     "inputSchema": _NO_ARGS, "annotations": _READ_ONLY},
    {"name": "jongo_doctor", "title": "Health report",
     "description": "One health report: Python, database reachable, pending migrations, compile errors, "
                    "production settings. Pass production=true to judge the settings as a deploy.",
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {"production": {"type": "boolean", "default": False}}},
     "annotations": _READ_ONLY},
    {"name": "jongo_explain", "title": "Explain an error code",
     "description": "What a Jongo error code (e.g. J302) means and how to fix it. With no code, lists every code.",
     "inputSchema": {"type": "object", "additionalProperties": False,
                     "properties": {"code": {"type": "string", "description": "e.g. J302"}}},
     "annotations": _READ_ONLY},
    {"name": "jongo_status", "title": "Live status",
     "description": "What this server process has been doing: recent requests, errors with code and traceback, "
                    "per-route counters, likely N+1 queries, channel subscribers.",
     "inputSchema": _NO_ARGS, "annotations": {**_READ_ONLY, "idempotentHint": False}},
    {"name": "jongo_openapi", "title": "OpenAPI document",
     "description": "The OpenAPI 3.1 document generated from the app's type hints and models.",
     "inputSchema": _NO_ARGS, "annotations": _READ_ONLY},
    {"name": "jongo_get", "title": "Render a page",
     "description": "GET a path on this app in-process (a page or a GET route) and return {status, "
                    "content_type, body}. Use it to confirm a page renders after a change. The body is "
                    f"truncated to {MAX_BODY} characters.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["path"],
                     "properties": {"path": {"type": "string", "description": "e.g. /todos/3?tab=done"}}},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
]

_RESOURCES = [
    {"uri": "jongo://context", "name": "context", "title": "App context pack", "mimeType": "text/markdown",
     "description": "The framework's rules and this app's map, as `jongo context` prints them."},
    {"uri": "jongo://openapi", "name": "openapi", "title": "OpenAPI document", "mimeType": "application/json",
     "description": "The OpenAPI 3.1 document generated from the running code."},
]


def _read_resource(server, uri: str) -> str:
    if uri == "jongo://context":
        return str(_t_context(server, {}, None))
    return json.dumps(_t_openapi(server, {}, None), indent=2)


# ---------------------------------------------------------------------------------- stdio --


def _stdio_reply(server: McpServer, line: str) -> dict | None:
    """The reply to one line of stdio input. Nothing a client sends may end the read loop."""
    try:
        message = json.loads(line)
    except (ValueError, RecursionError):  # RecursionError: nested deeper than json's C decoder goes
        return _error(None, _PARSE_ERROR, "the line isn't valid JSON (or is nested too deeply)", "J422")
    if isinstance(message, list):
        return _error(None, _INVALID_REQUEST, "send one JSON-RPC message per line (batches aren't supported)",
                      "J422")
    try:
        return server.handle(message)
    except Exception as exc:  # handle() answers errors itself; this is the last line of defence
        id_ = message.get("id") if isinstance(message, dict) else None
        return _error(id_, _INTERNAL, f"{type(exc).__name__} while handling the message")


def serve_stdio(server: McpServer, stdin=None, stdout=None) -> None:
    """Speak MCP over stdin/stdout until the input closes: one JSON message per line.

    This is what ``jongo mcp`` runs. Anything the app prints goes to stderr, because
    stdout carries only protocol messages.

    Parameters:
        server: the :class:`McpServer` to serve.
        stdin, stdout: text streams (default: the process's own).

    Example::

        serve_stdio(McpServer(app, tools="all", detail=True))
    """
    stdin = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    redirect = stdout is None
    if redirect:
        sys.stdout = sys.stderr  # a stray print() in app code must not corrupt the protocol
    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            response = _stdio_reply(server, line)
            if response is not None:
                try:
                    text = json.dumps(response, separators=(",", ":"), default=str)
                except Exception as exc:  # e.g. a result (or an echoed id) nested too deeply to encode
                    text = json.dumps(_error(None, _INTERNAL, f"the reply couldn't be encoded: {type(exc).__name__}"))
                out.write(text + "\n")
                out.flush()
    finally:
        if redirect:
            sys.stdout = out
