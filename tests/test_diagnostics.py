"""0.5.0 diagnostics: the error catalogue, `jongo doctor`, the live system view and `jongo status`.

What these pin: every code Jongo raises is in the catalogue and every catalogue entry is
raised somewhere; errors carry their code, hint and location to every surface (exceptions,
`jongo check`, server-call JSON, the dev error page, the CLI); the doctor's verdicts and exit
codes; and that the status endpoint is never open without authentication in production and
never reports secrets.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

import jongo
from jongo import Jongo, auth, component, db, server, state
from jongo.errors import (
    CATALOGUE,
    CompileError,
    HTTPError,
    JongoError,
    RouteError,
    URLBuildError,
    classify,
    error_dict,
    explain,
    format_explain,
)
from jongo.html import div, li, p, ul
from jongo.introspect import check
from jongo.monitor import Monitor, redact

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "jongo"
CODE = re.compile(r"^J\d{3}$")


def run_cli(*args, cwd, env=None, timeout=60):
    environment = {**os.environ, "NO_COLOR": "1"}
    for name in ("JONGO_DEV", "JONGO_ENV", "JONGO_APP", "JONGO_STATUS_TOKEN", "JONGO_DATABASE"):
        environment.pop(name, None)
    environment.update(env or {})
    return subprocess.run([sys.executable, "-m", "jongo", *args], cwd=cwd, capture_output=True, text=True,
                          env=environment, timeout=timeout)


# =============================================================================== catalogue ==


def _codes_in_source() -> set[str]:
    """Every "Jnnn" string literal in the package, outside the CATALOGUE definition itself."""
    found = set()
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text())
        skip = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t, ast.Name) and t.id == "CATALOGUE" for t in targets):
                    skip |= {id(n) for n in ast.walk(node)}
        for node in ast.walk(tree):
            if id(node) in skip:
                continue
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and CODE.match(node.value):
                found.add(node.value)
    return found


def test_every_code_raised_is_in_the_catalogue_and_every_entry_is_raised():
    raised = _codes_in_source()
    assert raised - set(CATALOGUE) == set(), "codes used in the code but missing from CATALOGUE"
    assert set(CATALOGUE) - raised == set(), "catalogue entries nothing raises or reports"


def test_every_catalogue_entry_is_complete():
    for code, entry in CATALOGUE.items():
        assert CODE.match(code)
        assert entry["title"] and entry["explanation"] and entry["fix"], code
        assert entry["title"][0].isupper(), code
        assert code[:2] in jongo.errors.CATEGORIES


def test_the_catalogue_is_documented_in_the_guide():
    guide = (ROOT / "docs" / "guide.md").read_text()
    missing = [code for code, entry in CATALOGUE.items() if f"| `{code}` | {entry['title']} |" not in guide]
    assert not missing, f"error codes missing from docs/guide.md: {missing}"


def test_explain():
    entry = explain("j302")
    assert entry["code"] == "J302" and entry["category"].startswith("Database")
    assert "jongo migrate" in entry["fix"]
    assert explain("J999") is None
    text = format_explain("J101")
    assert text.startswith("J101") and "Fix:" in text and "Example:" in text


def test_explain_cli(tmp_path):
    one = run_cli("explain", "J403", cwd=tmp_path)
    assert one.returncode == 0 and "wrong type" in one.stdout
    listed = run_cli("explain", "--list", "--json", cwd=tmp_path)
    assert listed.returncode == 0
    assert [e["code"] for e in json.loads(listed.stdout)] == sorted(CATALOGUE)
    as_json = run_cli("explain", "J302", "--json", cwd=tmp_path)
    assert json.loads(as_json.stdout)["code"] == "J302"
    unknown = run_cli("explain", "J999", cwd=tmp_path)
    assert unknown.returncode == 1 and "unknown error code" in unknown.stderr


# ===================================================================== exceptions carry codes ==


def test_jongo_errors_keep_their_hierarchy_and_gain_codes():
    assert issubclass(CompileError, JongoError) and issubclass(HTTPError, JongoError)
    assert issubclass(jongo.NotFound, HTTPError) and issubclass(jongo.Forbidden, HTTPError)
    assert issubclass(RouteError, ValueError) and issubclass(URLBuildError, KeyError)
    assert jongo.NotFound().code == "J205" and jongo.Forbidden().code == "J210"
    err = HTTPError(400, "nope", code="J403", hint="send an int")
    assert (err.status, err.message, err.code, err.hint) == (400, "nope", "J403", "send an int")
    d = err.to_dict()
    assert d["code"] == "J403" and d["hint"] == "send an int" and d["explain"] == "jongo explain J403"


def test_orm_exceptions_have_codes_without_changing_base_classes():
    from jongo.db.models import FieldDoesNotExist

    assert db.ValidationError("bad").to_dict()["code"] == "J304"
    assert not issubclass(db.ValidationError, JongoError)  # unchanged: still a plain Exception subclass
    assert db.DoesNotExist.code == "J307" and db.MultipleObjectsReturned.code == "J308"
    assert db.MigrationError.code == "J303"
    assert issubclass(FieldDoesNotExist, KeyError) and FieldDoesNotExist.code == "J310"
    with pytest.raises(LookupError) as info:
        db.get_model("NoSuchModelAnywhere")
    assert info.value.code == "J310"


def test_database_url_errors_have_a_code_and_redact_passwords():
    with pytest.raises(ValueError) as info:
        db.configure("mysql://user:hunter2@host/db")
    assert info.value.code == "J309"
    assert "hunter2" not in str(info.value)


def test_classify_recognises_driver_errors():
    assert classify(sqlite3.OperationalError("no such table: todo"))[0] == "J302"
    assert classify(sqlite3.OperationalError("table todo has no column named due"))[0] == "J302"
    assert classify(sqlite3.OperationalError("unable to open database file"))[0] == "J301"
    assert classify(sqlite3.IntegrityError("UNIQUE constraint failed: user.email"))[0] == "J305"
    assert classify(sqlite3.OperationalError("database is weird"))[0] == "J300"
    code, hint = classify(ZeroDivisionError("x"))
    assert code == "J000" and hint is None


def test_error_dict_points_at_the_app_frame():
    def broken():
        return 1 / 0

    try:
        broken()
    except ZeroDivisionError as exc:
        d = error_dict(exc, ROOT)
    assert d["code"] == "J000" and d["file"] == "tests/test_diagnostics.py" and isinstance(d["line"], int)


def _compile_error(source: str, tmp_path, name: str) -> CompileError:
    path = tmp_path / f"{name}.py"
    path.write_text(textwrap.dedent(source))
    sys.path.insert(0, str(tmp_path))
    try:
        module = __import__(name)
    finally:
        sys.path.remove(str(tmp_path))
    from jongo.compiler.bundle import Bundler

    bundler = Bundler(roots=[tmp_path])
    try:
        with pytest.raises(CompileError) as info:
            bundler.add_components([module.Widget])
    finally:  # registries are global: don't leave a broken component for other tests' bundles
        from jongo.vdom import COMPONENTS

        for key, comp in list(COMPONENTS.items()):
            if comp.fn.__module__ == name:
                del COMPONENTS[key]
        for key, model in list(db.models_registry.items()):
            if model.__module__ == name:
                del db.models_registry[key]
    return info.value


@pytest.mark.parametrize("body, code", [
    ("    import math\n    return p(str(math.pi))", "J101"),
    ("    return p(Thing.all())", "J102"),
    ("    return p(nmae)", "J103"),
    ("    x = b'raw'\n    return p(x)", "J104"),
])
def test_compile_errors_have_specific_codes_and_locations(tmp_path, body, code):
    name = f"diag_compile_{code.lower()}"
    source = (
        "from jongo import component, db\nfrom jongo.html import p\n\n"
        f"class Thing(db.Model):\n    name = db.Text()\n    class Meta:\n        table = '{name}_thing'\n\n"
        "@component\ndef Widget():\n" + body + "\n"
    )
    err = _compile_error(source, tmp_path, name)
    assert err.code == code
    assert err.hint, "every compile error has a hint (its own, or the catalogue's fix)"
    assert err.filename.endswith(f"{name}.py") and err.lineno >= 11
    assert str(err).startswith(f"[{code}]")
    assert err.to_dict()["line"] == err.lineno


def test_route_errors_name_the_line(tmp_path):
    app = Jongo("diag_routes", root=tmp_path)
    with pytest.raises(RouteError) as info:
        @app.page("/posts/<integer:id>")
        def post(id):
            return p(id)
    assert info.value.code == "J201" and "int" in info.value.hint
    assert info.value.file == __file__ and isinstance(info.value.line, int)
    with pytest.raises(ValueError):  # still catchable the old way
        app.page("nope")(lambda: None)
    with pytest.raises(KeyError) as info:
        app.url_for("missing")
    assert info.value.code == "J203" and str(info.value) == "no route named 'missing'"


def test_runtime_component_errors_have_codes():
    with pytest.raises(JongoError) as info:
        state(0)
    assert info.value.code == "J107"

    @component
    def DiagListy():
        return [li("a"), li("b")]

    app = Jongo("diag_listy", dev=True, secret_key="x" * 40)

    @app.page("/diag-listy")
    def page():
        return ul(DiagListy())

    response = app.test_client().get("/diag-listy")
    assert response.status == 500
    assert "J110" in response.text and "jongo explain J110" in response.text and "Try this" in response.text


# ======================================================================== server calls (rpc) ==


diag_app = Jongo("diag_rpc_app", root=Path(__file__).parent, database=":memory:", secret_key="k" * 40)


class DiagNote(db.Model):
    text = db.Text(unique=True)

    class Meta:
        table = "diag_note"


@server
def diag_double(n: int) -> int:
    return n * 2


@server
def diag_crash() -> int:
    return 1 / 0  # the line the error report should point at


@server
def diag_create(text: str) -> dict:
    return DiagNote.create(text=text).to_dict()


@diag_app.page("/diag")
def diag_page():
    return p("ok")


@pytest.fixture
def rpc_client():
    db.configure(":memory:")
    db.migrate()
    diag_app.dev = True
    diag_app.monitor.clear()
    yield diag_app.test_client()
    diag_app.dev = False


def _rpc(client, fid, *args):
    return client.post(f"/_jongo/rpc/{fid}", json={"args": list(args)})


def test_rpc_errors_carry_code_hint_and_request_id(rpc_client):
    wrong = _rpc(rpc_client, diag_double.id, "three").json()["error"]
    assert wrong["code"] == "J403" and "int" in wrong["hint"] and len(wrong["request_id"]) == 4
    arity = _rpc(rpc_client, diag_double.id, 1, 2).json()["error"]
    assert arity["code"] == "J404" and "(n: int)" in arity["hint"]
    missing = _rpc(rpc_client, "nope.nothing").json()["error"]
    assert missing["code"] == "J401"
    malformed = rpc_client.post(f"/_jongo/rpc/{diag_double.id}", json=[1]).json()["error"]
    assert malformed["code"] == "J402"
    rpc_client.rpc(diag_create, "same")
    with pytest.raises(jongo.ServerError) as info:
        rpc_client.rpc(diag_create, "same")
    assert info.value.code == "J304" and info.value.errors


def test_rpc_crash_in_dev_points_at_the_line(rpc_client):
    error = _rpc(rpc_client, diag_crash.id).json()["error"]
    assert error["code"] == "J407" and error["type"] == "ZeroDivisionError"
    assert error["file"].endswith("test_diagnostics.py") and error["line"] and "traceback" in error


def test_rpc_crash_in_production_reveals_nothing(rpc_client):
    diag_app.dev = False
    response = _rpc(rpc_client, diag_crash.id)
    error = response.json()["error"]
    assert response.status == 500
    assert error["message"] == "Server error" and error["code"] == "J407"
    assert not {"traceback", "file", "line", "hint"} & set(error)
    # ...but the operator can still find it, by request id, in the live view
    recorded = jongo.status(diag_app)["errors"][0]
    assert recorded["request_id"] == error["request_id"] and recorded["file"].endswith("test_diagnostics.py")


def test_check_problems_have_codes():
    report = check(diag_app)
    for problem in report["problems"]:
        assert CODE.match(problem["code"])


# ============================================================================= live system view ==


def test_status_records_requests_routes_and_errors(rpc_client):
    rpc_client.get("/diag?token=supersecret")
    _rpc(rpc_client, diag_double.id, 2)
    _rpc(rpc_client, diag_crash.id)
    rpc_client.get("/no-such-page")
    snap = jongo.status(diag_app)
    assert snap["mode"] == "dev" and snap["jongo"] == jongo.__version__
    paths = [r["path"] for r in snap["requests"]["recent"]]
    assert paths[0] == "/no-such-page" and "/diag" in paths
    assert "supersecret" not in json.dumps(snap), "query strings are never recorded"
    routes = {r["route"]: r for r in snap["routes"]}
    assert routes["/diag"]["count"] == 1
    assert routes[f"@server {diag_crash.id}"]["errors"] == 1
    assert routes["(no route)"]["count"] == 1
    assert snap["requests"]["recent"][0]["code"] == "J205"
    assert snap["requests"]["by_status"] == {"2xx": 2, "4xx": 1, "5xx": 1}
    error = snap["errors"][0]
    assert error["code"] == "J407" and error["traceback"][-1].endswith("in diag_crash")
    assert snap["database"] == {"backend": "sqlite"}


def test_status_detects_n_plus_one(rpc_client):
    from jongo import log as jlog

    previous = jlog.get_logger("sql").level
    jlog.get_logger("sql").setLevel("DEBUG")
    try:
        for i in range(6):
            DiagNote.create(text=f"n{i}")

        app = Jongo("diag_n1", dev=False, secret_key="k" * 40)

        @app.page("/n1")
        def n1():
            return ul(*[li(DiagNote.get(id=n.id).text) for n in DiagNote.all()])

        app.test_client().get("/n1")
        found = jongo.status(app)["sql"]["n_plus_one"]
    finally:
        jlog.get_logger("sql").setLevel(previous)
    assert found and found[0]["repeated"] >= 5 and "diag_note" in found[0]["statement"]
    assert "n0" not in found[0]["statement"], "parameters are never recorded"


def test_status_counts_channel_subscribers():
    from jongo.live import hub

    app = Jongo("diag_live", dev=True, secret_key="k" * 40)

    @app.channel("room:<int:id>")
    def room(request, id):
        return True

    subscriber = hub.subscribe(["room:7"])
    try:
        channels = jongo.status(app)["channels"]
        assert channels["declared"] == ["room:<int:id>"]
        assert channels["subscribers"] >= 1 and channels["active"]["room:7"] == 1
    finally:
        hub.unsubscribe(subscriber)


def test_monitor_is_bounded_and_thread_safe():
    monitor = Monitor(max_requests=50, max_errors=5)

    def hammer():
        for i in range(500):
            monitor.record_request("GET", f"/x/{i}", 200, 1.0, "abcd", "/x/<i>")

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snap = monitor.snapshot(recent=100)
    assert snap["requests"]["total"] == 4000 and len(snap["requests"]["recent"]) == 50
    assert snap["routes"][0]["count"] == 4000
    for _ in range(9):
        try:
            raise RuntimeError("postgres://app:hunter2@db/x failed")
        except RuntimeError as exc:
            monitor.record_error(exc, rid="r", method="GET", path="/")
    assert len(monitor.errors) == 5
    assert "hunter2" not in json.dumps(monitor.snapshot())


def test_redact():
    assert redact("postgres://u:p4ss@h/db") == "postgres://u:***@h/db"
    assert len(redact("x" * 1000)) == 300


# ------------------------------------------------------------------- /_jongo/status access rules


@pytest.fixture
def prod_app(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "ITERATIONS", 1000)
    monkeypatch.delenv("JONGO_STATUS_TOKEN", raising=False)
    monkeypatch.delenv("JONGO_STATUS", raising=False)
    app = Jongo("diag_prod", root=tmp_path, database=":memory:", secret_key="k" * 40, dev=False)
    app.admin()
    db.migrate()
    return app


def test_status_endpoint_is_open_in_dev(prod_app):
    prod_app.dev = True
    response = prod_app.test_client().get("/_jongo/status")
    assert response.status == 200 and response.json()["mode"] == "dev"
    assert response.headers["cache-control"] == "no-store"


def test_status_endpoint_refuses_anonymous_requests_in_production(prod_app):
    response = prod_app.test_client().get("/_jongo/status")
    assert response.status == 403
    assert response.json()["error"]["code"] == "J508"
    assert "requests" not in response.text


def test_status_endpoint_refuses_a_non_admin(prod_app):
    user = auth.User.create_user("bob", "pw")
    client = prod_app.test_client()
    client.login(user)
    assert client.get("/_jongo/status").status == 403


def test_status_endpoint_admits_an_admin(prod_app):
    admin = auth.User.create_user("root", "pw", is_admin=True)
    client = prod_app.test_client()
    client.login(admin)
    assert client.get("/_jongo/status").status == 200


def test_status_endpoint_token(prod_app, monkeypatch):
    token = "t" * 32
    monkeypatch.setenv("JONGO_STATUS_TOKEN", token)
    client = prod_app.test_client()
    assert client.get("/_jongo/status", headers={"Authorization": f"Bearer {token}"}).status == 200
    assert client.get("/_jongo/status", headers={"Authorization": "Bearer wrong"}).status == 403
    assert client.get("/_jongo/status", headers={"Authorization": f"Bearer {token}é"}).status == 403


def test_status_endpoint_ignores_a_short_token(prod_app, monkeypatch):
    monkeypatch.setenv("JONGO_STATUS_TOKEN", "short")
    client = prod_app.test_client()
    assert client.get("/_jongo/status", headers={"Authorization": "Bearer short"}).status == 403
    monkeypatch.setenv("JONGO_STATUS_TOKEN", "")
    assert client.get("/_jongo/status", headers={"Authorization": "Bearer "}).status == 403


def test_status_endpoint_can_be_switched_off(prod_app, monkeypatch):
    monkeypatch.setenv("JONGO_STATUS", "off")
    prod_app.dev = True
    assert prod_app.test_client().get("/_jongo/status").status == 404


def test_status_requests_are_not_recorded(prod_app):
    prod_app.dev = True
    client = prod_app.test_client()
    client.get("/_jongo/status")
    client.get("/_jongo/status")
    assert jongo.status(prod_app)["requests"]["total"] == 0


# ==================================================================================== doctor ==


def _ids(report):
    return {c["id"]: c for c in report["checks"]}


def test_doctor_report_shape_for_a_healthy_dev_app(tmp_path, monkeypatch):
    # Other test modules register deliberately broken server functions in this process.
    monkeypatch.setattr(jongo.rpc, "SERVER_FUNCTIONS", {})
    app = Jongo("diag_doc_ok", root=tmp_path, dev=True, secret_key="k" * 40, database=":memory:")
    db.migrate()  # this process has every test module's models registered
    report = jongo.doctor(app)
    assert report["ok"], [c for c in report["checks"] if c["status"] == "fail"]
    assert not report["production"]
    for c in report["checks"]:
        assert set(c) >= {"id", "status", "message", "hint"} and c["status"] in ("ok", "warn", "fail")
    checks = _ids(report)
    for key in ("python", "jongo", "app", "dev_mode", "secret_key", "components", "server_functions", "database",
                "migrations", "static", "node"):
        assert key in checks, key
    assert checks["database"]["status"] == "ok"


@pytest.mark.parametrize("secret, source, status", [
    ("short", "argument", "fail"),
    ("k" * 40, "argument", "ok"),
])
def test_doctor_judges_the_secret_key_for_production(tmp_path, secret, source, status):
    app = Jongo("diag_doc_secret", root=tmp_path, secret_key=secret, database=":memory:")
    check_ = _ids(jongo.doctor(app, production=True))["secret_key"]
    assert check_["status"] == status
    assert "short" not in check_["message"] and ("k" * 40) not in check_["message"], "never print the key"
    if status == "fail":
        assert check_["code"] == "J501" and check_["hint"]


def test_doctor_file_secret_and_proxy_warnings_in_production(tmp_path, monkeypatch):
    monkeypatch.delenv("JONGO_SECRET_KEY", raising=False)
    app = Jongo("diag_doc_file", root=tmp_path, database=":memory:")
    checks = _ids(jongo.doctor(app, production=True))
    assert checks["secret_key"]["status"] == "warn" and checks["secret_key"]["code"] == "J501"
    assert checks["cookies"]["status"] == "warn" and checks["cookies"]["code"] == "J507"
    app.trust_proxy = True
    assert _ids(jongo.doctor(app, production=True))["cookies"]["status"] == "ok"


def test_doctor_fails_dev_mode_when_judged_as_production(tmp_path):
    app = Jongo("diag_doc_dev", root=tmp_path, dev=True, secret_key="k" * 40, database=":memory:")
    report = jongo.doctor(app, production=True)
    assert not report["ok"] and _ids(report)["dev_mode"]["code"] == "J502"


def test_doctor_production_detection(tmp_path, monkeypatch):
    from jongo.diagnostics import is_production_environment

    app = Jongo("diag_doc_env", root=tmp_path, secret_key="k" * 40, database=":memory:")
    for name in ("JONGO_ENV", "RAILWAY_ENVIRONMENT", "DYNO", "FLY_APP_NAME", "K_SERVICE", "RENDER", "VERCEL_ENV"):
        monkeypatch.delenv(name, raising=False)
    assert not is_production_environment(app)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    assert is_production_environment(app)
    monkeypatch.setenv("JONGO_ENV", "development")
    assert not is_production_environment(app)
    app.dev = True
    monkeypatch.setenv("JONGO_ENV", "production")
    assert not is_production_environment(app)


def test_doctor_reports_pending_migrations_and_unreachable_databases(tmp_path):
    database = tmp_path / "old.sqlite3"
    raw = sqlite3.connect(database)
    raw.execute("CREATE TABLE diag_old (id INTEGER PRIMARY KEY)")
    raw.commit()
    raw.close()

    class DiagOld(db.Model):
        name = db.Text()

        class Meta:
            table = "diag_old"

    try:
        app = Jongo("diag_doc_db", root=tmp_path, dev=True, secret_key="k" * 40, database=str(database))
        migrations = _ids(jongo.doctor(app))["migrations"]
        assert migrations["status"] == "warn" and migrations["code"] == "J302"
        assert any("diag_old" in op for op in migrations["pending"])

        db.configure(str(tmp_path / "a-file" / "x.sqlite3"))
        (tmp_path / "a-file").write_text("not a directory")
        database_check = _ids(jongo.doctor(app))["database"]
        assert database_check["status"] == "fail" and database_check["code"] == "J301"
    finally:
        db.models_registry.pop("DiagOld", None)
        db.configure(":memory:")


def test_doctor_does_not_create_the_database(tmp_path):
    app = Jongo("diag_doc_lazy", root=tmp_path, dev=True, secret_key="k" * 40, database="new/app.sqlite3")
    try:
        report = jongo.doctor(app)
        assert _ids(report)["database"]["status"] == "ok"
        assert not (tmp_path / "new").exists()
    finally:
        db.configure(":memory:")


def test_doctor_warns_about_a_missing_static_directory(tmp_path):
    app = Jongo("diag_doc_static", root=tmp_path, dev=True, secret_key="k" * 40, database=":memory:",
                head=['<link rel="stylesheet" href="/static/site.css">'])
    assert _ids(jongo.doctor(app))["static"]["code"] == "J506"
    (tmp_path / "static").mkdir()
    assert _ids(jongo.doctor(app))["static"]["status"] == "ok"


def test_doctor_port_check(tmp_path):
    import socket

    from jongo.diagnostics import environment_checks

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        busy = {c["id"]: c for c in environment_checks(port)}["port"]
    assert busy["status"] == "warn" and busy["code"] == "J505"


def test_doctor_admin_without_admins(prod_app):
    checks = _ids(jongo.doctor(prod_app))
    assert checks["admin"]["code"] == "J510"
    auth.User.create_user("root", "pw", is_admin=True)
    assert _ids(jongo.doctor(prod_app))["admin"]["status"] == "ok"


# ------------------------------------------------------------------------------------------ CLI


GOOD_APP = """
from jongo import Jongo, component, server, state
from jongo.html import *

