"""Regression tests for 0.5.1: the diagnostics found wrong or crashing by a black-box run of 0.5.0."""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from jongo import Jongo, component, server
from jongo.compiler import Bundler
from jongo.errors import CompileError, explain
from jongo.html import button, div, p

ROOT = Path(__file__).resolve().parent.parent


def run_cli(*args, cwd, env=None, timeout=60):
    environment = {**os.environ, "NO_COLOR": "1"}
    for name in ("JONGO_DEV", "JONGO_ENV", "JONGO_APP", "JONGO_STATUS_TOKEN", "JONGO_DATABASE"):
        environment.pop(name, None)
    environment.update(env or {})
    return subprocess.run([sys.executable, "-m", "jongo", *args], cwd=cwd, capture_output=True, text=True,
                          env=environment, timeout=timeout)


def run_python(code: str, cwd, timeout=60):
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, env={**os.environ, "NO_COLOR": "1"})


APP = '''
from jongo import Jongo, component, db, server, state
from jongo.html import *

app = Jongo(__name__, database="{database}")


class Item(db.Model):
    name = db.Text()


@app.page("/")
def home():
    return div("hi")
'''


# ------------------------------------------------------------------- import jongo.server ---


def test_importing_the_server_module_keeps_the_decorator_working(tmp_path):
    result = run_python('''
        import jongo.server                 # rebinds the package attribute `jongo.server` to the module
        from jongo import server
        from jongo.rpc import ServerFunction

        @server
        def plain(x: int) -> int:
            return x

        @server(login_required=True)
        def guarded() -> None:
            pass

        assert isinstance(plain, ServerFunction) and plain(2) == 2
        assert isinstance(guarded, ServerFunction) and guarded.login_required
        assert jongo.server.ThreadingWSGIServer and jongo.server.serve
        print("ok")
    ''', cwd=tmp_path)
    assert result.returncode == 0 and result.stdout.strip() == "ok", result.stderr


# --------------------------------------------------------------------------- server calls ---


@server
def greet(name: str, punctuation: str = "!") -> str:
    return f"hi {name}{punctuation}"


@server
def stale() -> None:
    raise sqlite3.OperationalError("no such column: item.price")


@pytest.fixture
def rpc_app(tmp_path):
    app = Jongo("fixes_051_rpc", root=tmp_path, dev=True, secret_key="k" * 40)

    @app.page("/")
    def home():
        return p("home")

    @app.page("/list")
    def listing(page: int = 1):
        return p(f"page {page}")

    @app.post("/echo", csrf=False)
    def echo(request):
        return {"got": request.json}

    return app, greet


def _rpc(client, fn, body: bytes, headers=None):
    client.get("/")
    token = client.cookies["jongo_csrf"]
    return client.post(f"/_jongo/rpc/{fn.id}", data=body,
                       headers={"X-CSRF-Token": token, "Content-Type": "application/json", **(headers or {})})


def test_rpc_body_that_isnt_json_is_a_malformed_call_not_an_app_error(rpc_app):
    app, greet = rpc_app
    client = app.test_client()
    response = _rpc(client, greet, b"hello")
    error = response.json()["error"]
    assert response.status == 400 and error["code"] == "J402", error
    assert "not valid JSON" in error["message"]
    assert "Nothing to fix in Jongo" not in (error["hint"] or "")

    listed = _rpc(client, greet, b"[1, 2]")
    assert listed.json()["error"]["code"] == "J402" and '"args"' in listed.json()["error"]["message"]


def test_rpc_unknown_keyword_is_named(rpc_app):
    app, greet = rpc_app
    client = app.test_client()
    response = _rpc(client, greet, json.dumps({"args": [], "kwargs": {"name": "a", "punctuaton": "?"}}).encode())
    error = response.json()["error"]
    assert response.status == 400 and error["code"] == "J404"
    assert "'punctuaton'" in error["message"] and "unexpected keyword" in error["message"]
    assert "greet(name: str, punctuation: str = '!')" in error["hint"]


def test_rpc_csrf_and_method_hints_are_about_server_calls(rpc_app):
    app, greet = rpc_app
    client = app.test_client()
    client.get("/")
    refused = client.post(f"/_jongo/rpc/{greet.id}", json={"args": ["a"]}, headers={"X-CSRF-Token": "forged"})
    error = refused.json()["error"]
    assert refused.status == 403 and error["code"] == "J207"
    assert "X-CSRF-Token" in error["hint"] and "hidden" not in error["hint"]

    wrong_method = client.get(f"/_jongo/rpc/{greet.id}")
    assert wrong_method.status == 405 and wrong_method.json()["error"]["code"] == "J206"
    assert "POST" in wrong_method.json()["error"]["hint"]


def test_page_query_parameter_of_the_wrong_type_is_j211(rpc_app):
    app, _ = rpc_app
    client = app.test_client()
    assert "page 3" in client.get("/list?page=3").text
    response = client.get("/list?page=abc")
    assert response.status == 400
    assert "J211" in response.text and "&#x27;abc&#x27;" in response.text
    assert "browser before the call" not in response.text  # that's the server-function hint


def test_route_body_that_isnt_json_has_a_code(rpc_app):
    app, _ = rpc_app
    response = app.test_client().post("/echo", data=b"{nope", headers={"Content-Type": "application/json"})
    assert response.status == 400 and "J212" in response.text


