"""Requests, responses, cookies, signed sessions and CSRF protection."""
from __future__ import annotations

import base64
import email.parser
import email.policy
import hashlib
import hmac
import http.cookies
import json
import secrets
import time
import urllib.parse
from http import HTTPStatus

from .errors import HTTPError

CSRF_COOKIE = "jongo_csrf"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"
SESSION_COOKIE = "jongo_session"
_UNSET = object()


class QueryDict(dict):
    """``d["tag"]`` gives the last value; ``d.getlist("tag")`` gives all of them."""

    def __init__(self, pairs=()):
        self._lists: dict[str, list] = {}
        for key, value in pairs:
            self._lists.setdefault(key, []).append(value)
        super().__init__({k: v[-1] for k, v in self._lists.items()})

    def getlist(self, key) -> list:
        return list(self._lists.get(key, []))


class UploadedFile:
    def __init__(self, filename: str, content_type: str, data: bytes):
        self.filename = filename
        self.content_type = content_type
        self.data = data

    @property
    def size(self) -> int:
        return len(self.data)

    def save(self, path) -> None:
        with open(path, "wb") as fh:
            fh.write(self.data)

    def __repr__(self):
        return f"<UploadedFile {self.filename!r} {self.size} bytes>"


class Headers:
    """Case-insensitive, read-only view of the request headers."""

    def __init__(self, environ):
        self._items = {}
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                self._items[key[5:].replace("_", "-").lower()] = value
            elif key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
                self._items[key.replace("_", "-").lower()] = value

    def get(self, name: str, default=None):
        return self._items.get(name.lower(), default)

    def __getitem__(self, name):
        return self._items[name.lower()]

    def __contains__(self, name):
        return name.lower() in self._items

    def items(self):
        return self._items.items()


