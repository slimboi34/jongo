"""Requests, responses, cookies, signed sessions and CSRF protection."""
from __future__ import annotations

import base64
import binascii
import codecs
import email.message
import email.parser
import email.policy
import hashlib
import hmac
import http.cookies
import ipaddress
import json
import quopri
import re
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

#: The environ key an in-process client (TestClient, the MCP ``jongo_get`` tool) sets to the
#: Host it sends, so the Host check doesn't refuse its made-up name. A network client can't set
#: environ keys: its headers arrive as ``HTTP_*``.
IN_PROCESS_HOST = "jongo.in_process_host"

#: The longest header block one part of a multipart form may have.
_MAX_PART_HEADERS = 16 * 1024
#: Charsets a multipart part may not name: their decoders are quadratic on crafted input.
_SLOW_CODECS = frozenset(("punycode", "idna"))

_LOCAL_NAMES = ("127.0.0.1", "localhost", "::1")


def _hostname(host: str) -> str:
    """``localhost:8000`` -> ``localhost``; ``[::1]:8000`` -> ``::1``.

    Anything that isn't ``name``, ``name:port`` or ``[v6]:port`` comes back unchanged (and
    so matches no allowed name): ``127.0.0.1:80@evil.example`` is not ``127.0.0.1``.
    """
    host = (host or "").strip().lower()
    if host.startswith("["):
        inside, sep, rest = host[1:].partition("]")
        return inside if sep and (not rest or (rest[0] == ":" and rest[1:].isdigit())) else host
    if host.count(":") == 1:
        name, port = host.split(":")
        return name if port.isdigit() or not port else host
    return host


def host_allowed(host: str, allowed=(), *, dev: bool = False) -> bool:
    """Whether a request's ``Host`` header names this app (see ``Jongo(allowed_hosts=...)``).

    ``allowed`` holds host names (``"example.com"``), ``".example.com"`` (or
    ``"*.example.com"``) for that domain and every subdomain, or ``"*"`` for anything. A port
    in an entry is ignored. With ``dev=True`` this machine's names are
    allowed too: ``localhost``, ``*.localhost``, and any IP address (a DNS-rebinding page
    always names its own domain, never an IP address).
    """
    name = _hostname(host).rstrip(".")
    if not name:
        return False
    for pattern in allowed:
        pattern = _hostname(str(pattern)).rstrip(".")
        if pattern.startswith("*."):
            pattern = pattern[1:]
        if pattern == "*":
            return True
        if pattern.startswith(".") and len(pattern) > 1:
            if name == pattern[1:] or name.endswith(pattern):
                return True
        elif pattern and name == pattern:
            return True
    if dev:
        if name in _LOCAL_NAMES or name.endswith(".localhost"):
            return True
        try:
            ipaddress.ip_address(name)
            return True
        except ValueError:
            pass
    return False


