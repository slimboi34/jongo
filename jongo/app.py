"""The Jongo application: routing, pages, server functions, sessions and the WSGI entry point."""
from __future__ import annotations

import collections.abc
import hashlib
import hmac
import html as _html
import inspect
import json
import logging
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import traceback
import types
import typing
import urllib.parse
from pathlib import Path

from . import live as live_module
from . import vdom
from . import log as jlog
from .errors import CompileError, HTTPError, NotFound, app_location, classify, hint_for
from .http import (
    CSRF_COOKIE,
    IN_PROCESS_HOST,
    SESSION_COOKIE,
    Request,
    Response,
    Session,
    Signer,
    _hostname,
    host_allowed,
    json_response,
    redirect,
)
from .monitor import Monitor
from .ratelimit import check as _check_rate
from .ratelimit import parse as _parse_rate
from .routing import Route, Router
from .rpc import SERVER_FUNCTIONS, _label, coerce
from .styles import collect_css
from .vdom import VNode, render_to_string, serialize

log = logging.getLogger("jongo")
SAFE_METHODS = frozenset(("GET", "HEAD", "OPTIONS"))


class Page:
    """Return from a page function to set the title, status or extra <head> tags."""

    def __init__(self, content, *, title: str | None = None, status: int = 200, head=()):
        self.content = content
        self.title = title
        self.status = status
        self.head = list(head)


#: Hints for errors on /_jongo/rpc/ calls, where the catalogue's generic hint (about HTML
#: forms or registering routes) would point the wrong way.
_RPC_HINTS = {
    "J207": "Server calls send the CSRF token in the X-CSRF-Token header, read from the jongo_csrf cookie; "
            "the browser runtime does this for you, so reload the page (the token may have expired). "
            "From tests use client.rpc(fn, ...); from curl, GET a page first and send its jongo_csrf cookie "
            "back as both the cookie and the X-CSRF-Token header.",
    "J206": "Server functions are called with POST /_jongo/rpc/<id> and a JSON body {\"args\": [...], "
            "\"kwargs\": {...}}; browser code does this for you with await fn(...).",
}

_FRAME_ANCESTORS = {"SAMEORIGIN": "frame-ancestors 'self'", "DENY": "frame-ancestors 'none'"}
#: A scheme name as browsers parse one (after dropping control characters and spaces).
_SCHEME = re.compile(r"[a-z][a-z0-9+.\-]*")


def _redirect_allowed(location: str) -> bool:
    """Whether a server call may send the browser to ``location``: a relative URL, or http(s).

    The browser runtime navigates to whatever a server call's redirect names, so a
    ``javascript:`` (or ``data:``, ``vbscript:``...) target would run script in the page.
    """
    text = str(location)
    if vdom._is_dangerous_url(text):
        return False
    head, sep, _ = text.partition(":")
    if not sep or any(c in head for c in "/?#"):
        return True  # no scheme: a path, ?query, #fragment or //host
    scheme = "".join(c for c in head if c > " ").lower()  # browsers ignore controls and spaces here
    if not _SCHEME.fullmatch(scheme):
        return True  # not a scheme ("1x:y"), so browsers read it as a relative path
    return scheme in ("http", "https")


_QUERY_TRUE = frozenset(("true", "1", "on", "yes"))
_QUERY_FALSE = frozenset(("false", "0", "off", "no"))
_SEQUENCES = (list, tuple, set, frozenset, collections.abc.Sequence, collections.abc.Iterable)


def _query_bool(text):
    """``"true"``/``"1"``/``"on"``/``"yes"`` -> True, the opposites -> False, anything else -> None."""
    word = text.strip().lower() if isinstance(text, str) else None
    return True if word in _QUERY_TRUE else False if word in _QUERY_FALSE else None


def _hint_options(hint) -> list:
    """The non-None types a hint allows: ``Optional[bool]`` -> ``[bool]``."""
    if typing.get_origin(hint) in (typing.Union, getattr(types, "UnionType", typing.Union)):
        return [a for a in typing.get_args(hint) if a is not type(None)]
    return [hint]


def _query_argument(query, name: str, hint):
    """A page or route argument from the query string, checked against its annotation.

    ``coerce()`` checks decoded JSON, where a bool is a bool and a list is a list; a query
    string holds only text. So ``bool`` accepts true/1/on/yes and false/0/off/no (any case),
    and a list annotation collects the repeated key (``?id=1&id=2``), each item checked.
    """
    options = _hint_options(hint)
    sequence = next((o for o in options if (typing.get_origin(o) or o) in _SEQUENCES and o is not str), None)
    if sequence is not None:
        items = query.getlist(name)
        inner = typing.get_args(sequence)
        if any(bool in _hint_options(t) for t in inner):  # list[bool]: convert the words first
            items = [b if (b := _query_bool(v)) is not None else v for v in items]
        return coerce(items, hint, name)
    raw = query[name]
    try:
        return coerce(raw, hint, name)
    except HTTPError as exc:
        word = _query_bool(raw) if exc.code == "J403" and bool in options else None
        if word is None:
            raise
        return coerce(word, hint, name)