def test_production_server_call_error_keeps_its_real_code(tmp_path):
    app = Jongo("fixes_051_prod", root=tmp_path, dev=False, secret_key="k" * 40)

    @app.page("/")
    def home():
        return p("home")

    client = app.test_client()
    response = _rpc(client, stale, b'{"args": []}')
    error = response.json()["error"]
    assert response.status == 500 and error["message"] == "Server error"
    assert error["code"] == "J302"  # the code the log and `jongo status` show, not a blanket J407
    assert "traceback" not in error and "hint" not in error


# -------------------------------------------------------------------------------- compiler ---


@pytest.fixture
def own_components():
    """Components register globally by name; a deliberately broken one must not outlive its test."""
    from jongo import vdom

    saved = dict(vdom.COMPONENTS)
    yield
    vdom.COMPONENTS.clear()
    vdom.COMPONENTS.update(saved)


def compile_component(comp) -> str:
    return Bundler().resolve(comp.__name__, comp)


def test_react_style_event_names_are_a_compile_error(own_components):
    @component
    def Clicker():
        return button("go", onClick=lambda e: None)

    with pytest.raises(CompileError) as err:
        compile_component(Clicker)
    assert err.value.code == "J114" and "on_click" in err.value.hint

    @component
    def Lower():
        return div(button("go", onclick=lambda e: None))

    with pytest.raises(CompileError) as err:
        compile_component(Lower)
    assert err.value.code == "J114"


def test_a_component_prop_named_on_something_is_fine(own_components):
    @component
    def Dialog(onclose=None):
        return div("dialog")

    @component
    def Page():
        return div(Dialog(onclose=lambda: None), button("x", on_click=lambda e: None), onclick="ignored")

    compile_component(Page)  # no J114: onclose= goes to a component, onclick= is a plain string


# ------------------------------------------------------------------------------ CLI / db ---


def test_check_does_not_create_the_sqlite_file(tmp_path):
    (tmp_path / "app.py").write_text(APP.format(database="data/db.sqlite3"))
    report = json.loads(run_cli("check", "--json", cwd=tmp_path).stdout)
    assert report["ok"] is True
    assert any("item" in op["describe"] for op in report["pending_migrations"])
    assert not (tmp_path / "data").exists(), "check is read-only: it must not create the database"


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions as a normal user")
def test_unwritable_database_directory_is_reported_not_a_traceback(tmp_path):
    readonly = tmp_path / "ro"
    readonly.mkdir()
    readonly.chmod(0o555)
    try:
        (tmp_path / "app.py").write_text(APP.format(database="ro/db.sqlite3"))
        report = json.loads(run_cli("check", "--json", cwd=tmp_path).stdout)
        assert report["ok"] is False and report["problems"][0]["code"] == "J301"

        doctor = json.loads(run_cli("doctor", "--json", cwd=tmp_path).stdout)
        database = next(c for c in doctor["checks"] if c["id"] == "database")
        assert database["status"] == "fail" and "exists but isn't writable" in database["message"]
        assert "doesn't exist" not in database["message"]

        migrate = run_cli("migrate", cwd=tmp_path)
        assert migrate.returncode == 1 and "[J301]" in migrate.stderr
        assert "Traceback" not in migrate.stderr
    finally:
        readonly.chmod(0o755)


def test_run_warns_about_pending_migrations(tmp_path):
    (tmp_path / "app.py").write_text(APP.format(database="db.sqlite3"))
    assert run_cli("migrate", cwd=tmp_path).returncode == 0
    (tmp_path / "app.py").write_text(APP.format(database="db.sqlite3").replace(
        "    name = db.Text()", "    name = db.Text()\n    price = db.Int(default=0)"))
    result = run_python('''
        import logging, sys
        sys.path.insert(0, ".")
        import app
        from jongo.server import warn_pending_migrations
        logging.basicConfig(level=logging.WARNING, format="%(message)s")
        warn_pending_migrations()
    ''', cwd=tmp_path)
    assert "[J302]" in result.stderr and "price" in result.stderr and "jongo migrate" in result.stderr


def test_port_in_use_exits_with_its_own_status(tmp_path):
    (tmp_path / "app.py").write_text(APP.format(database=":memory:"))
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        result = run_cli("run", "--host", "127.0.0.1", "--port", str(port), cwd=tmp_path, timeout=30)
    from jongo.server import EXIT_PORT_IN_USE

    assert result.returncode == EXIT_PORT_IN_USE and "[J505]" in result.stderr


def test_status_url_without_a_scheme(tmp_path):
    with socket.socket() as probe:  # a port nothing listens on
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    result = run_cli("status", "--url", f"127.0.0.1:{port}", "--json", "--timeout", "2", cwd=tmp_path)
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "J512"
    assert f"http://127.0.0.1:{port}/_jongo/status" in error["message"]
    assert "unknown url type" not in error["message"]


def test_explain_accepts_a_bare_number():
    assert explain("302")["code"] == "J302"
    assert explain("J114")["title"].startswith("Event handler")


def test_missing_module_hint_says_how_to_install(tmp_path):
    (tmp_path / "app.py").write_text("import surely_not_installed_051\n")
    report = json.loads(run_cli("check", "--json", cwd=tmp_path).stdout)
    problem = report["problems"][0]
    assert problem["code"] == "J509" and "pip install surely_not_installed_051" in problem["hint"]