def parse_cookie_header(header: str) -> dict[str, str]:
    """Parse a Cookie header leniently.

    ``http.cookies.SimpleCookie`` gives up at the first cookie it considers invalid,
    and on ``localhost`` every dev server you've ever run shares one cookie jar, so
    one odd cookie from another app would hide ours.
    """
    cookies: dict[str, str] = {}
    for chunk in header.split(";"):
        name, sep, value = chunk.strip().partition("=")
        if not sep or not name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            # SimpleCookie quotes special values as "..." with backslash escapes; undo that.
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        # Cookie values are sent verbatim (set_cookie/SimpleCookie does not percent-encode),
        # so must NOT be unquoted here, or "a%20b" would corrupt to "a b". Last occurrence
        # wins, matching SimpleCookie and browser behaviour.
        cookies[name.strip()] = value
    return cookies


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
    #: The largest request body read, in bytes (413, ``J212``, beyond it).
    max_body_size = 16 * 1024 * 1024
    #: The most fields an ``application/x-www-form-urlencoded`` body may have (413, ``J212``).
    max_form_fields = 1000
    #: The most parts a ``multipart/form-data`` body may have (413, ``J212``).
    max_form_parts = 1000

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
        #: The server-side record of this sign-in (jongo.auth.LoginSession), once request.user is loaded.
        self.login_session = None
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
        # X-Forwarded-Proto is client-controlled; trusting it by default lets a client
        # strip the Secure cookie flag / spoof request.url. Opt in with Jongo(trust_proxy=True)
        # only when a trusted proxy sets the header.
        if getattr(self.app, "trust_proxy", False):
            forwarded = self.headers.get("x-forwarded-proto")
            if forwarded:
                return forwarded.split(",")[0].strip().lower()
        return self.environ.get("wsgi.url_scheme", "http")

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
    def client_ip(self) -> str:
        """The caller's address, for rate limits and the session list.

        ``REMOTE_ADDR`` unless the app was created with ``trust_proxy=True``, in which case
        it is the last entry of ``X-Forwarded-For``: the one the trusted proxy itself
        appended. Earlier entries are whatever the client chose to send, so they are never
        used. Without ``trust_proxy`` the header is ignored entirely.
        """
        if getattr(self.app, "trust_proxy", False):
            forwarded = self.headers.get("x-forwarded-for")
            if forwarded:
                last = forwarded.split(",")[-1].strip()
                if last:
                    return last[:64]
        return self.remote_addr

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
            if length < 0:
                # A negative Content-Length must never reach read(): read(-1) drains the
                # whole stream and defeats max_body_size (unauthenticated memory DoS).
                raise HTTPError(400, "Invalid Content-Length", code="J212")
            if length > self.max_body_size:
                raise HTTPError(413, "Request body too large", code="J212")
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
            raise HTTPError(400, "Request body is not valid JSON", code="J212") from None
        except RecursionError:  # json's C decoder recurses once per [ or {
            raise HTTPError(400, "Request body JSON is nested too deeply", code="J212") from None

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
        """Parse a form body. The cost is bounded: field and part counts are capped before parsing."""
        form, files = QueryDict(), {}
        if self.content_type == "application/x-www-form-urlencoded":
            try:
                pairs = urllib.parse.parse_qsl(self.body.decode("utf-8", "replace"), keep_blank_values=True,
                                               max_num_fields=self.max_form_fields)
            except ValueError:  # parse_qsl counts the separators before it parses anything
                raise HTTPError(413, f"The form has more than {self.max_form_fields} fields", code="J212",
                                hint="send fewer fields, or raise jongo.Request.max_form_fields") from None
            form = QueryDict(pairs)
        elif self.content_type == "multipart/form-data":
            form, files = self._parse_multipart()
        self._form, self._files = form, files

    def _parse_multipart(self) -> tuple[QueryDict, dict]:
        boundary = _multipart_boundary(self.headers.get("content-type") or "")
        if boundary is None:
            raise HTTPError(400, "Malformed multipart form: no valid boundary in the Content-Type header",
                            code="J212")
        body = self.body
        delimiter = b"--" + boundary
        # One delimiter per part plus the closing one. Counting is a single C-speed pass, so an
        # oversized form is refused before any per-part work is done.
        if body.count(delimiter) > self.max_form_parts + 1:
            raise HTTPError(413, f"The form has more than {self.max_form_parts} parts", code="J212",
                            hint="send fewer fields and files, or raise jongo.Request.max_form_parts")
        pairs, files = [], {}
        header_parser = email.parser.BytesHeaderParser(policy=email.policy.HTTP)
        for raw in _multipart_parts(body, delimiter):
            head, data = _split_part(raw)
            try:
                part = header_parser.parsebytes(head + b"\r\n\r\n")
                name = part.get_param("name", header="content-disposition")
                filename = part.get_filename()
                content_type = part.get_content_type()
                charset = part.get_content_charset()
                encoding = str(part.get("content-transfer-encoding", "")).strip().lower()
            except Exception:  # the stdlib header parser is lenient, but nothing it raises may become a 500
                raise HTTPError(400, "Malformed multipart form: a part's headers can't be read",
                                code="J212") from None
            if not name:
                continue
            data = _transfer_decode(data, encoding)
            if filename:
                files[name] = UploadedFile(filename, content_type, data)
            else:
                pairs.append((name, _decode_text(data, charset)))
        return QueryDict(pairs), files

    # -- cookies and CSRF ---------------------------------------------------------------

    @property
    def cookies(self) -> dict:
        if self._cookies is None:
            self._cookies = parse_cookie_header(self.headers.get("cookie") or "")
        return self._cookies

    @property
    def _signer(self):
        return getattr(self.app, "signer", None)

    @property
    def csrf_token(self) -> str:
        if self._csrf_token is None:
            existing = self.cookies.get(CSRF_COOKIE)
            signer = self._signer
            if signer is not None:
                # Sign the token with the app secret so a planted/forged cookie is rejected.
                self._csrf_token = existing if existing and signer.loads(existing) is not None \
                    else signer.dumps(secrets.token_urlsafe(16))
            else:
                self._csrf_token = existing if existing and len(existing) >= 32 else secrets.token_urlsafe(32)
        return self._csrf_token

    def rotate_csrf(self) -> str:
        """Issue this browser a fresh CSRF token; the response carries it as a new cookie.

        Call it when the user's privileges change (signing in or out), so a token someone
        planted or saw before stops being the one the page uses. Returns the new token, which
        is also ``request.csrf_token`` from now on (pages rendered later in this request
        embed it).
        """
        signer = self._signer
        self._csrf_token = signer.dumps(secrets.token_urlsafe(16)) if signer is not None \
            else secrets.token_urlsafe(32)
        return self._csrf_token

    def _form_may_carry_csrf(self) -> bool:
        # Server calls (/_jongo/rpc/) and the other internal endpoints take the token in the
        # X-CSRF-Token header only: parsing a form body there would let anyone make the server
        # do that work before the CSRF verdict, on every app.
        if self.path.startswith("/_jongo/") or getattr(self.route, "kind", None) == "internal":
            return False
        return self.content_type in ("application/x-www-form-urlencoded", "multipart/form-data")

    def csrf_ok(self) -> bool:
        cookie = self.cookies.get(CSRF_COOKIE)
        if not cookie or not cookie.isascii():
            return False
        signer = self._signer
        if signer is not None and signer.loads(cookie) is None:
            return False  # the token wasn't minted (signed) by this app; checked before any body is read
        sent = self.headers.get(CSRF_HEADER)
        if sent is None and self._form_may_carry_csrf():
            sent = self.form.get(CSRF_FIELD)
        # Every token Jongo mints is ASCII; compare_digest raises TypeError on non-ASCII str.
        if not sent or not isinstance(sent, str) or not sent.isascii() or not hmac.compare_digest(sent, cookie):
            return False
        origin = self.headers.get("origin")
        if origin is not None:
            # A present Origin must match; "null" (sandboxed iframes, some clients) is untrusted.
            return origin != "null" and _same_site_origin(origin, self.scheme, self.host)
        return True

    def __repr__(self):
        return f"<Request {self.method} {self.full_path}>"