class Request:
    max_body_size = 16 * 1024 * 1024

    def __init__(self, environ, app=None):
        self.environ = environ
        self.app = app
        self.method = environ.get("REQUEST_METHOD", "GET").upper()
        raw_path = environ.get("PATH_INFO") or "/"
        try:
            self.path = raw_path.encode("latin-1").decode("utf-8")
        except UnicodeError:
            self.path = raw_path
        self.query_string = environ.get("QUERY_STRING", "")
        self.query = QueryDict(urllib.parse.parse_qsl(self.query_string, keep_blank_values=True))
        self.headers = Headers(environ)
        self.path_params: dict = {}
        self.session: Session = Session()
        self._user = _UNSET
        self.route = None
        self._body: bytes | None = None
        self._form: QueryDict | None = None
        self._files: dict | None = None
        self._cookies: dict | None = None
        self._csrf_token: str | None = None

    # -- basics ------------------------------------------------------------------

    @property
    def user(self):
        """The logged-in user (loaded from the session on first access), or None."""
        if self._user is _UNSET:
            self._user = self.app.load_user(self) if self.app is not None else None
        return self._user

    @user.setter
    def user(self, value):
        self._user = value

    @property
    def args(self) -> QueryDict:
        return self.query

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()

    @property
    def scheme(self) -> str:
        forwarded = self.headers.get("x-forwarded-proto")
        return forwarded or self.environ.get("wsgi.url_scheme", "http")

    @property
    def host(self) -> str:
        return self.headers.get("host") or self.environ.get("SERVER_NAME", "localhost")

    @property
    def url(self) -> str:
        qs = f"?{self.query_string}" if self.query_string else ""
        return f"{self.scheme}://{self.host}{self.path}{qs}"

    @property
    def full_path(self) -> str:
        return self.path + (f"?{self.query_string}" if self.query_string else "")

    @property
    def remote_addr(self) -> str:
        return self.environ.get("REMOTE_ADDR", "")

    @property
    def is_navigation(self) -> bool:
        """True when Jongo's browser router is fetching a page as JSON."""
        return self.headers.get("x-jongo-nav") == "1"

    # -- body ------------------------------------------------------------------------

    @property
    def body(self) -> bytes:
        if self._body is None:
            try:
                length = int(self.environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                length = 0
            if length > self.max_body_size:
                raise HTTPError(413, "Request body too large")
            stream = self.environ.get("wsgi.input")
            self._body = stream.read(length) if stream and length else b""
        return self._body

    @property
    def json(self):
        if not self.body:
            return None
        try:
            return json.loads(self.body)
        except ValueError:
            raise HTTPError(400, "Request body is not valid JSON") from None

    @property
    def form(self) -> QueryDict:
        if self._form is None:
            self._parse_form()
        return self._form

    @property
    def files(self) -> dict:
        if self._files is None:
            self._parse_form()
        return self._files

    def _parse_form(self):
        self._form, self._files = QueryDict(), {}
        if self.content_type == "application/x-www-form-urlencoded":
            pairs = urllib.parse.parse_qsl(self.body.decode("utf-8", "replace"), keep_blank_values=True)
            self._form = QueryDict(pairs)
        elif self.content_type == "multipart/form-data":
            header = f"Content-Type: {self.headers.get('content-type')}\r\n\r\n".encode()
            message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(header + self.body)
            pairs = []
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if not name:
                    continue
                data = part.get_payload(decode=True) or b""
                filename = part.get_filename()
                if filename:
                    self._files[name] = UploadedFile(filename, part.get_content_type(), data)
                else:
                    pairs.append((name, data.decode(part.get_content_charset() or "utf-8", "replace")))
            self._form = QueryDict(pairs)

    # -- cookies and CSRF ---------------------------------------------------------------

    @property
    def cookies(self) -> dict:
        if self._cookies is None:
            jar = http.cookies.SimpleCookie()
            try:
                jar.load(self.headers.get("cookie") or "")
            except http.cookies.CookieError:
                pass
            self._cookies = {k: m.value for k, m in jar.items()}
        return self._cookies

    @property
    def csrf_token(self) -> str:
        if self._csrf_token is None:
            existing = self.cookies.get(CSRF_COOKIE)
            self._csrf_token = existing if existing and len(existing) >= 32 else secrets.token_urlsafe(32)
        return self._csrf_token

    def csrf_ok(self) -> bool:
        cookie = self.cookies.get(CSRF_COOKIE)
        if not cookie:
            return False
        sent = self.headers.get(CSRF_HEADER)
        if sent is None and self.content_type in ("application/x-www-form-urlencoded", "multipart/form-data"):
            sent = self.form.get(CSRF_FIELD)
        if not sent or not hmac.compare_digest(str(sent), cookie):
            return False
        origin = self.headers.get("origin")
        if origin and origin != "null":
            return urllib.parse.urlsplit(origin).netloc == self.host
        return True

    def __repr__(self):
        return f"<Request {self.method} {self.full_path}>"


class Response:
    def __init__(self, body=b"", status: int = 200, headers=None, content_type="text/html; charset=utf-8"):
        self.status = status
        self.headers: dict[str, str] = {"Content-Type": content_type} if content_type else {}
        if headers:
            self.headers.update(headers)
        self.cookies: list[str] = []
        self.body = body

    def set_cookie(self, name, value, *, max_age=None, path="/", httponly=True, samesite="Lax", secure=False):
        morsel = http.cookies.SimpleCookie()
        morsel[name] = value
        cookie = morsel[name]
        cookie["path"] = path
        if max_age is not None:
            cookie["max-age"] = int(max_age)
        if httponly:
            cookie["httponly"] = True
        if secure:
            cookie["secure"] = True
        if samesite:
            cookie["samesite"] = samesite
        self.cookies.append(cookie.OutputString())

    def delete_cookie(self, name, path="/"):
        self.set_cookie(name, "", max_age=0, path=path)

    def iter_body(self):
        body = self.body
        if isinstance(body, str):
            return [body.encode("utf-8")]
        if isinstance(body, (bytes, bytearray)):
            return [bytes(body)]
        return (chunk.encode("utf-8") if isinstance(chunk, str) else chunk for chunk in body)

    def __call__(self, environ, start_response):
        try:
            phrase = HTTPStatus(self.status).phrase
        except ValueError:
            phrase = "Unknown"
        body = self.iter_body()
        headers = list(self.headers.items())
        if isinstance(body, list) and "Content-Length" not in self.headers:
            headers.append(("Content-Length", str(sum(len(b) for b in body))))
        headers.extend(("Set-Cookie", c) for c in self.cookies)
        start_response(f"{self.status} {phrase}", headers)
        if environ.get("REQUEST_METHOD") == "HEAD":
            return [b""]
        return body

    def __repr__(self):
        return f"<Response {self.status}>"


def json_response(data, status: int = 200, headers=None) -> Response:
    from .vdom import to_json_data

    text = json.dumps(to_json_data(data), separators=(",", ":"))
    return Response(text, status, headers, content_type="application/json")


def redirect(url: str, status: int = 303) -> Response:
    return Response(b"", status, {"Location": url}, content_type=None)


# ---------------------------------------------------------------------------
# Signed-cookie sessions


class Session(dict):
    """Session data stored in a signed cookie. Must be JSON-serialisable."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.modified = False

    def _touch(self):
        self.modified = True

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._touch()

    def __delitem__(self, key):
        super().__delitem__(key)
        self._touch()

    def pop(self, *args):
        self._touch()
        return super().pop(*args)

    def clear(self):
        self._touch()
        super().clear()

    def update(self, *args, **kwargs):
        self._touch()
        super().update(*args, **kwargs)

    def setdefault(self, key, default=None):
        if key not in self:
            self._touch()
        return super().setdefault(key, default)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Signer:
    def __init__(self, secret: str, salt: str = "jongo-session"):
        self.key = hashlib.sha256(f"{salt}:{secret}".encode()).digest()

    def _sign(self, message: str) -> str:
        return _b64(hmac.new(self.key, message.encode(), hashlib.sha256).digest())

    def dumps(self, data) -> str:
        payload = _b64(json.dumps(data, separators=(",", ":")).encode())
        message = f"{payload}.{int(time.time())}"
        return f"{message}.{self._sign(message)}"

    def loads(self, token: str, max_age: int | None = None):
        try:
            payload, stamp, signature = token.rsplit(".", 2)
        except ValueError:
            return None
        if not hmac.compare_digest(signature, self._sign(f"{payload}.{stamp}")):
            return None
        try:
            if max_age is not None and time.time() - int(stamp) > max_age:
                return None
            return json.loads(_unb64(payload))
        except ValueError:
            return None