class _LiveWatch(typing.NamedTuple):
    ids: dict = {}  # login_session= and user= for hub.subscribe(); never mutated
    alive: typing.Callable[[], bool] | None = None


def _caller_root(name: str | None) -> Path:
    """The directory of the file that called ``Jongo(...)``, for a name that isn't a loaded module."""
    try:
        caller = sys._getframe(2).f_globals.get("__file__")  # 0: here, 1: Jongo.__init__, 2: its caller
    except ValueError:
        caller = None
    if caller and not str(caller).startswith("<"):
        return Path(caller).resolve().parent
    log.warning("Jongo(%r) can't tell where the app's files are, so it uses the current directory (%s) for "
                "the database, the secret key and static/. Pass Jongo(__name__), or root=...", name, Path.cwd())
    return Path.cwd()


class Jongo:
    """A Jongo application.

    Parameters (the less obvious ones):
        name: pass ``__name__``. It locates the app's directory (``root``), where a relative
            ``database`` path, ``.jongo/secret`` and ``static/`` live. If it isn't the name of
            an imported module, the directory of the file that created the app is used.
        allowed_hosts: the ``Host`` names the app answers to, e.g. ``["example.com",
            ".example.com"]`` (a leading dot also allows subdomains; ``"*"`` allows any).
            Under ``jongo dev`` the app always answers ``localhost``, ``*.localhost`` and IP
            addresses, and refuses other names with a 400 (``J214``) unless they are listed
            here, so a web page can't reach the dev server by DNS rebinding. In production the
            check applies only when this is set.
        frame_options: ``"SAMEORIGIN"`` (the default) lets only the app's own pages frame it;
            ``"DENY"`` forbids framing; ``None`` sends no framing headers (for an app meant to
            be embedded elsewhere). Sent as ``X-Frame-Options`` and, unless the response
            already has a ``Content-Security-Policy``, as ``frame-ancestors``.
        trust_proxy: trust ``X-Forwarded-Proto``/``X-Forwarded-For`` (only behind a proxy you
            control).
    """

    def __init__(
        self,
        name: str | None = None,
        *,
        title: str = "Jongo",
        database: str | None = None,
        secret_key: str | None = None,
        dev: bool | None = None,
        lang: str = "en",
        head=(),
        root: str | os.PathLike | None = None,
        login_url: str = "/login",
        session_max_age: int = 14 * 24 * 3600,
        static_url: str = "/static",
        trust_proxy: bool = False,
        allowed_hosts=None,
        frame_options: str | None = "SAMEORIGIN",
    ):
        self.name = name or "app"
        module = sys.modules.get(name) if name else None
        if root is not None:
            self.root = Path(root).resolve()
        elif module is not None and getattr(module, "__file__", None):
            self.root = Path(module.__file__).resolve().parent
        else:
            self.root = _caller_root(name)
        if frame_options is not None:
            frame_options = str(frame_options).upper()
            if frame_options not in _FRAME_ANCESTORS:
                raise ValueError(f"frame_options must be 'SAMEORIGIN', 'DENY' or None, not {frame_options!r}")
        #: Sent as X-Frame-Options (and CSP frame-ancestors) on every response; None sends neither.
        self.frame_options = frame_options
        if isinstance(allowed_hosts, str):
            allowed_hosts = [allowed_hosts]
        #: The Host names this app answers to beyond this machine's (see the class docstring), or None.
        self.allowed_hosts = [str(h) for h in allowed_hosts] if allowed_hosts is not None else None
        self.title = title
        self.dev = dev if dev is not None else os.environ.get("JONGO_DEV") == "1"
        self.lang = lang
        self.head = list(head)
        self.login_url = login_url
        self.session_max_age = session_max_age
        self.static_url = static_url.rstrip("/")
        self.trust_proxy = trust_proxy  # trust X-Forwarded-Proto only when behind a known proxy
        self.router = Router()
        self.boot_id = secrets.token_hex(8)
        self.before_request_hooks: list = []
        self.after_request_hooks: list = []
        self.error_handlers: dict[int, typing.Callable] = {}
        self.channels: list[live_module.Channel] = []
        self.layout_component = None
        self.layout_props = None
        #: The mounted MCP endpoint (a jongo.mcp.McpServer) and its path, once app.mcp() is called.
        self.mcp_server = None
        self.mcp_path = None
        self._bundle: tuple[str, str] | None = None
        self._bundle_lock = threading.Lock()
        self.monitor = Monitor()
        #: Where the signing key came from: "argument", "env", "file" or "ephemeral" (for `jongo doctor`).
        self.secret_source = "argument" if secret_key else ("env" if os.environ.get("JONGO_SECRET_KEY") else None)
        secret = secret_key or os.environ.get("JONGO_SECRET_KEY") or self._local_secret()
        self.secret_length = len(secret)  # for `jongo doctor`; the secret itself is never kept or reported
        self.signer = Signer(secret)
        if database is not None:
            from . import db

            path = database if database == ":memory:" or "://" in database else str(self.root / database)
            db.configure(path)
        self._install_internal_routes()

    # -- registration ------------------------------------------------------------------

    def page(self, path: str, *, title=None, layout: bool = True, name=None, login_required=False,
             admin_required=False, rate_limit=None):
        """Register a page. The function returns UI (a component or elements), a Page, or a Response.

        ``rate_limit="30/minute"`` caps how often one caller (the signed-in user, else the
        client address) may load it; over the limit they get a 429 (``J213``).
        """

        def decorator(fn):
            self.router.add(
                Route(path, fn, methods=("GET",), kind="page", name=name, options={
                    "title": title, "layout": layout,
                    "login_required": login_required or admin_required, "admin_required": admin_required,
                    "rate_limit": _parse_rate(rate_limit),
                })
            )
            return fn

        return decorator

    def route(self, path: str, *, methods=("GET",), name=None, csrf=True, login_required=False,
              admin_required=False, rate_limit=None):
        """Register a plain handler. Return a Response, str (HTML), dict/list (JSON) or UI.

        ``rate_limit="5/minute"`` caps how often one caller (the signed-in user, else the
        client address) may call it; over the limit they get a 429 with ``Retry-After``
        (``J213``) and the handler doesn't run. See :mod:`jongo.ratelimit`.
        """

        def decorator(fn):
            self.router.add(
                Route(path, fn, methods=methods, kind="route", name=name, options={
                    "csrf": csrf, "login_required": login_required or admin_required, "admin_required": admin_required,
                    "rate_limit": _parse_rate(rate_limit),
                })
            )
            return fn

        return decorator

    def get(self, path: str, **options):
        return self.route(path, methods=("GET",), **options)

    def post(self, path: str, **options):
        return self.route(path, methods=("POST",), **options)

    def channel(self, pattern: str):
        """Declare a channel components may subscribe to with ``live()``.

        The decorated function authorises each subscription: return True to allow it and
        False (or raise Forbidden) to refuse it. Returning nothing refuses too (``J412``),
        so a guard that forgets its ``return`` fails closed. Pattern parameters work as
        they do in routes::

            @app.channel("room:<int:id>")
            def room(request, id):
                return id in request.session.get("rooms", [])

        A channel that is not declared cannot be subscribed to at all — the same rule
        that keeps undecorated functions off the RPC boundary.
        """

        def decorator(fn):
            self.channels.append(live_module.Channel(pattern, fn))
            return fn

        return decorator

    def layout(self, component=None, *, props=None):
        """Wrap every page in ``component``. ``props(request)`` may supply extra props."""

        def decorator(comp):
            self.layout_component = comp
            self.layout_props = props
            return comp

        return decorator(component) if component is not None else decorator

    def before_request(self, fn):
        self.before_request_hooks.append(fn)
        return fn

    def after_request(self, fn):
        self.after_request_hooks.append(fn)
        return fn

    def errorhandler(self, status: int):
        def decorator(fn):
            self.error_handlers[status] = fn
            return fn

        return decorator

    def url_for(self, name: str, **params) -> str:
        return self.router.url_for(name, **params)

    # -- WSGI ------------------------------------------------------------------------------

    def __call__(self, environ, start_response):
        request = Request(environ, self)
        response = self.handle(request)
        return response(environ, start_response)

    def handle(self, request: Request) -> Response:
        started = time.perf_counter()
        with jlog.request_scope() as rid:
            request.request_id = rid
            try:
                self._check_host(request)
                self._load_session(request)
                response = None
                for hook in self.before_request_hooks:
                    result = hook(request)
                    if result is not None:
                        response = self.to_response(result, request)
                        break
                if response is None:
                    response = self._dispatch(request)
            except HTTPError as exc:
                response = self.http_error(request, exc)
            except Exception as exc:
                response = self.server_error(request, exc)
            try:
                for hook in self.after_request_hooks:
                    response = hook(request, response) or response
                self._finish(request, response)
            except Exception as exc:
                # A failing after_request hook or _finish (e.g. a non-serialisable session)
                # must still yield a proper 500 instead of escaping to the WSGI server.
                response = self.server_error(request, exc)
                try:
                    self._finish(request, response)
                except Exception:
                    jlog.get_logger("http").error("failed to finalise error response", exc_info=True)
            path = request.path
            if not path.startswith("/_jongo/") or path.startswith("/_jongo/rpc/"):
                # Recorded for the live system view: no query string, headers or bodies.
                elapsed = (time.perf_counter() - started) * 1000
                route = getattr(request, "route", None)
                key = None
                if route is not None:
                    key = route.path
                    if route.name == "_jongo_rpc":  # one counter per server function, not one for all
                        fid = request.path_params.get("function_id")
                        key = f"@server {fid}" if fid in SERVER_FUNCTIONS else "@server (unknown)"
                stats = jlog.current_stats()
                self.monitor.record_request(
                    request.method, path[:200], response.status, elapsed, rid,
                    key, stats["sql"], stats["sql_ms"],
                    jlog.n_plus_one() if stats["sql"] else None, getattr(request, "error_code", None),
                )
                if self.dev:
                    jlog.request_done(request.method, request.full_path, response.status, elapsed,
                                      route=route.name if route is not None else None)
            return response

    def _check_host(self, request: Request) -> None:
        """Refuse a Host header that doesn't name this app (J214); see ``allowed_hosts``."""
        host = request.headers.get("host")
        if host is None or host == request.environ.get(IN_PROCESS_HOST):
            return  # no Host to rebind (HTTP/1.0), or an in-process client's made-up name
        if not self.dev and not self.allowed_hosts:
            return
        if host_allowed(host, self.allowed_hosts or (), dev=self.dev):
            return
        shown = host if len(host) <= 100 else host[:100] + "…"
        if self.dev:
            hint = ("under jongo dev the app answers localhost, *.localhost and IP addresses only, so a web page "
                    f"can't reach it by DNS rebinding; to use this name, list it: Jongo(__name__, "
                    f"allowed_hosts=[{_hostname(shown)!r}])")
        else:
            hint = "add the host to Jongo(allowed_hosts=[...]) if this app should answer it"
        raise HTTPError(400, f"This app doesn't answer to the host {shown!r}", code="J214", hint=hint)

    def _dispatch(self, request: Request) -> Response:
        route, params = self.router.match(request.method, request.path)
        request.route = route
        request.path_params = params
        options = route.options
        if request.method not in SAFE_METHODS and options.get("csrf", True) and not request.csrf_ok():
            raise HTTPError(403, "The form expired (CSRF check failed). Reload the page and try again.", code="J207")
        if options.get("login_required") and not request.user:
            if route.kind == "page":
                return redirect(f"{self.login_url}?next={urllib.parse.quote(request.full_path, safe='')}", 302)
            raise HTTPError(401, "Please log in first", code="J208")
        if options.get("admin_required") and not getattr(request.user, "is_admin", False):
            raise HTTPError(403, "Admins only", code="J209")
        if options.get("rate_limit") is not None:
            _check_rate(request, options["rate_limit"], f"{route.kind} {route.path}")
        if route.kind == "page":
            return self.render_page(request, self.call_handler(route, request, params), route)
        return self.to_response(self.call_handler(route, request, params), request)

    def call_handler(self, route: Route, request: Request, params: dict):
        handler = route.handler
        signature = inspect.signature(handler)
        try:
            hints = typing.get_type_hints(handler)
        except Exception:
            hints = {}
        kwargs = {}
        takes_kwargs = any(p.kind is p.VAR_KEYWORD for p in signature.parameters.values())
        for name, param in signature.parameters.items():
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                continue
            if name == "request":
                kwargs[name] = request
            elif name in params:
                kwargs[name] = params[name]
            elif name in request.query:
                raw = request.query[name]
                try:
                    kwargs[name] = _query_argument(request.query, name, hints.get(name, typing.Any))
                except HTTPError as exc:
                    if exc.code != "J403":
                        raise
                    shown = raw if len(str(raw)) <= 40 else str(raw)[:40] + "…"
                    hint = hints.get(name)
                    raise HTTPError(
                        400, f"query parameter {name!r} must be {_label(hint)}, "
                             f"got {shown!r}", code="J211",
                        hint=f"fix the link or form that builds ?{name}=…, or annotate {name} with a type that "
                             "accepts it and handle the value in the function",
                    ) from None
        if takes_kwargs:
            for name, value in params.items():
                kwargs.setdefault(name, value)
        return handler(**kwargs)

    # -- pages -------------------------------------------------------------------------------

    def render_page(self, request: Request, result, route: Route | None = None) -> Response:
        if isinstance(result, Response):
            return result
        page = result if isinstance(result, Page) else Page(result)
        options = route.options if route else {}
        title = page.title or options.get("title") or self.title
        tree = page.content
        if isinstance(tree, (list, tuple)):
            tree = vdom.Tag("div")(*tree)
        elif isinstance(tree, str):
            tree = vdom.Tag("div")(tree)
        if options.get("layout", True) and self.layout_component is not None:
            extra = self.layout_props(request) if self.layout_props else {}
            tree = self.layout_component(tree, **extra)

        # Render the HTML from the *serialised* tree, not the original one, so the server
        # and the browser start from identical data and cannot diverge. The renderer walks
        # that data directly: rebuilding a second VNode tree to render from was a full
        # extra pass and an extra tree per request, and reading the data itself is the
        # stronger guarantee anyway.
        data = serialize(tree)
        encoded = json.dumps(data, separators=(",", ":"))
        body_html = render_to_string(data)
        _, build_hash = self.bundle()
        headers = {"Vary": "X-Jongo-Nav", "Cache-Control": "no-store"}
        if request.is_navigation:
            payload = json.dumps({"title": title, "tree": data, "build": build_hash}, separators=(",", ":"))
            return Response(payload, page.status, {**headers, "X-Jongo-Page": "1"}, content_type="application/json")
        return Response(self.document(request, title, body_html, encoded, page), page.status, headers)

    def document(self, request: Request, title: str, body_html: str, tree_json: str, page: Page) -> str:
        _, build_hash = self.bundle()
        boot = (
            '{"tree":' + tree_json
            + f',"build":{json.dumps(build_hash)},"boot":{json.dumps(self.boot_id)}'
            + f',"dev":{json.dumps(self.dev)},"csrf":{json.dumps(request.csrf_token)}}}'
        ).replace("<", "\\u003c")  # no </script> or <!-- can end or derail this <script> block
        head = "".join(
            render_to_string(item) if isinstance(item, VNode) else str(item) for item in [*self.head, *page.head]
        )
        css_hash = hashlib.sha1(collect_css().encode(), usedforsecurity=False).hexdigest()[:10]
        return (
            f'<!doctype html>\n<html lang="{_html.escape(self.lang)}">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"<title>{_html.escape(title)}</title>\n"
            '<link rel="icon" href="data:,">\n'
            f'<link rel="stylesheet" href="/_jongo/app.css?v={css_hash}">\n{head}\n</head>\n<body>\n'
            f'<div id="jongo-root">{body_html}</div>\n'
            f'<script id="jongo-data" type="application/json">{boot}</script>\n'
            f'<script src="/_jongo/app.js?v={build_hash}" defer></script>\n</body>\n</html>\n'
        )

    def bundle(self) -> tuple[str, str]:
        """The compiled browser bundle and its hash (built once per process)."""
        if self._bundle is None:
            with self._bundle_lock:
                if self._bundle is None:
                    from .compiler import build_bundle

                    self._bundle = build_bundle(roots=[self.root])
        return self._bundle

    def to_response(self, result, request: Request) -> Response:
        if isinstance(result, Response):
            return result
        if isinstance(result, (VNode, Page)):
            return self.render_page(request, result)
        if result is None:
            return Response(b"", 204, content_type=None)
        if isinstance(result, str):
            return Response(result)
        if isinstance(result, bytes):
            return Response(result, content_type="application/octet-stream")
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], int):
            response = self.to_response(result[0], request)
            response.status = result[1]
            return response
        return json_response(result)

    # -- sessions and auth ---------------------------------------------------------------------

    def _local_secret(self) -> str:
        path = self.root / ".jongo" / "secret"
        self.secret_source = "file"
        try:
            if path.exists():
                existing = path.read_text().strip()
                if existing:
                    return existing
                # An empty file (e.g. a crash mid-write) must never become an empty HMAC
                # key: anyone could then forge session cookies. Replace it.
                path.unlink()
            path.parent.mkdir(parents=True, exist_ok=True)
            secret = secrets.token_urlsafe(48)
            # Create it owner-only from the start: write-then-chmod left a window where the
            # key was readable by other local users (umask 022).
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:  # another process (e.g. a worker) created it just now
                time.sleep(0.05)
                return path.read_text().strip() or secret
            with os.fdopen(fd, "w") as fh:
                fh.write(secret)
            return secret
        except OSError:
            log.warning("couldn't persist a secret key; sessions will reset on restart (set JONGO_SECRET_KEY)")
            self.secret_source = "ephemeral"
            return secrets.token_urlsafe(48)

    def _load_session(self, request: Request):
        token = request.cookies.get(SESSION_COOKIE)
        data = self.signer.loads(token, self.session_max_age) if token else None
        request.session = Session(data if isinstance(data, dict) else {})
        # Snapshot the loaded state so in-place nested mutation (session["x"].append(...))
        # is detected in _finish even though it doesn't set Session.modified.
        request._session_loaded = json.dumps(dict(request.session), sort_keys=True, default=str)

    def _session_changed(self, request: Request) -> bool:
        if request.session.modified:
            return True
        current = json.dumps(dict(request.session), sort_keys=True, default=str)
        return current != getattr(request, "_session_loaded", "{}")

    def _finish(self, request: Request, response: Response):
        # Stop browsers from sniffing a response into a different (executable) type,
        # e.g. an uploaded .txt served from /static rendered as HTML.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if self.frame_options:
            # Clickjacking: only the app's own pages may frame it. A header the response set
            # itself (the admin's DENY, an app's own CSP) is kept.
            present = {name.lower() for name in response.headers}
            if "x-frame-options" not in present:
                response.headers["X-Frame-Options"] = self.frame_options
            if "content-security-policy" not in present:
                response.headers["Content-Security-Policy"] = _FRAME_ANCESTORS[self.frame_options]
        if self._session_changed(request):
            if request.session:
                value = self.signer.dumps(dict(request.session))
                if len(value) > 3800:
                    log.warning("session cookie is %d bytes; browsers may drop cookies over 4KB", len(value))
                response.set_cookie(
                    SESSION_COOKIE, value, max_age=self.session_max_age, secure=request.scheme == "https"
                )
            else:
                response.delete_cookie(SESSION_COOKIE)
        if request.cookies.get(CSRF_COOKIE) != request.csrf_token:
            response.set_cookie(
                CSRF_COOKIE, request.csrf_token, max_age=365 * 24 * 3600, httponly=False,
                secure=request.scheme == "https",
            )
        # A header value the server can't send would fail after the status line has gone out
        # (a truncated response). Fail here instead, as a proper 500 that names the header.
        response.wsgi_headers()

    def load_user(self, request: Request):
        auth = sys.modules.get("jongo.auth")
        return auth.get_user(request) if auth else None

    def _live_watch(self, request: Request) -> "_LiveWatch":
        """Who a live stream belongs to, and (when a user is signed in) a check that the
        sign-in still holds. The stream re-runs the check as it goes and ends once the
        session is revoked, has expired or the password changed (see ``live.SESSION_CHECK_SECONDS``)."""
        if not request.session.get("_sid"):
            return _LiveWatch()
        try:
            user = request.user  # usually loaded already, by the channel guard
        except Exception:
            log.warning("couldn't load the user for a live stream; it won't be ended on sign-out", exc_info=True)
            return _LiveWatch()
        if user is None:
            return _LiveWatch()
        uid = getattr(user, "pk", None)

        def alive() -> bool:
            current = self.load_user(request)  # the same lookup every request makes
            return current is not None and getattr(current, "pk", None) == uid

        return _LiveWatch({"login_session": getattr(request.login_session, "pk", None), "user": uid}, alive)

    # -- errors ------------------------------------------------------------------------------------

    def http_error(self, request: Request, exc: HTTPError) -> Response:
        headers = {"Allow": ", ".join(exc.allowed)} if getattr(exc, "allowed", None) else {}
        if getattr(exc, "retry_after", None):  # a rate limit: tell well-behaved clients when to come back
            headers["Retry-After"] = str(exc.retry_after)
        route = getattr(request, "route", None)
        # also /_jongo/status/ and //_jongo/status; the other machine-facing endpoints answer in JSON too
        # (the live stream's refusals included, so the browser runtime can read why it was refused)
        is_status = route is not None and route.name in ("_jongo_status", "_jongo_mcp", "_jongo_openapi",
                                                         "_jongo_live")
        if request.path.startswith("/_jongo/rpc/") or is_status:
            code, hint = classify(exc)
            if request.path.startswith("/_jongo/rpc/") and code in _RPC_HINTS and not getattr(exc, "hint", None):
                hint = _RPC_HINTS[code]  # the generic hints are about forms and routes, not server calls
            request.error_code = code
            error = {"type": type(exc).__name__, "message": exc.message, "code": code, "hint": hint,
                     "request_id": getattr(request, "request_id", None)}
            return json_response({"error": error}, exc.status, headers)
        request.error_code = exc.code
        handler = self.error_handlers.get(exc.status)
        if handler is not None:
            try:
                accepts = inspect.signature(handler).parameters
                result = handler(**{k: v for k, v in (("request", request), ("error", exc)) if k in accepts})
                response = self.render_page(request, result) if not isinstance(result, Response) else result
                response.status = exc.status
                response.headers.update(headers)
                return response
            except Exception as inner:  # a broken error handler shouldn't hide the original error
                log.exception("error handler for %s failed", exc.status)
                if self.dev:
                    return self.server_error(request, inner)
        from .errors_page import http_error_page

        if self.dev:  # the code and the hint help the developer; production pages stay plain
            code, hint = classify(exc)
            return Response(http_error_page(exc, request_id=getattr(request, "request_id", None), code=code,
                                            hint=hint), exc.status, headers)
        return Response(http_error_page(exc), exc.status, headers)

    def server_error(self, request: Request, exc: Exception) -> Response:
        rid = getattr(request, "request_id", None)
        is_rpc = request.path.startswith("/_jongo/rpc/")
        code, hint = classify(exc)
        if code == "J000" and is_rpc:
            code, hint = "J407", hint or "the server function's own code raised; see the traceback"
        request.error_code = code
        try:
            self.monitor.record_error(exc, rid=rid, method=request.method, path=request.path, root=self.root,
                                      code=code, hint=hint)
        except Exception:  # the recorder must never turn one error into two
            pass
        jlog.get_logger("http").error("error handling %s %s", request.method, request.full_path, exc_info=exc,
                                      extra=jlog.fields(code=code))
        if is_rpc:
            if self.dev:
                error = {"type": type(exc).__name__, "message": str(exc), "code": code, "hint": hint,
                         "request_id": rid}
                file, line = app_location(exc, self.root)
                if file:
                    error["file"], error["line"] = file, line
                error["traceback"] = "".join(traceback.format_exception(exc))
            else:  # production: no message, no hint, no location — just enough to find it in the log
                error = {"type": type(exc).__name__, "message": "Server error", "code": code, "request_id": rid}
            return json_response({"error": error}, 500)
        handler = self.error_handlers.get(500)
        if handler is not None and not self.dev:
            try:
                return self.to_response(handler(), request)
            except Exception:
                log.exception("500 handler failed")
        from .errors_page import debug_page, http_error_page

        if self.dev:
            return Response(debug_page(exc, request, self.root, code=code, hint=hint, request_id=rid), 500)
        return Response(http_error_page(HTTPError(500), request_id=rid), 500)

    # -- built-in routes -------------------------------------------------------------------------------

    def _install_internal_routes(self):
        app = self

        def asset(content: str, content_type: str, version: str, request: Request) -> Response:
            fresh = request.query.get("v") == version and not app.dev
            cache = "public, max-age=31536000, immutable" if fresh else "no-cache"
            etag = f'"{version}"'
            if request.headers.get("if-none-match") == etag:
                return Response(b"", 304, {"ETag": etag, "Cache-Control": cache}, content_type=None)
            return Response(content, 200, {"ETag": etag, "Cache-Control": cache}, content_type=content_type)

        def app_js(request):
            js, build_hash = app.bundle()
            return asset(js, "application/javascript; charset=utf-8", build_hash, request)

        def app_css(request):
            css = collect_css()
            return asset(css, "text/css; charset=utf-8", hashlib.sha1(css.encode(), usedforsecurity=False).hexdigest()[:10], request)

        def rpc(request, function_id: str):
            fn = SERVER_FUNCTIONS.get(function_id)
            if fn is None:
                raise NotFound(f"no server function {function_id!r}", code="J401")
            try:
                # Only an empty body means "no arguments": `[]`, `null` or `0` is a malformed call (J402).
                payload = request.json if request.body.strip() else {}
            except HTTPError as exc:
                if exc.status != 400:  # too large stays J212
                    raise
                raise HTTPError(400, f"malformed server function call: {exc.message}", code="J402") from None
            if not isinstance(payload, dict):
                raise HTTPError(400, 'malformed server function call: the body must be a JSON object '
                                     '{"args": [...], "kwargs": {...}}', code="J402")
            try:
                result = fn.invoke(request, payload.get("args", []), payload.get("kwargs", {}))
            except HTTPError:
                raise
            except Exception as exc:
                errors = getattr(exc, "errors", None)
                if type(exc).__name__ == "ValidationError" and isinstance(errors, dict):
                    message = "; ".join(str(v) for v in errors.values()) or str(exc)
                    return json_response(
                        {"error": {"type": "ValidationError", "message": message, "errors": errors,
                                   "code": "J304", "hint": exc.hint or hint_for("J304"),
                                   "request_id": request.request_id}}, 400
                    )
                raise
            if isinstance(result, Response):
                location = result.headers.get("Location")
                if location:
                    if not _redirect_allowed(location):
                        shown = location if len(location) <= 60 else location[:60] + "…"
                        raise HTTPError(
                            500, f"server function {function_id} returned redirect({shown!r}); a server call "
                                 "may only redirect to a relative URL or an http(s) one", code="J408",
                            hint="the browser would run a javascript: (or data:) target as script; check a "
                                 "target taken from the request with is_local_url() before redirecting to it",
                        )
                    response = json_response({"redirect": location})
                    response.cookies.extend(result.cookies)
                    return response
                raise HTTPError(500, "server functions can return data or redirect(), not other responses",
                                code="J408")
            return json_response({"ok": result})

        def live(request):
            """Two streams on one route: dev live-reload, and declared channels."""
            channels = live_module.parse_channels(request.query.get("channels"))
            if channels:
                allowed = live_module.authorize(channels, app.channels, request)  # eager: a refusal is a 403
                headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
                if request.method != "GET":  # HEAD: the headers a GET would get, and nothing to subscribe
                    return Response(b"", headers=headers, content_type="text/event-stream")
                live_module.hub.check_capacity()
                watch = app._live_watch(request)

                def stream():
                    # Subscribed only once the server starts sending, so a response that is
                    # never sent (or sent without a body) can't leave a subscriber behind.
                    try:
                        subscriber = live_module.hub.subscribe(allowed, **watch.ids)
                    except live_module.HubFull:
                        yield f"retry: {live_module.FULL_RETRY_MS}\n\n"
                        return
                    try:
                        yield from live_module.event_stream(subscriber, alive=watch.alive)
                    finally:  # the WSGI server closes the iterator when the client goes
                        live_module.hub.unsubscribe(subscriber)

                return Response(stream(), headers=headers, content_type="text/event-stream")

            if not app.dev:
                raise NotFound()

            def reload_stream():
                yield f"retry: 400\ndata: {app.boot_id}\n\n"
                while True:
                    time.sleep(15)
                    yield ": ping\n\n"

            return Response(reload_stream(), headers={"Cache-Control": "no-cache"},
                            content_type="text/event-stream")

        def static(request, path: str):
            if "\x00" in path:
                raise NotFound()
            try:  # an over-long name (ENAMETOOLONG) or an unreadable file is "not found", not a 500
                base = (app.root / "static").resolve()
                target = (base / path).resolve()
                if not target.is_relative_to(base) or not target.is_file():
                    raise NotFound()
                stat = target.stat()
                etag = f'"{int(stat.st_mtime)}-{stat.st_size}"'
                if request.headers.get("if-none-match") == etag:
                    return Response(b"", 304, {"ETag": etag}, content_type=None)
                data = target.read_bytes()
            except (ValueError, OSError):
                raise NotFound() from None
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return Response(data, 200, {"ETag": etag, "Cache-Control": "public, max-age=3600"}, content_type)

        self.router.add(Route("/_jongo/app.js", app_js, name="_jongo_js", kind="internal"))
        self.router.add(Route("/_jongo/app.css", app_css, name="_jongo_css", kind="internal"))
        self.router.add(Route("/_jongo/rpc/<path:function_id>", rpc, methods=("POST",), name="_jongo_rpc", kind="internal"))
        self.router.add(Route("/_jongo/live", live, name="_jongo_live", kind="internal"))
        def status_view(request):
            """The live system view as JSON. Open in dev; an admin or the status token in production."""
            from .monitor import status

            if os.environ.get("JONGO_STATUS", "").lower() in ("0", "off", "false", "no"):
                raise NotFound()
            if not app._status_open() and not app._status_allowed(request):
                raise HTTPError(
                    403, "The status endpoint needs a signed-in admin or the status token in production.",
                    code="J508",
                )
            return json_response(status(app), headers={"Cache-Control": "no-store"})

        def openapi_view(request):
            """The OpenAPI document. Open in dev; an admin or the status token in production."""
            from .schema import openapi

            if os.environ.get("JONGO_STATUS", "").lower() in ("0", "off", "false", "no"):
                raise NotFound()
            if not app._status_open() and not app._status_allowed(request):
                raise HTTPError(
                    403, "The OpenAPI document needs a signed-in admin or the status token in production.",
                    code="J508",
                )
            return json_response(openapi(app), headers={"Cache-Control": "no-store"})

        self.router.add(Route(f"{self.static_url}/<path:path>", static, name="static", kind="internal"))
        self.router.add(Route("/_jongo/status", status_view, name="_jongo_status", kind="internal"))
        self.router.add(Route("/_jongo/openapi.json", openapi_view, name="_jongo_openapi", kind="internal"))

    def _status_open(self) -> bool:
        """/_jongo/status needs no credentials in dev mode, unless JONGO_ENV says this is production."""
        from .diagnostics import declared_production

        return self.dev and not declared_production()

    def _status_allowed(self, request: Request) -> bool:
        """Production access to /_jongo/status: an admin session, or ``Bearer $JONGO_STATUS_TOKEN``.

        The token must be at least 16 characters, so an empty or toy value never opens it.
        """
        token = (os.environ.get("JONGO_STATUS_TOKEN") or "").strip()
        supplied = request.headers.get("authorization") or ""
        if len(token) >= 16 and hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
            return True
        try:
            return bool(getattr(request.user, "is_admin", False))
        except Exception:
            return False

    def admin(self, path: str = "/admin", *, models=None, title: str = "Jongo admin"):
        """Mount the generated admin site. Sign in with a user that has ``is_admin=True``."""
        from .admin import Admin

        return Admin(self, path, models=models, title=title)

    def mcp(self, path: str = "/_jongo/mcp", *, tools=None, act_as=None):
        """Mount an MCP endpoint, so a coding agent can inspect and call the running app.

        Until this is called the app has no MCP endpoint. See :mod:`jongo.mcp` for the
        protocol and the tools; ``jongo mcp`` serves the same thing over stdio without
        touching the app's code.

        Parameters:
            path: where to mount it.
            tools: which ``@server`` functions agents may call: a list of the functions,
                ``"all"``, or ``None`` (the default) for only the read-only ``jongo_*``
                tools (context, check, doctor, explain, status, openapi, get).
            act_as: who a tool call runs as: a user, or ``act_as(request) -> User | None``.
                Default: anonymous, so ``login_required`` functions refuse (``J208``).

        Access: under ``jongo dev``, this machine only (as ``localhost``/``127.0.0.1``). In production,
        ``Authorization: Bearer $JONGO_MCP_TOKEN`` (16+ characters) is required; with the
        variable unset the endpoint refuses everyone (``J420``).

        Returns:
            The :class:`jongo.mcp.McpServer`.

        Example::

            app.mcp(tools=[add_todo, toggle_todo])
            # then, in the project:  claude mcp add --transport http myapp http://127.0.0.1:8000/_jongo/mcp
        """
        from .mcp import McpServer

        server = McpServer(self, tools=tools, act_as=act_as)
        self.mcp_server = server
        self.mcp_path = path
        self.router.add(Route(path, server.http, methods=("GET", "POST", "DELETE"), name="_jongo_mcp",
                              kind="internal", options={"csrf": False}))
        return server

    # -- running ----------------------------------------------------------------------------------------

    def check(self) -> None:
        """Compile every component now so errors surface at startup instead of first request."""
        self._bundle = None
        self.bundle()

    def run(self, host: str = "127.0.0.1", port: int = 8000, **kwargs):
        from .server import serve

        serve(self, host=host, port=port, **kwargs)

    def test_client(self):
        from .testing import TestClient

        return TestClient(self)
