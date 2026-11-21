"""Regression tests for 0.7.0: the CLI, the agent surfaces, the scaffold and the docs.

What these pin: a command run with --json prints JSON even when it fails; an ordinary
CLI mistake (a port out of range, a host the machine doesn't have, an unwritable -o path,
an over-long project name) is a coded message, never a traceback; `jongo sessions --revoke`
says when nothing was revoked; `jongo new` writes a test setup that keeps tests off the
app's real database; and the docs say what the code does.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from jongo import ratelimit
from jongo.diagnostics import doctor
from jongo.errors import CATALOGUE, JongoError

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "jongo"
GUIDE = (ROOT / "docs" / "guide.md").read_text()
README = (ROOT / "README.md").read_text()

APP = textwrap.dedent('''
    from jongo import Jongo, db, server
    from jongo.html import div

    app = Jongo(__name__, title="Shop", database="shop.sqlite3")


    class Item(db.Model):
        name = db.Text(max_length=50)


    @server
    def add(name: str) -> dict:
        return Item.create(name=name).to_dict()


    @app.page("/")
    def home():
        return div("shop")
''')


def run_cli(*args, cwd, env=None, timeout=60):
    environment = {**os.environ, "NO_COLOR": "1", "JONGO_TRACEBACK": "0"}
    for name in ("JONGO_DEV", "JONGO_ENV", "JONGO_APP", "JONGO_STATUS_TOKEN", "JONGO_DATABASE", "PORT"):
        environment.pop(name, None)
    environment.update(env or {})
    return subprocess.run([sys.executable, "-m", "jongo", *args], cwd=cwd, capture_output=True, text=True,
                          env=environment, timeout=timeout)


def no_traceback(proc):
    assert "Traceback (most recent call last)" not in proc.stderr, proc.stderr
    return proc


@pytest.fixture
def project(tmp_path):
    (tmp_path / "app.py").write_text(APP)
    (tmp_path / "broken.py").write_text("from jongo import\n")
    return tmp_path


# ============================================================ --json always prints JSON ==


def json_error(proc) -> dict:
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    data = json.loads(proc.stdout)
    assert data["ok"] is False and set(data["error"]) >= {"code", "message", "hint", "file", "line"}
    return data["error"]


@pytest.mark.parametrize("command", [["routes", "--json"], ["context", "--json"], ["sessions", "--json"]])
def test_json_commands_report_a_broken_app_as_json(project, command):
    error = json_error(no_traceback(run_cli(*command, "broken.py", cwd=project)))
    assert error["code"] == "J509" and error["file"] == "broken.py" and error["line"] == 1
    assert "SyntaxError" in error["message"]


@pytest.mark.parametrize("command", [["routes", "--json"], ["context", "--json"], ["sessions", "--json"]])
def test_json_commands_report_a_missing_app_as_json(project, command):
    error = json_error(run_cli(*command, "missing.py", cwd=project))
    assert error["code"] == "J503" and "missing.py" in error["message"] and error["hint"]


def test_sessions_json_reports_an_unknown_user_as_json(project):
    error = json_error(run_cli("sessions", "--json", "--user", "nobody", cwd=project))
    assert error["message"] == "no user called 'nobody'" and error["code"] is None


def test_explain_json_reports_an_unknown_code_as_json(tmp_path):
    error = json_error(run_cli("explain", "--json", "J999", cwd=tmp_path))
    assert "J999" in error["message"] and "--list" in error["hint"]


def test_the_json_success_paths_are_unchanged(project):
    routes = run_cli("routes", "--json", cwd=project)
    assert routes.returncode == 0 and json.loads(routes.stdout)["pages"][0]["path"] == "/"
    explain = run_cli("explain", "--json", "J214", cwd=project)
    assert explain.returncode == 0 and json.loads(explain.stdout)["title"] == "Host not allowed"


def test_without_json_failures_still_go_to_stderr(project):
    proc = run_cli("routes", "missing.py", cwd=project)
    assert proc.returncode == 1 and proc.stdout == "" and "[J503]" in proc.stderr


# ============================================================ no tracebacks for mistakes ==


@pytest.mark.parametrize("command", [
    ["run", "--port", "99999"], ["run", "--port", "-1"], ["run", "--port", "eighty"],
    ["dev", "--no-reload", "--port", "70000"], ["doctor", "--port", "65536"],
])
def test_a_port_out_of_range_is_refused_before_anything_starts(project, command):
    proc = no_traceback(run_cli(*command, cwd=project))
    assert proc.returncode == 2 and "isn't a port number" in proc.stderr, proc.stderr


def test_a_bad_port_variable_only_bothers_jongo_run(project):
    assert run_cli("check", "--json", cwd=project, env={"PORT": "abc"}).returncode == 0
    proc = no_traceback(run_cli("run", cwd=project, env={"PORT": "abc"}))
    assert proc.returncode == 2 and "'abc' isn't a port number" in proc.stderr


def test_a_host_this_machine_doesnt_have_is_j505(project):
    # 192.0.2.1 is TEST-NET-1: never an address of this machine, and no DNS lookup is made.
    proc = no_traceback(run_cli("run", "--host", "192.0.2.1", "--port", "10391", cwd=project))
    assert proc.returncode == 3, proc.stderr
    assert "[J505] can't listen on 192.0.2.1:10391" in proc.stderr and "jongo explain J505" in proc.stderr


def test_a_port_in_use_is_still_j505(project):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        proc = no_traceback(run_cli("run", "--host", "127.0.0.1", "--port", str(port), cwd=project))
    assert proc.returncode == 3 and "J505" in proc.stderr


def test_doctor_reports_a_bad_port_instead_of_raising():
    check = next(c for c in doctor(None, port=99999)["checks"] if c["id"] == "port")
    assert check["status"] == "fail" and check["code"] == "J505"


@pytest.mark.parametrize("command", [["context", "-o"], ["openapi", "-o"], ["context", "--json", "-o"]])
def test_an_unwritable_output_path_is_j515(project, command):
    target = str(project / "no" / "such" / "dir" / "out.txt")
    proc = no_traceback(run_cli(*command, target, cwd=project))
    if "--json" in command:
        assert json_error(proc)["code"] == "J515"
    else:
        assert proc.returncode == 1 and f"[J515] can't write {target}" in proc.stderr


def test_jongo_new_with_a_name_the_file_system_refuses_is_j515(tmp_path):
    proc = no_traceback(run_cli("new", "x" * 300, cwd=tmp_path))
    assert proc.returncode == 1 and "[J515]" in proc.stderr


def test_jongo_new_over_an_existing_file_is_a_message(tmp_path):
    (tmp_path / "taken").write_text("a file")
    proc = no_traceback(run_cli("new", "taken", cwd=tmp_path))
    assert proc.returncode == 1 and "is a file" in proc.stderr


def test_jongo_build_into_an_unwritable_place_is_j515(project):
    (project / ".jongo").write_text("a file where the build directory should be")
    proc = no_traceback(run_cli("build", cwd=project))
    assert proc.returncode == 1 and "[J515]" in proc.stderr


def test_jongo_dev_on_another_interface_explains_the_host_check(project):
    # The note comes before the server binds; 192.0.2.1 can't be bound, so nothing listens.
    proc = no_traceback(run_cli("dev", "--no-reload", "--host", "192.0.2.1", "--port", "10392", cwd=project))
    assert "[J214]" in proc.stderr and "allowed_hosts" in proc.stderr, proc.stderr
    assert proc.returncode == 3 and "[J505]" in proc.stderr


# ======================================================================= Bug 8 nits ==


def test_revoking_a_session_that_doesnt_exist_fails(project):
    proc = run_cli("sessions", "--revoke", "99999", cwd=project)
    assert proc.returncode == 1 and "no active session #99999" in proc.stderr and "✓" not in proc.stdout
    error = json_error(run_cli("sessions", "--revoke", "99999", "--json", cwd=project))
    assert "nothing was revoked" in error["message"]


def test_revoking_a_real_session_still_works(project):
    script = textwrap.dedent('''
        import app
        from jongo import auth, db
        db.migrate([auth.User, auth.LoginSession, app.Item])
        user = auth.User.create_user("ann", "pw-123456")
        print(auth.LoginSession.create(user_id=user.id, key="k" * 64).id)
    ''')
    made = subprocess.run([sys.executable, "-c", script], cwd=project, capture_output=True, text=True, timeout=60)
    assert made.returncode == 0, made.stderr
    session = made.stdout.strip().splitlines()[-1]
    listed = json.loads(run_cli("sessions", "--json", cwd=project).stdout)["sessions"]
    assert [str(s["id"]) for s in listed] == [session] and listed[0]["username"] == "ann"
    proc = run_cli("sessions", "--revoke", session, "--json", cwd=project)
    assert proc.returncode == 0 and json.loads(proc.stdout) == {"revoked": 1, "session": int(session)}
    assert json.loads(run_cli("sessions", "--json", cwd=project).stdout)["sessions"] == []


@pytest.mark.parametrize("spec", [("5", "60"), (5, "60"), (True, 60), (5, float("nan")), (float("inf"), 60),
                                  (0, 60), (5, 0)])
def test_a_rate_limit_pair_that_isnt_two_numbers_is_j513(spec):
    with pytest.raises(JongoError) as refused:
        ratelimit.parse(spec)
    assert refused.value.code == "J513" and refused.value.hint


def test_a_numeric_rate_limit_pair_still_parses():
    assert ratelimit.parse((5, 60)) == ratelimit.RateLimit(5, 60)
    assert ratelimit.parse([2.0, 30]) == ratelimit.RateLimit(2, 30)


def test_jongo_get_with_a_bad_path_hints_at_the_path_not_at_json_rpc():
    from jongo import Jongo
    from jongo.mcp import McpServer

    app = Jongo("cli_070_mcp", secret_key="k" * 40)
    mcp = McpServer(app, tools=None)
    for bad in ("relative", "//evil.example/x", 5):
        error = mcp.call("jongo_get", {"path": bad})["structuredContent"]["error"]
        assert error["code"] == "J422" and "path" in error["hint"] and "JSON-RPC" not in error["hint"]
    error = mcp.call("jongo_describe", {"section": "nope"})["structuredContent"]["error"]
    assert "section" in error["hint"] and "JSON-RPC" not in error["hint"]


# ==================================================================== the scaffold ==


def test_jongo_new_writes_tests_that_keep_off_the_real_database(tmp_path):
    proc = run_cli("new", "site", cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    site = tmp_path / "site"
    conftest = (site / "tests" / "conftest.py").read_text()
    assert 'db.configure(":memory:")' in conftest and "db.migrate()" in conftest and "autouse=True" in conftest
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    for name in ("JONGO_DEV", "JONGO_ENV", "JONGO_DATABASE", "JONGO_APP"):
        env.pop(name, None)
    tests = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=site,
                           capture_output=True, text=True, env=env, timeout=120)
    assert tests.returncode == 0, tests.stdout + tests.stderr
    assert re.search(r"\b3 passed\b", tests.stdout), tests.stdout
    assert not (site / "db.sqlite3").exists(), "the scaffold's tests wrote to the app's real database"


def test_every_scaffold_file_is_shipped_in_the_wheel():
    """pyproject's package-data must match every scaffold file, or `jongo new` breaks once installed."""
    text = (ROOT / "pyproject.toml").read_text()
    globs = re.findall(r'"([^"]+)"', re.search(r"\[tool\.setuptools\.package-data\]\s*jongo = \[([^\]]*)\]",
                                               text).group(1))
    for path in (PACKAGE / "scaffold").rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            relative = path.relative_to(PACKAGE)
            assert any(relative.match(g) and len(relative.parts) == len(Path(g).parts) for g in globs), relative


# ======================================================================== the docs ==


def test_the_new_codes_are_catalogued_and_documented():
    for code in ("J214", "J515"):
        assert code in CATALOGUE and f"| `{code}` | {CATALOGUE[code]['title']} |" in GUIDE
    assert CATALOGUE["J214"]["title"] == "Host not allowed"
