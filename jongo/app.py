"""The Jongo application: routing, pages, server functions, sessions and the WSGI entry point."""
from __future__ import annotations

import hashlib
import html as _html
import inspect
import json
import logging
import mimetypes
import os
import secrets
import sys
import threading
import time
import traceback
import typing
import urllib.parse
from pathlib import Path

from . import vdom
from . import log as jlog
from .errors import CompileError, HTTPError, NotFound
from .http import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    Request,
    Response,
    Session,
    Signer,
    json_response,
    redirect,
)
from .routing import Route, Router
from .rpc import SERVER_FUNCTIONS, coerce
from .styles import collect_css
from .vdom import VNode, build, render_to_string, serialize

log = logging.getLogger("jongo")
SAFE_METHODS = frozenset(("GET", "HEAD", "OPTIONS"))


class Page:
    """Return from a page function to set the title, status or extra <head> tags."""

    def __init__(self, content, *, title: str | None = None, status: int = 200, head=()):
        self.content = content
        self.title = title
        self.status = status
        self.head = list(head)


class Jongo:
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
    ):
        self.name = name or "app"
        module = sys.modules.get(name) if name else None
        if root is not None:
            self.root = Path(root).resolve()
        elif module is not None and getattr(module, "__file__", None):
            self.root = Path(module.__file__).resolve().parent
        else:
            self.root = Path.cwd()
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
        self.layout_component = None
        self.layout_props = None
        self._bundle: tuple[str, str] | None = None
        self._bundle_lock = threading.Lock()
        self.signer = Signer(secret_key or os.environ.get("JONGO_SECRET_KEY") or self._local_secret())
        if database is not None:
            from . import db

            path = database if database == ":memory:" or "://" in database else str(self.root / database)
            db.configure(path)
        self._install_internal_routes()

    # -- registration ------------------------------------------------------------------

    def page(self, path: str, *, title=None, layout: bool = True, name=None, login_required=False, admin_required=False):
        """Register a page. The function returns UI (a component or elements), a Page, or a Response."""

        def decorator(fn):
            self.router.add(
                Route(path, fn, methods=("GET",), kind="page", name=name, options={
                    "title": title, "layout": layout,
                    "login_required": login_required or admin_required, "admin_required": admin_required,
                })
            )
            return fn

        return decorator

    def route(self, path: str, *, methods=("GET",), name=None, csrf=True, login_required=False, admin_required=False):
        """Register a plain handler. Return a Response, str (HTML), dict/list (JSON) or UI."""

        def decorator(fn):
            self.router.add(
                Route(path, fn, methods=methods, kind="route", name=name, options={
                    "csrf": csrf, "login_required": login_required or admin_required, "admin_required": admin_required,
                })
            )
            return fn

        return decorator

    def get(self, path: str, **options):
        return self.route(path, methods=("GET",), **options)

    def post(self, path: str, **options):
        return self.route(path, methods=("POST",), **options)

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
        with jlog.request_scope():
            try:
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
            if self.dev and not (request.path.startswith("/_jongo/app") or request.path == "/_jongo/live"):
                elapsed = (time.perf_counter() - started) * 1000
                route_name = getattr(getattr(request, "route", None), "name", None)
                jlog.request_done(request.method, request.full_path, response.status, elapsed, route=route_name)
            return response

    def _dispatch(self, request: Request) -> Response:
        route, params = self.router.match(request.method, request.path)
        request.route = route
        request.path_params = params
        options = route.options
        if request.method not in SAFE_METHODS and options.get("csrf", True) and not request.csrf_ok():
            raise HTTPError(403, "The form expired (CSRF check failed). Reload the page and try again.")
        if options.get("login_required") and not request.user:
            if route.kind == "page":
                return redirect(f"{self.login_url}?next={urllib.parse.quote(request.full_path, safe='')}", 302)
            raise HTTPError(401, "Please log in first")
        if options.get("admin_required") and not getattr(request.user, "is_admin", False):
            raise HTTPError(403, "Admins only")
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
                kwargs[name] = coerce(request.query[name], hints.get(name, typing.Any), name)
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

        data = serialize(tree)
        encoded = json.dumps(data, separators=(",", ":"))
        body_html = render_to_string(build(json.loads(encoded)))
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
        ).replace("</", "<\\/")
        head = "".join(
            render_to_string(item) if isinstance(item, VNode) else str(item) for item in [*self.head, *page.head]
        )
        css_hash = hashlib.sha1(collect_css().encode()).hexdigest()[:10]
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
        try:
            if path.exists():
                return path.read_text().strip()
            path.parent.mkdir(parents=True, exist_ok=True)
            secret = secrets.token_urlsafe(48)
            path.write_text(secret)
            path.chmod(0o600)
            return secret
        except OSError:
            log.warning("couldn't persist a secret key; sessions will reset on restart (set JONGO_SECRET_KEY)")
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

    def load_user(self, request: Request):
        auth = sys.modules.get("jongo.auth")
        return auth.get_user(request) if auth else None

    # -- errors ------------------------------------------------------------------------------------

    def http_error(self, request: Request, exc: HTTPError) -> Response:
        headers = {"Allow": ", ".join(exc.allowed)} if getattr(exc, "allowed", None) else {}
        if request.path.startswith("/_jongo/rpc/"):
            return json_response({"error": {"type": type(exc).__name__, "message": exc.message}}, exc.status, headers)
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

        return Response(http_error_page(exc), exc.status, headers)

    def server_error(self, request: Request, exc: Exception) -> Response:
        jlog.get_logger("http").error("error handling %s %s", request.method, request.full_path, exc_info=exc)
        if request.path.startswith("/_jongo/rpc/"):
            error = {"type": type(exc).__name__, "message": str(exc) if self.dev else "Server error"}
            if self.dev:
                error["traceback"] = "".join(traceback.format_exception(exc))
            return json_response({"error": error}, 500)
        handler = self.error_handlers.get(500)
        if handler is not None and not self.dev:
            try:
                return self.to_response(handler(), request)
            except Exception:
                log.exception("500 handler failed")
        from .errors_page import debug_page, http_error_page

        if self.dev:
            return Response(debug_page(exc, request, self.root), 500)
        return Response(http_error_page(HTTPError(500)), 500)

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
            return asset(css, "text/css; charset=utf-8", hashlib.sha1(css.encode()).hexdigest()[:10], request)

        def rpc(request, function_id: str):
            fn = SERVER_FUNCTIONS.get(function_id)
            if fn is None:
                raise NotFound(f"no server function {function_id!r}")
            payload = request.json or {}
            if not isinstance(payload, dict):
                raise HTTPError(400, "malformed server function call")
            try:
                result = fn.invoke(request, payload.get("args", []), payload.get("kwargs", {}))
            except HTTPError:
                raise
            except Exception as exc:
                errors = getattr(exc, "errors", None)
                if type(exc).__name__ == "ValidationError" and isinstance(errors, dict):
                    message = "; ".join(str(v) for v in errors.values()) or str(exc)
                    return json_response(
                        {"error": {"type": "ValidationError", "message": message, "errors": errors}}, 400
                    )
                raise
            if isinstance(result, Response):
                location = result.headers.get("Location")
                if location:
                    response = json_response({"redirect": location})
                    response.cookies.extend(result.cookies)
                    return response
                raise HTTPError(500, "server functions can return data or redirect(), not other responses")
            return json_response({"ok": result})

        def live(request):
            if not app.dev:
                raise NotFound()

            def stream():
                yield f"retry: 400\ndata: {app.boot_id}\n\n"
                while True:
                    time.sleep(15)
                    yield ": ping\n\n"

            return Response(stream(), headers={"Cache-Control": "no-cache"}, content_type="text/event-stream")

        def static(request, path: str):
            base = (app.root / "static").resolve()
            target = (base / path).resolve()
            if not target.is_relative_to(base) or not target.is_file():
                raise NotFound()
            stat = target.stat()
            etag = f'"{int(stat.st_mtime)}-{stat.st_size}"'
            if request.headers.get("if-none-match") == etag:
                return Response(b"", 304, {"ETag": etag}, content_type=None)
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return Response(target.read_bytes(), 200, {"ETag": etag, "Cache-Control": "public, max-age=3600"}, content_type)

        self.router.add(Route("/_jongo/app.js", app_js, name="_jongo_js", kind="internal"))
        self.router.add(Route("/_jongo/app.css", app_css, name="_jongo_css", kind="internal"))
        self.router.add(Route("/_jongo/rpc/<path:function_id>", rpc, methods=("POST",), name="_jongo_rpc", kind="internal"))
        self.router.add(Route("/_jongo/live", live, name="_jongo_live", kind="internal"))
        self.router.add(Route(f"{self.static_url}/<path:path>", static, name="static", kind="internal"))

    def admin(self, path: str = "/admin", *, models=None, title: str = "Jongo admin"):
        """Mount the generated admin site. Sign in with a user that has ``is_admin=True``."""
        from .admin import Admin

        return Admin(self, path, models=models, title=title)

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