def _same_site_origin(origin: str, scheme: str, host: str) -> bool:
    """Whether an ``Origin`` header names this site: the same host and the same scheme.

    One mismatch is allowed: an ``https`` page posting to an app that sees ``http``. That
    is a proxy terminating TLS without ``Jongo(trust_proxy=True)``, and still this site.
    The reverse, an ``http://`` page posting to an ``https`` app, is what an on-path
    attacker who injects into the plain-HTTP site would send, so it is refused.
    """
    try:
        parts = urllib.parse.urlsplit(origin)
    except ValueError:  # e.g. an unclosed IPv6 bracket
        return False
    if parts.netloc.lower() != (host or "").lower():
        return False
    theirs, ours = parts.scheme.lower(), (scheme or "http").lower()
    return theirs == ours or (theirs == "https" and ours == "http")


def _multipart_boundary(content_type: str) -> bytes | None:
    """The boundary parameter of a multipart Content-Type, if it is a usable one."""
    try:
        message = email.message.Message()
        message["Content-Type"] = content_type
        boundary = message.get_param("boundary")
    except Exception:
        return None
    if not isinstance(boundary, str) or not 0 < len(boundary) <= 200 or not boundary.isascii() \
            or any(c in boundary for c in "\r\n"):
        return None
    return boundary.encode("ascii")


_DELIMITER_END = re.compile(rb"[ \t]*(?:\r\n|\n|$)")


