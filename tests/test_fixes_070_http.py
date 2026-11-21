"""0.7.0 fixes to the HTTP core, live streams, MCP and the monitor (from the 0.6.0 stress test).

Security first: form parsing is bounded before the CSRF verdict, the error redactor is
linear, HEAD on a live stream leaves nothing subscribed, a server call can't redirect the
browser to javascript:, a revoked session's live stream ends, the dev server refuses a
rebound Host, the CSRF Origin check compares the scheme, and pages refuse foreign framing.
Then the crashes (non-ASCII cookies, odd charsets, deep JSON, NUL bytes) and the
correctness fixes (headers a server can't send, bool/list query parameters, app.root).
"""
from __future__ import annotations

import importlib.util
import io
import json
import logging
import sys
import time
import typing
from pathlib import Path

import pytest

import jongo
from conftest import use_test_database
from jongo import HTTPError, Jongo, Response, auth, db, ratelimit, redirect, rpc, server
from jongo.html import div
from jongo.http import CSRF_COOKIE, Request, Signer, host_allowed
from jongo.live import broadcast, event_stream, hub
from jongo.mcp import McpServer, serve_stdio
from jongo.monitor import redact
from jongo.testing import TestClient

HERE = Path(__file__).parent
live = importlib.import_module("jongo.live")  # `jongo.live` the attribute is the component hook


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    saved_models, saved_functions = dict(db.models_registry), dict(rpc.SERVER_FUNCTIONS)
    db.models_registry.clear()
    db.models_registry.update({"User": auth.User, "LoginSession": auth.LoginSession})
    use_test_database()
    monkeypatch.setattr(auth, "ITERATIONS", 1000)
    monkeypatch.delenv("JONGO_ENV", raising=False)
    monkeypatch.delenv("JONGO_MCP_TOKEN", raising=False)
    ratelimit.limiter.reset()
    hub.clear()
    yield
    hub.clear()
    ratelimit.limiter.reset()
    db.close_connections()
    db.models_registry.clear()
    db.models_registry.update(saved_models)
    rpc.SERVER_FUNCTIONS.clear()
    rpc.SERVER_FUNCTIONS.update(saved_functions)


def make_app(**options):
    options.setdefault("dev", False)
    app = Jongo("fixes_070_http", root=HERE, secret_key="k" * 40, **options)

    @app.post("/form")
    def form(request):
        return {"fields": len(request.form), "files": sorted(request.files)}

    @app.post("/open", csrf=False)
    def open_form(request):
        files = {name: [f.filename, f.content_type, f.data.decode("latin-1")] for name, f in request.files.items()}
        return {"form": {k: request.form.getlist(k) for k in request.form}, "files": files}

    @app.page("/")
    def home():
        return div("home")

    return app


def call(app, environ):
    """Run one raw WSGI request: (status, {header: value}, body)."""
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"], captured["headers"] = int(status.split()[0]), headers

    body = b"".join(app(environ, start_response))
    return captured["status"], {k.lower(): v for k, v in captured["headers"]}, body


def multipart(*parts, boundary="XyZ"):
    """parts: (name, value) or (name, filename, content_type, data)."""
    out = b""
    for part in parts:
        if len(part) == 2:
            name, value = part
            out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
                    + (value if isinstance(value, bytes) else value.encode()) + b"\r\n")
        else:
            name, filename, ctype, data = part
            out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                    f"Content-Type: {ctype}\r\n\r\n").encode() + data + b"\r\n"
    return out + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


# ===================================================================== form parsing DoS ==


def test_twenty_thousand_empty_parts_are_refused_quickly():
    app = make_app(dev=True)
    part = b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\n\r\n'
    body = part * 20_000 + b"--b--\r\n"
    started = time.perf_counter()
    response = TestClient(app).post("/form", data=body, headers={"Content-Type": "multipart/form-data; boundary=b"})
    assert time.perf_counter() - started < 1.0
    assert response.status == 413 and "J212" in response.text