app = Jongo(__name__, database=":memory:", secret_key="k" * 40)

@app.page("/")
def home():
    return p("hi")
"""

BROKEN_COMPONENT = GOOD_APP + """
@component
def Widget():
    import math
    return p(math.pi)
"""

BROKEN_ROUTE = GOOD_APP + """
@app.page("/x/<integer:id>")
def x(id):
    return p(id)
"""


def test_doctor_cli_healthy(tmp_path):
    (tmp_path / "app.py").write_text(GOOD_APP)
    proc = run_cli("doctor", cwd=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "✓ python" in proc.stdout and "healthy" in proc.stdout
    report = json.loads(run_cli("doctor", "--json", cwd=tmp_path).stdout)
    assert report["ok"] is True


def test_doctor_cli_compile_failure_exits_1_with_location(tmp_path):
    (tmp_path / "app.py").write_text(BROKEN_COMPONENT)
    proc = run_cli("doctor", "--json", cwd=tmp_path)
    assert proc.returncode == 1
    failed = [c for c in json.loads(proc.stdout)["checks"] if c["status"] == "fail"]
    assert failed[0]["code"] == "J101" and failed[0]["file"] == "app.py" and failed[0]["line"] == 13
    text = run_cli("doctor", cwd=tmp_path)
    assert text.returncode == 1 and "[J101]" in text.stdout and "hint:" in text.stdout


def test_doctor_cli_production_flag(tmp_path):
    (tmp_path / "app.py").write_text(GOOD_APP.replace('"k" * 40', '"weak"'))
    assert run_cli("doctor", cwd=tmp_path).returncode == 0
    proc = run_cli("doctor", "--production", "--json", cwd=tmp_path)
    assert proc.returncode == 1 and _ids(json.loads(proc.stdout))["secret_key"]["code"] == "J501"


def test_cli_load_errors_have_codes(tmp_path):
    missing = run_cli("check", cwd=tmp_path)
    assert missing.returncode == 1 and "[J503]" in missing.stderr and "hint:" in missing.stderr
    (tmp_path / "app.py").write_text("x = 1\n")
    no_app = run_cli("doctor", "--json", cwd=tmp_path)
    assert no_app.returncode == 1 and _ids(json.loads(no_app.stdout))["app"]["code"] == "J504"
    (tmp_path / "app.py").write_text(BROKEN_ROUTE)
    proc = run_cli("check", "--json", cwd=tmp_path)
    problem = json.loads(proc.stdout)["problems"][0]
    assert proc.returncode == 1 and problem["kind"] == "load" and problem["code"] == "J201"
    assert problem["file"] == "app.py" and problem["line"] == 11
    (tmp_path / "app.py").write_text("import nonexistent_module_xyz\n")
    crash = run_cli("check", cwd=tmp_path)
    assert crash.returncode == 1 and "[J509]" in crash.stderr and "app.py:1" in crash.stderr


def test_check_cli_shows_codes(tmp_path):
    (tmp_path / "app.py").write_text(BROKEN_COMPONENT)
    report = json.loads(run_cli("check", "--json", cwd=tmp_path).stdout)
    assert report["problems"][0]["code"] == "J101"
    text = run_cli("check", cwd=tmp_path).stdout
    assert "[J101]" in text and "jongo explain" in text


def test_status_cli_against_a_running_server(tmp_path):
    import importlib
    from wsgiref.simple_server import WSGIRequestHandler

    # Importing the jongo.server *module* rebinds the `jongo.server` attribute (the decorator)
    # to it; put the decorator back so later `from jongo import server` still works.
    ThreadingWSGIServer = importlib.import_module("jongo.server").ThreadingWSGIServer
    jongo.server = jongo.rpc.server

    class Quiet(WSGIRequestHandler):
        def log_message(self, *args):
            pass

    app = Jongo("diag_status_cli", root=tmp_path, dev=True, secret_key="k" * 40, database=":memory:")

    @app.page("/hello")
    def hello():
        return p("hi")

    httpd = ThreadingWSGIServer(("127.0.0.1", 0), Quiet)
    httpd.set_app(app)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_port}"
    try:
        app.test_client().get("/hello")
        text = run_cli("status", "--url", url, cwd=tmp_path)
        assert text.returncode == 0, text.stderr
        assert "diag_status_cli" in text.stdout and "/hello" in text.stdout
        data = json.loads(run_cli("status", "--url", url, "--json", cwd=tmp_path).stdout)
        assert data["requests"]["total"] >= 1
        app.dev = False
        refused = run_cli("status", "--url", url, cwd=tmp_path, env={"JONGO_STATUS_TOKEN": ""})
        assert refused.returncode == 1 and "[J508]" in refused.stderr
        token = "s" * 32
        os.environ["JONGO_STATUS_TOKEN"] = token
        try:
            allowed = run_cli("status", "--url", url, "--json", "--token", token, cwd=tmp_path)
        finally:
            del os.environ["JONGO_STATUS_TOKEN"]
        assert allowed.returncode == 0 and json.loads(allowed.stdout)["mode"] == "production"
    finally:
        httpd.shutdown()
        httpd.server_close()
    gone = run_cli("status", "--url", url, "--json", cwd=tmp_path, timeout=30)
    assert gone.returncode == 1 and json.loads(gone.stdout)["error"]["code"] == "J512"


# ======================================================================== agent-facing docs ==


def test_context_pack_and_scaffold_point_at_the_diagnostics(tmp_path):
    text = jongo.context(diag_app)
    for command in ("jongo doctor", "jongo explain", "jongo status"):
        assert command in text
    scaffold = (PACKAGE / "scaffold" / "AGENTS.md.tmpl").read_text()
    for command in ("jongo doctor --json", "jongo explain", "jongo status --json"):
        assert command in scaffold


def test_public_api():
    assert callable(jongo.doctor) and callable(jongo.status) and callable(jongo.explain)
    assert jongo.__version__ == "0.5.1"