def _multipart_parts(body: bytes, delimiter: bytes) -> list[bytes]:
    """Split a multipart body into its raw parts (each starts just after its delimiter line's text).

    A delimiter counts only at the start of the body or of a line, and followed by the end
    of that line (or ``--`` for the closing one); elsewhere the bytes are part content.
    Everything here is ``bytes.split``/``join``, so the cost is linear in the body size
    whatever the body holds.
    """
    pieces = body.split(delimiter)
    parts: list[bytes] = []
    current: list[bytes] | None = None  # the chunks of the part being collected
    for index in range(1, len(pieces)):
        before, piece = pieces[index - 1], pieces[index]
        at_line_start = before.endswith(b"\n") or (index == 1 and before == b"")
        # "--boundary--" closes the body; so does a bare delimiter at its very end (a client that
        # left off the final "--"), rather than starting an empty part.
        closing = piece.startswith(b"--") or (index == len(pieces) - 1 and not piece.strip())
        if at_line_start and (closing or _DELIMITER_END.match(piece)):
            if current is not None:
                last = current[-1]  # == before: drop the line break that belongs to the delimiter
                current[-1] = last[:-2] if last.endswith(b"\r\n") else last[:-1]
                parts.append(b"".join(current))
            if closing:
                return parts
            current = [piece]
        elif current is not None:
            current += (delimiter, piece)
        # else: still in the preamble, which is ignored
    if current is not None:  # no closing delimiter (a truncated body): keep what arrived
        parts.append(b"".join(current))
    return parts


def _split_part(raw: bytes) -> tuple[bytes, bytes]:
    """``(header block, content)`` of one raw part, which starts with the rest of its delimiter line."""
    line_end = _DELIMITER_END.match(raw)
    raw = raw[line_end.end():] if line_end else raw
    if raw.startswith(b"\r\n"):  # no headers at all
        return b"", raw[2:]
    if raw.startswith(b"\n"):
        return b"", raw[1:]
    crlf = raw.find(b"\r\n\r\n", 0, _MAX_PART_HEADERS)
    lf = raw.find(b"\n\n", 0, _MAX_PART_HEADERS)
    if lf != -1 and (crlf == -1 or lf < crlf):
        return raw[:lf], raw[lf + 2:]
    if crlf != -1:
        return raw[:crlf], raw[crlf + 4:]
    raise HTTPError(400, "Malformed multipart form: a part's headers don't end (or are over "
                         f"{_MAX_PART_HEADERS // 1024} KB)", code="J212")


def _transfer_decode(data: bytes, encoding: str) -> bytes:
    """Undo a part's Content-Transfer-Encoding (browsers never send one; RFC 7578 forbids it)."""
    if encoding == "base64":
        try:
            return base64.b64decode(data)
        except (binascii.Error, ValueError):
            return data
    if encoding == "quoted-printable":
        return quopri.decodestring(data)
    return data


def _decode_text(data: bytes, charset: str | None) -> str:
    """A text field's value, in its declared charset when that is a usable text encoding, else UTF-8."""
    if charset:
        try:
            info = codecs.lookup(charset)
            if info.name not in _SLOW_CODECS and getattr(info, "_is_text_encoding", True):
                return data.decode(info.name, "replace")
        except (LookupError, ValueError, TypeError):  # an unknown or bytes-to-bytes codec
            pass
    return data.decode("utf-8", "replace")


class Response:
    def __init__(self, body=b"", status: int = 200, headers=None, content_type="text/html; charset=utf-8"):
        self.status = status
        self.headers: dict[str, str] = {"Content-Type": content_type} if content_type else {}
        if headers:
            self.headers.update(headers)
        self.cookies: list[str] = []
        self.body = body

    def set_cookie(self, name, value, *, max_age=None, path="/", httponly=True, samesite="Lax", secure=False):
        def _ctrl(s):
            return any(ord(c) < 0x20 or ord(c) == 0x7f for c in str(s))

        if _ctrl(value):
            raise ValueError(f"cookie {name!r} value contains control characters; encode it first")
        if _ctrl(name) or _ctrl(path) or (samesite and _ctrl(samesite)):
            raise ValueError(f"cookie {name!r} name/path/samesite contains control characters")
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
        output = cookie.OutputString()
        if not output.isascii():
            # SimpleCookie escapes Latin-1 but passes other characters through, and the server
            # would then fail to send the headers after the status line. Refuse it here instead.
            raise ValueError(f"cookie {name!r} contains characters a header can't carry; encode the value "
                             "first (e.g. urllib.parse.quote)")
        self.cookies.append(output)

    def delete_cookie(self, name, path="/"):
        self.set_cookie(name, "", max_age=0, path=path)

    def iter_body(self):
        body = self.body
        if isinstance(body, str):
            return [body.encode("utf-8")]
        if isinstance(body, (bytes, bytearray)):
            return [bytes(body)]
        return _encoded_chunks(body)

    def wsgi_headers(self) -> list[tuple[str, str]]:
        """The headers as the WSGI server will send them (without Content-Length).

        CR and LF are removed, so a value such as a redirect target can't split the response.
        A non-ASCII ``Location`` is percent-encoded. Any other value a server can't send
        (anything outside Latin-1) raises ``ValueError`` naming the header, instead of the
        server failing after it has sent the status line.
        """
        headers = [(k, _header_value(k, v)) for k, v in self.headers.items()]
        headers.extend(("Set-Cookie", _header_value("Set-Cookie", c)) for c in self.cookies)
        return headers

    def __call__(self, environ, start_response):
        try:
            phrase = HTTPStatus(self.status).phrase
        except ValueError:
            phrase = "Unknown"
        headers = self.wsgi_headers()
        head = environ.get("REQUEST_METHOD") == "HEAD"
        if head and not isinstance(self.body, (str, bytes, bytearray)):
            # A streaming body is never read for HEAD: close it now, so its cleanup doesn't
            # wait for garbage collection, and send the headers alone.
            close = getattr(self.body, "close", None)
            if close is not None:
                close()
            start_response(f"{self.status} {phrase}", headers)
            return [b""]
        body = self.iter_body()
        if isinstance(body, list) and "Content-Length" not in self.headers:
            headers.insert(len(self.headers), ("Content-Length", str(sum(len(b) for b in body))))
        start_response(f"{self.status} {phrase}", headers)
        return [b""] if head else body

    def __repr__(self):
        return f"<Response {self.status}>"


