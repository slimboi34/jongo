"""Test client: drive a Jongo app in-process without a server.

    client = app.test_client()
    assert client.get("/").status == 200
    todo = client.rpc(add_todo, "Buy milk")
"""
from __future__ import annotations

import http.cookies
import io
import json as _json
import secrets
import urllib.parse

from .errors import ServerError
from .http import CSRF_COOKIE, SESSION_COOKIE


class TestResponse:
    __test__ = False

    def __init__(self, status: str, headers: list, body: bytes):
        self.status = int(status.split()[0])
        self.header_list = headers
        self.headers = {k.lower(): v for k, v in headers}
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self):
        return _json.loads(self.body)

    def __repr__(self):
        return f"<TestResponse {self.status}>"


class TestClient:
    __test__ = False

    def __init__(self, app):
        self.app = app
        self.cookies: dict[str, str] = {}

    def request(self, method, path, *, data=None, json=None, headers=None, follow_redirects=False) -> TestResponse:
        method = method.upper()
        path, _, query = path.partition("?")
        headers = dict(headers or {})
        body = b""
        content_type = ""
        if json is not None:
            body = _json.dumps(json).encode()
            content_type = "application/json"
        elif data is not None:
            body = urllib.parse.urlencode(data, doseq=True).encode()
            content_type = "application/x-www-form-urlencoded"
        if method not in ("GET", "HEAD", "OPTIONS"):
            token = self.cookies.setdefault(CSRF_COOKIE, secrets.token_urlsafe(32))
            headers.setdefault("X-CSRF-Token", token)

        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path.encode("utf-8").decode("latin-1"),
            "QUERY_STRING": query,
            "SERVER_NAME": "testserver",
            "SERVER_PORT": "80",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "HTTP_HOST": "testserver",
            "REMOTE_ADDR": "127.0.0.1",
            "wsgi.input": io.BytesIO(body),
            "wsgi.url_scheme": "http",
            "wsgi.errors": io.StringIO(),
            "CONTENT_LENGTH": str(len(body)),
            "CONTENT_TYPE": content_type,
        }
        if self.cookies:
            environ["HTTP_COOKIE"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        for key, value in headers.items():
            name = key.upper().replace("-", "_")
            environ[name if name in ("CONTENT_TYPE", "CONTENT_LENGTH") else f"HTTP_{name}"] = value

        captured = {}

        def start_response(status, response_headers, exc_info=None):
            captured["status"], captured["headers"] = status, response_headers

        chunks = self.app(environ, start_response)
        body_out = b"".join(chunks)
        response = TestResponse(captured["status"], captured["headers"], body_out)
        for name, value in captured["headers"]:
            if name.lower() == "set-cookie":
                jar = http.cookies.SimpleCookie()
                jar.load(value)
                for key, morsel in jar.items():
                    if morsel["max-age"] == "0":
                        self.cookies.pop(key, None)
                    else:
                        self.cookies[key] = morsel.value
        if follow_redirects and response.status in (301, 302, 303, 307, 308):
            return self.get(response.headers["location"], follow_redirects=True)
        return response

    def get(self, path, **kwargs) -> TestResponse:
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs) -> TestResponse:
        return self.request("POST", path, **kwargs)

    def put(self, path, **kwargs) -> TestResponse:
        return self.request("PUT", path, **kwargs)

    def delete(self, path, **kwargs) -> TestResponse:
        return self.request("DELETE", path, **kwargs)

    def navigate(self, path) -> dict:
        """Fetch a page the way the browser router does (JSON tree)."""
        response = self.get(path, headers={"X-Jongo-Nav": "1"})
        assert response.headers.get("x-jongo-page") == "1", f"{path} is not a page ({response.status})"
        return response.json()

    def rpc(self, fn, *args, **kwargs):
        """Call a @server function over HTTP, like browser code does."""
        response = self.post(f"/_jongo/rpc/{fn.id}", json={"args": list(args), "kwargs": kwargs})
        data = response.json()
        if "error" in data:
            error = ServerError(data["error"]["message"])
            error.type = data["error"]["type"]
            error.status = response.status
            error.errors = data["error"].get("errors")
            raise error
        return data.get("redirect") and {"redirect": data["redirect"]} or data.get("ok")

    def login(self, user) -> None:
        from .auth import session_data_for

        self.cookies[SESSION_COOKIE] = self.app.signer.dumps(session_data_for(user))

    def logout(self) -> None:
        self.cookies.pop(SESSION_COOKIE, None)
