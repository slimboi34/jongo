"""Regression tests for 0.5.2: what a second-round review of 0.5.1 found (regressions, prod/dev mismatch)."""
from __future__ import annotations

import logging
import shutil
import subprocess

import pytest

from jongo import Jongo, auth, component, db, server
from jongo import log as jlog
from jongo.compiler import Bundler
from jongo.errors import CompileError
from jongo.html import a, button, div, h, p


@pytest.fixture
def own_components():
    from jongo import vdom

    saved = dict(vdom.COMPONENTS)
    yield
    vdom.COMPONENTS.clear()
    vdom.COMPONENTS.update(saved)


def compile_component(comp) -> str:
    return Bundler().resolve(comp.__name__, comp)


# --------------------------------------------------- J114 only rejects functions, never strings ---


def test_j114_does_not_reject_on_attributes_that_are_not_functions(own_components):
    # These compiled under 0.5.0 (the attribute is dropped, as inline handlers always are);
    # 0.5.1 made each a compile error, so the app refused to start.
    @component
    def Tracked(n=1):
        script = "track()"
        x = "y"
        return div(a("x", onclick=f"track({n})"), button("b", onclick=script), div(one=x, only=x))

    compile_component(Tracked)


def test_j114_still_catches_handlers_by_lambda_local_def_and_server_function(own_components):
    @component
    def ByDef():
        async def save(event):
            await save_052()

        return button("go", onClick=save)

    @component
    def ByServer():
        return button("go", onClick=save_052)

    @component
    def ByH():
        return h("my-widget", onChange=lambda e: None)

    for comp in (ByDef, ByServer, ByH):
        with pytest.raises(CompileError) as err:
            compile_component(comp)
        assert err.value.code == "J114", comp


# ------------------------------------------------------- JONGO_ENV=production beats JONGO_DEV=1 ---


@pytest.fixture
def prod_app(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "ITERATIONS", 1000)
    for name in ("JONGO_STATUS_TOKEN", "JONGO_STATUS", "JONGO_ENV"):
        monkeypatch.delenv(name, raising=False)
    app = Jongo("fix052_prod", root=tmp_path, database=":memory:", secret_key="k" * 40, dev=False)
    app.admin()
    db.migrate()
    return app


def test_status_endpoint_stays_closed_in_dev_mode_when_jongo_env_is_production(prod_app, monkeypatch):
    prod_app.dev = True  # e.g. a JONGO_DEV=1 copied into a deployed server's environment
    monkeypatch.setenv("JONGO_ENV", "production")
    client = prod_app.test_client()
    response = client.get("/_jongo/status")
    assert response.status == 403 and response.json()["error"]["code"] == "J508"
    monkeypatch.setenv("JONGO_STATUS_TOKEN", "t" * 32)
    assert client.get("/_jongo/status", headers={"Authorization": "Bearer " + "t" * 32}).status == 200
    monkeypatch.delenv("JONGO_ENV")
    assert client.get("/_jongo/status").status == 200  # plain dev mode: open, as before


def test_doctor_fails_dev_mode_when_jongo_env_is_production(prod_app, monkeypatch):
    import jongo

    prod_app.dev = True
    monkeypatch.setenv("JONGO_ENV", "production")
    report = jongo.doctor(prod_app)
    checks = {c["id"]: c for c in report["checks"]}
    assert report["production"] is True and not report["ok"]
    assert checks["dev_mode"]["status"] == "fail" and checks["dev_mode"]["code"] == "J502"
    assert "open" not in checks["status_endpoint"]["message"]


def test_serve_warns_when_dev_mode_meets_jongo_env_production(prod_app, monkeypatch, caplog):
    import importlib

    server_module = importlib.import_module("jongo.server")  # the module, not the @server decorator

    prod_app.dev = True
    monkeypatch.setenv("JONGO_ENV", "production")

    class Stop(Exception):
        pass

    def no_bind(*args, **kwargs):
        raise Stop

    monkeypatch.setattr(server_module, "auto_migrate", lambda *a, **k: None)
    monkeypatch.setattr(server_module, "ThreadingWSGIServer", no_bind)
    monkeypatch.setattr(server_module, "setup_logging", lambda dev: None)
    with caplog.at_level(logging.WARNING, logger="jongo"), pytest.raises(Stop):
        server_module.serve(prod_app, port=0)
    assert any("J502" in r.getMessage() for r in caplog.records)