def _encoded_chunks(body):
    """A streaming body's chunks as bytes; closing this closes the body too (the WSGI server
    closes what it was given when the client goes, and the body's cleanup must run then)."""
    iterator = iter(body)
    try:
        for chunk in iterator:
            yield chunk.encode("utf-8") if isinstance(chunk, str) else chunk
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()


#: What redirect() leaves alone: URL syntax, and existing %-escapes.
_URL_SAFE = "/:?#[]@!$&'()*+,;=%~"


def _quote_url(url: str) -> str:
    return urllib.parse.quote(url, safe=_URL_SAFE, errors="replace")


def _header_value(name: str, value) -> str:
    text = str(value).replace("\r", "").replace("\n", "")
    if name.lower() == "location" and not text.isascii():
        return _quote_url(text)  # browsers read Location as UTF-8 percent-escapes, never raw bytes
    try:
        text.encode("latin-1")
    except UnicodeEncodeError:
        raise ValueError(
            f"response header {name}: {text[:80]!r} has characters an HTTP header can't carry (only "
            "Latin-1, and ASCII to be safe); percent-encode the value first (urllib.parse.quote)"
        ) from None
    return text


def json_response(data, status: int = 200, headers=None) -> Response:
    from .vdom import to_json_data

    text = json.dumps(to_json_data(data), separators=(",", ":"))
    return Response(text, status, headers, content_type="application/json")


def is_local_url(url) -> bool:
    """True if ``url`` is a path on this site, safe to redirect to after login.

    Browsers drop tab/CR/LF from URLs and treat ``\\`` like ``/``, so ``/\\evil.com`` and
    ``/<TAB>/evil.com`` are protocol-relative URLs to another host even though they start
    with a single ``/``. Those (and anything with a scheme) are rejected.
    """
    if not isinstance(url, str):
        return False
    cleaned = url.replace("\t", "").replace("\r", "").replace("\n", "").replace("\\", "/")
    return cleaned.startswith("/") and not cleaned.startswith("//")


def redirect(url: str, status: int = 303) -> Response:
    """A redirect response (303 See Other by default) to ``url``.

    Characters a ``Location`` header can't carry (non-ASCII, spaces, control characters)
    are percent-encoded; URL syntax and existing ``%xx`` escapes are kept.

    Never redirect to a URL taken from the request (a ``?next=`` parameter, a form field)
    without checking it: ``redirect(nxt if is_local_url(nxt) else "/")``. Otherwise
    anyone can send your users to a site of their choosing from a link to yours. A
    ``@server`` function may only redirect to a relative URL or an ``http(s)`` one: any
    other scheme (``javascript:``, ``data:``) is refused with ``J408``.
    """
    return Response(b"", status, {"Location": _quote_url(str(url))}, content_type=None)


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
        if not isinstance(token, str) or not token.isascii():
            return None  # every token dumps() makes is ASCII: this one is forged or mangled
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