def test_too_many_urlencoded_fields_are_refused_before_parsing():
    app = make_app()
    client = TestClient(app)
    ok = client.post("/form", data="&".join(f"f{i}=1" for i in range(Request.max_form_fields)),
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert ok.status == 200 and ok.json()["fields"] == Request.max_form_fields
    started = time.perf_counter()
    refused = client.post("/form", data=b"a&" * 2_000_000, headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert time.perf_counter() - started < 1.0
    assert refused.status == 413


def test_the_caps_are_class_attributes(monkeypatch):
    app = make_app()
    monkeypatch.setattr(Request, "max_form_parts", 2)
    body, ctype = multipart(("a", "1"), ("b", "2"), ("c", "3"))
    assert TestClient(app).post("/form", data=body, headers={"Content-Type": ctype}).status == 413
    monkeypatch.setattr(Request, "max_form_parts", 3)
    assert TestClient(app).post("/form", data=body, headers={"Content-Type": ctype}).json()["fields"] == 3


def test_server_calls_never_parse_a_form_body_for_the_csrf_token(monkeypatch):
    """POST /_jongo/rpc/<anything> exists on every app: the body must not be parsed before the verdict."""
    app = make_app()
    parsed = []
    monkeypatch.setattr(Request, "_parse_form", lambda self: parsed.append(self.path))
    client = TestClient(app)
    client.get("/")
    token = client.cookies[CSRF_COOKIE]
    body, ctype = multipart(("csrf_token", token))
    environ = client._environ("POST", "/_jongo/rpc/no.such.fn", data=body, headers={"Content-Type": ctype})
    del environ["HTTP_X_CSRF_TOKEN"]
    status, _, _ = call(app, environ)
    assert status == 403 and parsed == []


def test_a_forged_csrf_cookie_is_refused_before_the_body_is_read(monkeypatch):
    app = make_app()
    parsed = []
    monkeypatch.setattr(Request, "_parse_form", lambda self: parsed.append(self.path))
    client = TestClient(app)
    environ = client._environ("POST", "/form", data={"csrf_token": "x" * 40})
    del environ["HTTP_X_CSRF_TOKEN"]
    environ["HTTP_COOKIE"] = f"{CSRF_COOKIE}={'x' * 40}"
    assert call(app, environ)[0] == 403 and parsed == []


def test_a_classic_form_still_carries_its_token_in_a_field():
    app = make_app()
    client = TestClient(app)
    client.get("/")
    token = client.cookies[CSRF_COOKIE]
    for body, ctype in [(f"csrf_token={token}&a=1".encode(), "application/x-www-form-urlencoded"),
                        multipart(("csrf_token", token), ("a", "1"))]:
        environ = client._environ("POST", "/form", data=body, headers={"Content-Type": ctype})
        del environ["HTTP_X_CSRF_TOKEN"]
        status, _, out = call(app, environ)
        assert status == 200 and json.loads(out)["fields"] == 2


def test_multipart_fields_and_files_parse_as_before():
    app = make_app()
    body, ctype = multipart(("title", "Hello"), ("tag", "a"), ("tag", "b"), ("naïve", "€ 5"),
                            ("avatar", "résumé.png", "image/png", b"\x89PNG\r\n\r\n--XyZ-not-a-boundary\r\n"))
    out = TestClient(app).post("/open", data=body, headers={"Content-Type": ctype}).json()
    assert out["form"] == {"title": ["Hello"], "tag": ["a", "b"], "naïve": ["€ 5"]}
    assert out["files"] == {"avatar": ["résumé.png", "image/png", "\x89PNG\r\n\r\n--XyZ-not-a-boundary\r\n"]}


def test_multipart_edge_cases():
    app = make_app()
    client = TestClient(app)
    # LF-only line endings, a preamble, transport padding and an epilogue
    body = b'preamble\n--q  \nContent-Disposition: form-data; name="a"\n\nline1\nline2\n--q--\nepilogue'
    assert client.post("/open", data=body, headers={"Content-Type": 'multipart/form-data; boundary="q"'}
                       ).json()["form"] == {"a": ["line1\nline2"]}
    # the boundary text inside a value, not at the start of a line, is content
    body, ctype = multipart(("a", "x--XyZ y"))
    assert client.post("/open", data=body, headers={"Content-Type": ctype}).json()["form"] == {"a": ["x--XyZ y"]}
    # no boundary, and headers that never end, are malformed (400), not a server error
    assert client.post("/open", data=b"x", headers={"Content-Type": "multipart/form-data"}).status == 400
    endless = b'--b\r\nContent-Disposition: form-data; name="a"' + b"; x=y" * 5000
    assert client.post("/open", data=endless, headers={"Content-Type": "multipart/form-data; boundary=b"}).status == 400


def test_one_huge_part_of_newlines_parses_in_linear_time():
    """The stdlib email parser spent ~1.5 s on 8 MB of newlines in a single part."""
    app = make_app()
    body, ctype = multipart(("a", b"\n" * 8_000_000))
    started = time.perf_counter()
    out = TestClient(app).post("/open", data=body, headers={"Content-Type": ctype})
    assert time.perf_counter() - started < 1.0
    assert out.status == 200 and len(out.json()["form"]["a"][0]) == 8_000_000


# ============================================================================ redaction ==


def test_redact_is_linear_on_hostile_messages():
    for text in ("://:" * 250_000, "://a:" * 200_000, "://" * 400_000):
        started = time.perf_counter()
        redact(text)
        assert time.perf_counter() - started < 0.05


def test_redact_still_masks_credentials():
    assert redact("connect postgres://admin:hunter2@db:5432/x") == "connect postgres://admin:***@db:5432/x"
    assert "hunter2" not in redact("x" * 1190 + " postgres://admin:hunter2secret@db", limit=2000)
    assert "p" * 10 not in redact("postgres://admin:" + "p" * 5000 + "@db")  # cut before its "@": masked
    assert redact("http://localhost:8000/x") == "http://localhost:8000/x"
    assert redact("a" * 400).endswith("…") and len(redact("a" * 400)) == 300


# ====================================================================== live streams ==


def live_app(**options):
    app = make_app(**options)

    @app.channel("lobby")
    def lobby(request):
        return True

    @app.channel("members")
    def members(request):
        return request.user is not None

    return app


def test_head_on_a_live_stream_leaves_nothing_subscribed():
    app = live_app()
    client = TestClient(app)
    for _ in range(200):
        response = client.request("HEAD", "/_jongo/live?channels=lobby")
        assert response.status == 200 and response.headers["content-type"].startswith("text/event-stream")
    assert hub.subscriber_count() == 0
    assert client.request("HEAD", "/_jongo/live?channels=secret").status == 403, "HEAD is still authorised"


def test_mcp_jongo_get_of_a_stream_leaves_nothing_subscribed():
    app = live_app()
    result = McpServer(app, tools=None).call("jongo_get", {"path": "/_jongo/live?channels=lobby"})
    assert result["structuredContent"]["streaming"] is True
    assert hub.subscriber_count() == 0


def test_a_stream_subscribes_when_it_starts_and_unsubscribes_when_closed():
    app = live_app()
    response, chunks = TestClient(app).stream("/_jongo/live?channels=lobby")
    assert response.status == 200 and hub.subscriber_count("lobby") == 1
    assert next(chunks) == b": subscribed\n\n"
    assert broadcast("lobby", "hi") == 1
    assert b"hi" in next(chunks)
    chunks.close()
    assert hub.subscriber_count() == 0


def test_head_closes_any_streaming_body():
    app = make_app()

    class Body:
        closed = False

        def __iter__(self):
            return iter([b"never sent"])

        def close(self):
            Body.closed = True

    @app.get("/download")
    def download():
        return Response(Body(), content_type="text/plain")

    response = TestClient(app).request("HEAD", "/download")
    assert response.status == 200 and response.body == b"" and Body.closed


def test_the_process_caps_live_connections(monkeypatch):
    app = live_app()
    monkeypatch.setattr(live, "MAX_SUBSCRIBERS", 1)
    _, first = TestClient(app).stream("/_jongo/live?channels=lobby")
    try:
        refused = TestClient(app).get("/_jongo/live?channels=lobby")
        assert refused.status == 503 and refused.headers["retry-after"]
        assert refused.json()["error"]["code"] == "J410"
    finally:
        first.close()


def test_the_channel_cap_is_coded_and_readable_by_the_browser():
    app = live_app()
    channels = ",".join(f"c{i}" for i in range(live.MAX_CHANNELS + 1))
    refused = TestClient(app).get(f"/_jongo/live?channels={channels}")
    assert refused.status == 403 and refused.headers["content-type"] == "application/json"
    error = refused.json()["error"]
    assert error["code"] == "J210"
    assert f"asked for {live.MAX_CHANNELS + 1}" in error["message"] and "MAX_CHANNELS" in error["hint"]


# ------------------------------------------------------------------ revoked sessions ---


def signed_in_stream(app):
    db.migrate()
    ann = auth.User.create_user("ann", "pw")
    client = TestClient(app)
    client.login(ann)
    response, chunks = client.stream("/_jongo/live?channels=members")
    assert response.status == 200 and next(chunks) == b": subscribed\n\n"
    return ann, chunks


def test_a_revoked_session_stops_receiving(monkeypatch):
    monkeypatch.setattr(live, "SESSION_CHECK_SECONDS", 0)
    app = live_app()
    ann, chunks = signed_in_stream(app)
    assert broadcast("members", "before") == 1 and b"before" in next(chunks)
    (session,) = auth.sessions(ann)
    auth.revoke_session(session)
    broadcast("members", "secret after sign-out")
    rest = b"".join(chunks)
    assert b"secret" not in rest and rest == b": signed out\n\n"
    assert hub.subscriber_count() == 0


def test_the_session_check_is_cached_between_messages(monkeypatch):
    monkeypatch.setattr(live, "SESSION_CHECK_SECONDS", 3600)
    app = live_app()
    ann, chunks = signed_in_stream(app)
    lookups = []
    original = app.load_user
    monkeypatch.setattr(app, "load_user", lambda request: lookups.append(1) or original(request))
    for i in range(5):
        broadcast("members", f"m{i}")
        assert f"m{i}".encode() in next(chunks)
    assert lookups == [], "no database lookup per message"
    chunks.close()


def test_the_hub_can_end_a_sessions_streams_at_once(monkeypatch):
    monkeypatch.setattr(live, "SESSION_CHECK_SECONDS", 3600)
    app = live_app()
    ann, chunks = signed_in_stream(app)
    (session,) = auth.sessions(ann)
    auth.revoke_session(session)
    assert hub.drop_session(session) == 1
    assert broadcast("members", "secret") == 0
    assert b"".join(chunks) == b": signed out\n\n"
    bob = auth.User.create_user("bob", "pw")
    client = TestClient(app)
    client.login(bob)
    _, bob_chunks = client.stream("/_jongo/live?channels=members")
    assert hub.drop_user(bob) == 1 and hub.subscriber_count() == 0
    bob_chunks.close()


def test_an_anonymous_stream_has_nothing_to_revoke():
    app = live_app()
    _, chunks = TestClient(app).stream("/_jongo/live?channels=lobby")
    (subscriber,) = hub._subscribers
    assert subscriber.login_session is None and subscriber.user is None
    chunks.close()


def test_the_check_also_runs_on_keepalives(monkeypatch):
    monkeypatch.setattr(live, "SESSION_CHECK_SECONDS", 0)
    frames = event_stream(hub.subscribe(["x"]), ping_seconds=0.01, alive=lambda: False)
    assert next(frames) == ": subscribed\n\n"
    assert list(frames) == [": signed out\n\n"]

    def broken():
        raise RuntimeError("database is down")

    frames = event_stream(hub.subscribe(["x"]), ping_seconds=0.01, alive=broken)
    next(frames)
    assert list(frames) == [": signed out\n\n"], "a check that can't run fails closed"


# ======================================================================= rpc redirects ==


@pytest.mark.parametrize("target", ["javascript:alert(1)", " JavaScript:alert(1)", "java\tscript:alert(1)",
                                    "\x01javascript:alert(1)", "JAVASCRIPT:alert(1)",
                                    "data:text/html,<script>alert(1)</script>", "vbscript:msgbox", "file:///etc/passwd"])
@pytest.mark.parametrize("build", ["redirect", "raw"])
def test_a_server_call_cannot_redirect_to_a_script_url(target, build):
    app = make_app(dev=True)

    @server
    def finish(request, to: str):
        return redirect(to) if build == "redirect" else Response(b"", 303, {"Location": to}, content_type=None)

    response = TestClient(app).post(f"/_jongo/rpc/{finish.id}", json={"args": [target]})
    data = response.json()
    if "redirect" in data:
        # redirect() percent-encodes a space or a tab: "%20javascript:" and "java%09script:" have no
        # scheme, so the browser reads them as paths on this site. No script URL gets through.
        assert build == "redirect" and "%" in data["redirect"].split(":", 1)[0], data
        return
    assert response.status == 500
    assert data["error"]["code"] == "J408" and "is_local_url" in data["error"]["hint"]


@pytest.mark.parametrize("target", ["/next", "/a:b", "?page=2", "#top", "https://example.com/x", "//cdn.example/x"])
def test_a_server_call_may_redirect_to_relative_and_http_urls(target):
    app = make_app()

    @server
    def finish(request, to: str):
        return redirect(to)

    assert TestClient(app).rpc(finish, target) == {"redirect": target}


def test_is_local_url_is_exported():
    assert jongo.is_local_url is jongo.http.is_local_url and "is_local_url" in jongo.__all__
    assert jongo.is_local_url("/next") and not jongo.is_local_url("//evil.example")


# ========================================================================== Host check ==


def test_dev_refuses_a_rebound_host():
    app = make_app(dev=True)

    @server
    def whoami(request):
        return None

    client = TestClient(app)
    assert client.get("/").status == 200, "TestClient's own host still works"
    for host in ("attacker.example:8000", "127.0.0.1.attacker.example", "127.0.0.1:80@attacker.example"):
        page = client.get("/", headers={"Host": host})
        assert page.status == 400 and "allowed_hosts" in page.text
        call_ = client.post(f"/_jongo/rpc/{whoami.id}", json={}, headers={"Host": host, "Origin": f"http://{host}"})
        assert call_.status == 400 and call_.json()["error"]["code"] == "J214"
    for host in ("localhost:8000", "127.0.0.1:8000", "[::1]:8000", "shop.localhost", "192.168.1.5:8000", "LOCALHOST"):
        assert client.get("/", headers={"Host": host}).status == 200, host


def test_allowed_hosts():
    dev = make_app(dev=True, allowed_hosts=[".shop.test", "other.test:8000"])
    client = TestClient(dev)
    for host, status in [("shop.test", 200), ("a.shop.test:8000", 200), ("other.test", 200),
                         ("evilshop.test", 400), ("localhost", 200)]:
        assert client.get("/", headers={"Host": host}).status == status, host
    production = make_app()
    assert TestClient(production).get("/", headers={"Host": "anything.example"}).status == 200, \
        "production without allowed_hosts doesn't check"
    strict = make_app(allowed_hosts="shop.example")
    assert TestClient(strict).get("/", headers={"Host": "shop.example"}).status == 200
    assert TestClient(strict).get("/", headers={"Host": "localhost"}).status == 400
    assert TestClient(make_app(allowed_hosts=["*"])).get("/", headers={"Host": "x.example"}).status == 200


def test_host_allowed_parsing():
    assert host_allowed("[::1]:8000", dev=True) and host_allowed("localhost.", dev=True)
    assert not host_allowed("localhost:80@evil.example", dev=True)
    assert not host_allowed("", dev=True) and not host_allowed("evil.example", dev=True)
    assert host_allowed("Shop.Example:443", ["shop.example"])


def test_mcp_get_still_works_in_dev():
    app = make_app(dev=True)
    result = McpServer(app, tools=None).call("jongo_get", {"path": "/"})
    assert result["structuredContent"]["status"] == 200


# ============================================================= headers a server can't send ==


def test_redirect_percent_encodes_what_a_header_cannot_carry():
    assert redirect("/€").headers["Location"] == "/%E2%82%AC"
    assert redirect("/café?q=a b").headers["Location"] == "/caf%C3%A9?q=a%20b"
    assert redirect("/already%20encoded?x=1&y=2#f").headers["Location"] == "/already%20encoded?x=1&y=2#f"
    response = Response(b"", 302, {"Location": "/ünï"})
    assert dict(response.wsgi_headers())["Location"] == "/%C3%BCn%C3%AF"


def test_an_unsendable_header_is_a_clear_500_not_a_truncated_response():
    app = make_app(dev=True)

    @app.get("/named")
    def named():
        return Response("hi", headers={"X-Name": "名前"})

    response = TestClient(app).get("/named")
    assert response.status == 500 and "X-Name" in response.text


def test_the_test_client_fails_on_headers_a_server_cannot_send():
    def wsgi(environ, start_response):
        start_response("200 OK", [("X-Name", "名前")])
        return [b""]

    with pytest.raises(ValueError, match="Latin-1"):
        TestClient(wsgi).get("/")


def test_set_cookie_refuses_what_it_cannot_encode():
    response = Response()
    response.set_cookie("name", "café")  # Latin-1: SimpleCookie escapes it
    with pytest.raises(ValueError, match="encode"):
        response.set_cookie("name", "名前")


# ================================================================== query parameters ==


def query_app():
    app = make_app(dev=True)

    @app.get("/flag")
    def flag(flag: bool = False):
        return {"flag": flag}

    @app.get("/maybe")
    def maybe(flag: typing.Optional[bool] = None):
        return {"flag": flag}

    @app.get("/ids")
    def ids(ids: list[int] = []):  # noqa: B006 - read only
        return {"ids": ids}

    @app.get("/tags")
    def tags(tag: list[str] | None = None, on: list[bool] | None = None):
        return {"tag": tag, "on": on}

    @app.get("/either")
    def either(value: typing.Union[int, bool] = 0):
        return {"value": value}

    return app


@pytest.mark.parametrize("raw, expected", [("true", True), ("1", True), ("on", True), ("YES", True),
                                           ("false", False), ("0", False), ("off", False), ("No", False)])
def test_a_bool_query_parameter_can_be_set(raw, expected):
    client = TestClient(query_app())
    assert client.get(f"/flag?flag={raw}").json() == {"flag": expected}
    assert client.get(f"/maybe?flag={raw}").json() == {"flag": expected}


def test_list_and_bad_query_parameters():
    client = TestClient(query_app())
    assert client.get("/ids?ids=1&ids=2").json() == {"ids": [1, 2]}
    assert client.get("/ids?ids=7").json() == {"ids": [7]}
    assert client.get("/ids").json() == {"ids": []}
    assert client.get("/tags?tag=a&tag=b&on=yes&on=0").json() == {"tag": ["a", "b"], "on": [True, False]}
    assert client.get("/either?value=1").json() == {"value": 1}
    assert client.get("/either?value=true").json() == {"value": True}
    for url in ("/flag?flag=maybe", "/flag?flag=", "/ids?ids=1&ids=x"):
        bad = client.get(url)
        assert bad.status == 400 and "J211" in bad.text, url


# ============================================================================ crashes ==


def test_non_ascii_cookies_and_tokens_are_treated_as_absent():
    app = make_app()
    client = TestClient(app)
    for cookie in (f"{CSRF_COOKIE}=a.b.\xe9", "jongo_session=x.y.\xc3\xa9"):
        environ = client._environ("GET", "/")
        environ["HTTP_COOKIE"] = cookie
        status, headers, _ = call(app, environ)
        assert status == 200
    environ = client._environ("GET", "/")
    environ["HTTP_COOKIE"] = f"{CSRF_COOKIE}=a.b.\xe9"
    assert f"{CSRF_COOKIE}=" in call(app, environ)[1]["set-cookie"], "a fresh token replaces the bad one"
    client.get("/")
    environ = client._environ("POST", "/form", data={"a": "1"}, headers={"X-CSRF-Token": "\xe9" * 40})
    assert call(app, environ)[0] == 403
    assert Signer("s").loads("a.b.\xe9") is None and Signer("s").loads(None) is None


@pytest.mark.parametrize("charset", ["no-such-charset", "base64", "punycode", "iso-8859-1"])
def test_a_part_charset_never_crashes(charset):
    app = make_app()
    body = (b'--b\r\nContent-Disposition: form-data; name="a"\r\nContent-Type: text/plain; charset=' + charset.encode()
            + b"\r\n\r\n" + b"caf\xc3\xa9" + b"9" * 100_000 + b"\r\n--b--\r\n")
    started = time.perf_counter()
    response = TestClient(app).post("/open", data=body, headers={"Content-Type": "multipart/form-data; boundary=b"})
    assert response.status == 200 and time.perf_counter() - started < 1.0
    value = response.json()["form"]["a"][0]
    assert value.startswith("cafÃ©" if charset == "iso-8859-1" else "café")


def test_deeply_nested_json_is_a_400():
    app = make_app()
    app.mcp()
    client = TestClient(app)
    deep = "[" * 100_000 + "]" * 100_000
    request = Request({"REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(deep)), "wsgi.input": io.BytesIO(deep.encode())})
    with pytest.raises(HTTPError) as caught:
        request.json
    assert caught.value.status == 400 and caught.value.code == "J212"

    @server
    def echo(request, value=None):
        return value

    reply = client.post(f"/_jongo/rpc/{echo.id}", data='{"args": ' + deep + "}", headers={"Content-Type": "application/json"})
    assert reply.status == 400 and reply.json()["error"]["code"] == "J402"
    app.dev = True  # the MCP endpoint answers this machine without a token in dev
    mcp = client.post("/_jongo/mcp", data=deep, headers={"Content-Type": "application/json", "Host": "localhost"})
    assert mcp.status == 400 and mcp.json()["error"]["code"] == -32700
    assert mcp.json()["error"]["data"]["code"] == "J422"
    assert app.monitor.snapshot()["errors"] == []


def test_the_stdio_server_survives_any_line(monkeypatch):
    app = make_app()
    server_ = McpServer(app, tools=None)
    lines = ["[" * 100_000, json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
             json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"})]
    out = io.StringIO()
    original = server_.handle
    monkeypatch.setattr(server_, "handle", lambda message, request=None: (
        (_ for _ in ()).throw(RuntimeError("boom")) if message.get("id") == 2 else original(message, request)))
    serve_stdio(server_, stdin=io.StringIO("\n".join(lines) + "\n"), stdout=out)
    answers = [json.loads(line) for line in out.getvalue().splitlines()]
    assert answers[0]["error"]["code"] == -32700 and answers[0]["error"]["data"]["code"] == "J422"
    assert answers[1]["id"] == 2 and answers[1]["error"]["code"] == -32603
    assert answers[2] == {"jsonrpc": "2.0", "id": 3, "result": {}}


@pytest.mark.parametrize("path", ["/static/\x00", "/static/a\x00.txt", "/static/" + "a" * 5000,
                                  "/static/" + "/".join(["a" * 255] * 20)])
def test_odd_static_paths_are_not_found(path):
    assert TestClient(make_app()).get(path).status == 404


# ============================================================================ CSRF origin ==


def rpc_environ(client, fn, *, scheme, origin):
    client.get("/")
    environ = client._environ("POST", f"/_jongo/rpc/{fn.id}", json={})
    environ["wsgi.url_scheme"] = scheme
    if origin is not None:
        environ["HTTP_ORIGIN"] = origin
    return environ


def test_the_origin_check_compares_the_scheme():
    app = make_app()

    @server
    def ping(request) -> str:
        return "pong"

    client = TestClient(app)
    cases = [("https", "https://testserver", 200), ("https", "http://testserver", 403),
             ("http", "http://testserver", 200), ("http", "https://testserver", 200),  # TLS ended at a proxy
             ("https", "https://evil.example", 403), ("https", "null", 403), ("https", "http://[::1", 403),
             ("https", None, 200)]
    for scheme, origin, status in cases:
        assert call(app, rpc_environ(client, ping, scheme=scheme, origin=origin))[0] == status, (scheme, origin)


def test_rotate_csrf_issues_a_new_cookie():
    app = make_app()

    @server
    def sign_in(request) -> str:
        old = request.csrf_token
        new = request.rotate_csrf()
        assert new != old and request.csrf_token == new and app.signer.loads(new) is not None
        return new

    client = TestClient(app)
    client.get("/")
    before = client.cookies[CSRF_COOKIE]
    new = client.rpc(sign_in)
    assert client.cookies[CSRF_COOKIE] == new != before
    assert client.rpc(sign_in), "the new token works for the next call"


# ============================================================================== framing ==


def test_pages_refuse_foreign_framing_by_default():
    app = make_app()

    @app.get("/own-csp")
    def own_csp():
        return Response("x", headers={"content-security-policy": "default-src 'self'"})

    headers = TestClient(app).get("/").headers
    assert headers["x-frame-options"] == "SAMEORIGIN" and headers["content-security-policy"] == "frame-ancestors 'self'"
    own = TestClient(app).get("/own-csp")
    assert own.headers["content-security-policy"] == "default-src 'self'" and own.headers["x-frame-options"] == "SAMEORIGIN"
    assert [k for k, _ in own.header_list if k.lower() == "content-security-policy"] == ["content-security-policy"]

    deny = TestClient(make_app(frame_options="deny")).get("/").headers
    assert deny["x-frame-options"] == "DENY" and deny["content-security-policy"] == "frame-ancestors 'none'"
    none = TestClient(make_app(frame_options=None)).get("/").headers
    assert "x-frame-options" not in none and "content-security-policy" not in none
    with pytest.raises(ValueError):
        make_app(frame_options="ALLOW-FROM https://example.com")


# ============================================================================= app.root ==


def test_root_comes_from_the_calling_file_when_the_name_is_not_a_module(tmp_path, monkeypatch):
    code = tmp_path / "code"
    code.mkdir()
    (code / "shop.py").write_text('from jongo import Jongo\napp = Jongo("My Shop", secret_key="k" * 40)\n')
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    spec = importlib.util.spec_from_file_location("not_registered_shop", code / "shop.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # never put in sys.modules
    assert "not_registered_shop" not in sys.modules
    assert module.app.root == code.resolve()
    assert Jongo("My Shop", root=elsewhere, secret_key="k" * 40).root == elsewhere.resolve()


def test_root_falls_back_to_the_cwd_with_a_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    namespace: dict = {}
    with caplog.at_level(logging.WARNING, logger="jongo"):
        exec(compile('from jongo import Jongo\napp = Jongo("x", secret_key="k" * 40)', "<string>", "exec"), namespace)
    assert namespace["app"].root == Path.cwd()
    assert "root=" in caplog.text