def test_status_token_with_surrounding_whitespace_still_works(prod_app, monkeypatch):
    monkeypatch.setenv("JONGO_STATUS_TOKEN", "t" * 32 + "\n")  # e.g. read from a file with its newline
    client = prod_app.test_client()
    assert client.get("/_jongo/status", headers={"Authorization": "Bearer " + "t" * 32}).status == 200


def test_status_errors_are_json_on_every_spelling_of_the_path(prod_app):
    response = prod_app.test_client().get("/_jongo/status/")  # reaches the status route: answer like it
    assert response.status == 403 and response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "J508"


# ----------------------------------------------------------------- server calls: falsy bodies ---


@server
def answer_052(n: int = 41) -> int:
    return n + 1


@server
def save_052() -> int:
    return 1


@pytest.fixture
def rpc_app(tmp_path):
    app = Jongo("fix052_rpc", root=tmp_path, database=":memory:", secret_key="k" * 40)

    @app.page("/")
    def home():
        return p("home")

    return app, answer_052


@pytest.mark.parametrize("body", [b"[]", b"null", b"0", b"false", b'""'])
def test_rpc_body_that_is_json_but_not_an_object_is_malformed(rpc_app, body):
    app, fn = rpc_app
    client = app.test_client()
    client.get("/")
    token = client.cookies["jongo_csrf"]
    response = client.post(f"/_jongo/rpc/{fn.id}", data=body,
                           headers={"X-CSRF-Token": token, "Content-Type": "application/json"})
    assert response.status == 400 and response.json()["error"]["code"] == "J402", response.text


def test_rpc_with_an_empty_body_still_means_no_arguments(rpc_app):
    app, fn = rpc_app
    client = app.test_client()
    client.get("/")
    token = client.cookies["jongo_csrf"]
    response = client.post(f"/_jongo/rpc/{fn.id}", data=b"",
                           headers={"X-CSRF-Token": token, "Content-Type": "application/json"})
    assert response.status == 200 and response.json() == {"ok": 42}


# ---------------------------------------------------------------------------- request ids ---


def test_request_ids_do_not_repeat_on_a_busy_server():
    ids = set()
    for _ in range(1000):
        with jlog.request_scope() as rid:
            ids.add(rid)
    # 4 hex digits (0.5.1) repeated within a few hundred requests; 8 make a repeat here ~1 in 10,000
    assert len(ids) == 1000 and all(len(rid) == 8 for rid in ids)


# ------------------------------------------------------------------ dev overlay shows the code ---


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_dev_error_overlay_shows_the_code_and_hint():
    from jongo.compiler.bundle import runtime_source

    source = runtime_source()
    start = source.index("function $report(err)")
    end = source.index("\n}\n", start) + 3
    script = """
    const elements = {};
    const document = {
      getElementById: (id) => elements[id] || null,
      createElement: () => ({ style: {} }),
      body: { appendChild: (el) => { elements[el.id] = el; } },
    };
    let $BOOT = { dev: true };
    console.error = () => {};
    """ + source[start:end] + """
    const err = new Error("Server error"); err.name = "ServerError"; err.code = "J407"; err.hint = "see the log";
    $report(err);
    process.stdout.write(elements["jongo-error"].textContent + "\\n---\\n");
    const dom = new Error("blocked"); dom.name = "SecurityError"; dom.code = 18;
    $report(dom);
    process.stdout.write(elements["jongo-error"].textContent);
    """
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    first, second = out.stdout.split("\n---\n")
    assert first.startswith("[J407] ServerError: Server error") and "hint: see the log" in first
    assert "jongo explain J407" in first
    assert second == "SecurityError: blocked"  # a DOMException's numeric code isn't a Jongo code
