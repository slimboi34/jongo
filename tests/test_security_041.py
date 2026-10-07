"""Regression tests for the 0.4.1 security fixes."""
from __future__ import annotations

import importlib
import json
import os
import shutil
import socket
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from jongo import Jongo, auth, db
from jongo.html import a, div, p
from jongo.http import is_local_url
from jongo.vdom import render_to_string

ROOT = Path(__file__).resolve().parents[1]


# -- open redirect after admin login ------------------------------------------------------


@pytest.mark.parametrize("url", ["/admin", "/admin/book?q=1", "/"])
def test_local_urls_are_allowed(url):
    assert is_local_url(url)


@pytest.mark.parametrize(
    "url",
    ["//evil.com", "/\\evil.com", "\\\\evil.com", "/\t/evil.com", "/\n/evil.com", "https://evil.com",
     "javascript:alert(1)", " //evil.com", "", None],
)
def test_offsite_urls_are_refused(url):
    assert not is_local_url(url)


@pytest.fixture
def admin_client(monkeypatch):
    monkeypatch.setattr(auth, "ITERATIONS", 1000)
    app = Jongo("security_test", root=Path(__file__).parent, dev=True, database=":memory:", secret_key="s")
    app.admin()
    db.migrate()
    auth.User.create_user("root", "pw", is_admin=True)
    return app.test_client()


@pytest.mark.parametrize("target", ["/\\evil.com", "/\t/evil.com", "//evil.com"])
def test_admin_login_does_not_redirect_offsite(admin_client, target):
    admin_client.get("/admin/login")
    token = admin_client.cookies["jongo_csrf"]
    response = admin_client.post(
        "/admin/login", data={"username": "root", "password": "pw", "csrf_token": token, "next": target}
    )
    assert response.status == 303
    assert response.headers["location"] == "/admin"


def test_admin_login_keeps_a_local_next(admin_client):
    admin_client.get("/admin/login")
    token = admin_client.cookies["jongo_csrf"]
    response = admin_client.post(
        "/admin/login", data={"username": "root", "password": "pw", "csrf_token": token, "next": "/admin/user"}
    )
    assert response.headers["location"] == "/admin/user"


# -- clickjacking / sniffing headers ---------------------------------------------------------


def test_admin_pages_refuse_framing(admin_client):
    response = admin_client.get("/admin/login")
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_nosniff_on_every_response(admin_client):
    assert admin_client.get("/admin/login").headers["x-content-type-options"] == "nosniff"
    assert admin_client.get("/no-such-page").headers["x-content-type-options"] == "nosniff"


def test_non_admin_pages_can_still_be_framed():
    app = Jongo("frame_test", root=Path(__file__).parent, dev=True, secret_key="s")
    app.admin()

    @app.page("/")
    def home():
        return p("hi")

    assert "x-frame-options" not in app.test_client().get("/").headers


# -- javascript: URLs in upper/mixed-case attribute names ------------------------------------


@pytest.mark.parametrize("name", ["HREF", "Href", "SRC", "FormAction", "XLINK:HREF"])
def test_url_attribute_check_ignores_case(name):
    html = render_to_string(a("x", **{name: "javascript:alert(1)"}))
    assert "javascript" not in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_browser_url_attribute_check_ignores_case(tmp_path):
    runtime = (ROOT / "jongo/compiler/pyrt.js").read_text() + "\n" + (ROOT / "jongo/compiler/dom.js").read_text()
    script = tmp_path / "t.js"
    script.write_text(
        "const set = [];\n"
        "const node = {setAttribute: (k, v) => set.push([k, v]), removeAttribute: () => {}, style: {}};\n"
        + runtime
        + '\n$setProp(node, "HREF", "javascript:alert(1)", undefined, false);'
        + '\n$setProp(node, "href", "/ok", undefined, false);'
        + "\nconsole.log(JSON.stringify(set));\n"
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == [["href", "/ok"]]


# -- the boot <script> block can't be derailed by page text ----------------------------------


def test_boot_json_escapes_every_angle_bracket(tmp_path):
    app = Jongo("boot_test", root=tmp_path, dev=True, secret_key="s")
    app.bundle = lambda: ("", "build")  # other tests' components would otherwise be compiled in

    @app.page("/")
    def home():
        return div("<!--<script>", "</script><b>")

    html = app.test_client().get("/").text
    boot = html.split('<script id="jongo-data" type="application/json">', 1)[1].split("</script>", 1)[0]
    assert "<" not in boot
    data = json.loads(boot)
    assert data["tree"]["c"][0] == "<!--<script>"
    assert '<script src="/_jongo/app.js' in html


# -- the generated secret key ----------------------------------------------------------------


def test_local_secret_is_owner_only(tmp_path):
    Jongo("secret_test", root=tmp_path)
    path = tmp_path / ".jongo" / "secret"
    assert len(path.read_text()) > 40
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_empty_secret_file_is_replaced(tmp_path):
    path = tmp_path / ".jongo" / "secret"
    path.parent.mkdir()
    path.write_text("")
    app = Jongo("secret_test", root=tmp_path)
    secret = path.read_text()
    assert len(secret) > 40
    assert app.signer.key == Jongo("secret_test", root=tmp_path).signer.key


# -- the built-in server drops stalled connections ---------------------------------------------


def test_server_times_out_idle_connections(monkeypatch):
    jserver = importlib.import_module("jongo.server")

    assert jserver._RequestHandler.timeout and jserver._RequestHandler.timeout <= 60  # on by default
    monkeypatch.setattr(jserver._RequestHandler, "timeout", 0.3)
    app = Jongo("timeout_test", root=Path(__file__).parent, secret_key="s")
    httpd = jserver.ThreadingWSGIServer(("127.0.0.1", 0), jserver._RequestHandler)
    httpd.set_app(app)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        sock = socket.create_connection(httpd.server_address, timeout=5)
        sock.sendall(b"GET / HTTP/1.1\r\n")  # never finish the request
        started = time.monotonic()
        assert sock.recv(1024) == b""  # the server hangs up
        assert time.monotonic() - started < 4
        sock.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
