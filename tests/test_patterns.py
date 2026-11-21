"""Every pattern in examples/patterns must import, migrate, render and compile.

These are the examples people copy. A broken example is worse than a missing one, so
each file is loaded in isolation, its schema is created, its first page is rendered on
the server and its components are compiled to JavaScript. Where a pattern teaches a
behaviour (a safe redirect after sign-in, the newest chat messages, a test suite that
passes), the behaviour itself is checked too.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import use_test_database
from jongo import Jongo, db, rpc, styles, vdom
from jongo.testing import TestClient

PATTERN_DIR = Path(__file__).parent.parent / "examples" / "patterns"
PATTERNS = sorted(PATTERN_DIR.glob("*.py"))
assert PATTERNS, "no patterns found"


def load(path: Path):
    spec = importlib.util.spec_from_file_location(f"pattern_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def isolated_registry():
    # Every registry is global by name, so a pattern's components, server functions and
    # styles would otherwise stay registered and be compiled into later tests' apps.
    registries = [db.models_registry, vdom.COMPONENTS, rpc.SERVER_FUNCTIONS, styles._REGISTRY]
    saved = [dict(r) for r in registries]
    db.models_registry.clear()
    use_test_database()
    yield
    db.close_connections()
    for registry, before in zip(registries, saved):
        registry.clear()
        registry.update(before)


@pytest.mark.parametrize("path", PATTERNS, ids=lambda p: p.stem)
def test_pattern_runs(path):
    module = load(path)
    app = getattr(module, "app", None)
    assert isinstance(app, Jongo), f"{path.name} defines no module-level `app = Jongo(__name__)`"

    db.migrate()
    client = TestClient(app)

    response = client.get("/")
    assert response.status == 200, f"{path.name}: GET / returned {response.status}"

    bundle = client.get("/_jongo/app.js")
    assert bundle.status == 200, f"{path.name}: components did not compile"
    assert len(bundle.text) > 1000


@pytest.mark.parametrize("path", PATTERNS, ids=lambda p: p.stem)
def test_pattern_explains_itself(path):
    """Each pattern opens with a docstring saying what it is and what to notice."""
    text = path.read_text()
    assert text.startswith('"""'), f"{path.name} has no module docstring"
    assert "What to notice" in text, f"{path.name} does not say what to notice"


# -- what the patterns teach ----------------------------------------------------------------

def migrate_with_auth():
    """The pattern's tables plus jongo.auth's (the registry fixture hides models imported earlier)."""
    from jongo import auth

    db.migrate()
    db.migrate([auth.User, auth.LoginSession])


_NUMBER_WORDS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                 "fifteen sixteen seventeen eighteen nineteen twenty").split()


def test_the_patterns_readme_counts_the_patterns():
    """The README's opening count word and its table both match the files on disk."""
    text = (PATTERN_DIR / "README.md").read_text()
    files = [p for p in PATTERNS if re.match(r"\d\d_", p.name)]
    first = re.search(r"^([A-Z][a-z]+) complete, runnable apps", text, re.M)
    assert first, "examples/patterns/README.md no longer opens with '<Number> complete, runnable apps'"
    assert first.group(1).lower() == _NUMBER_WORDS[len(files)], (first.group(1), len(files))
    rows = re.findall(r"^\| (\d\d) \| \[[^\]]+\]\((\d\d_\w+\.py)\)", text, re.M)
    assert sorted(name for _, name in rows) == sorted(p.name for p in files)


def test_the_testing_pattern_passes_its_own_tests(tmp_path):
    """12_testing.py says `pytest examples/patterns/12_testing.py`; that command must pass."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    for name in ("JONGO_DEV", "JONGO_ENV", "JONGO_DATABASE", "JONGO_APP"):
        env.pop(name, None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(tmp_path),
         str(PATTERN_DIR / "12_testing.py")],
        cwd=tmp_path, capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert re.search(r"\b[1-9]\d* passed\b", proc.stdout) and "error" not in proc.stdout.splitlines()[-1]
    assert not list(tmp_path.glob("*.sqlite3")), "the tests wrote a database file instead of using :memory:"


@pytest.mark.parametrize("target", ["https://evil.example/phish", "//evil.example", "/\\evil.example",
                                    "/\t/evil.example", "javascript:alert(1)"])
def test_the_auth_pattern_never_redirects_off_site(target):
    module = load(PATTERN_DIR / "02_auth.py")
    migrate_with_auth()
    from jongo import auth

    auth.User.create_user("ann", "ann-password-1")
    client = TestClient(module.app)
    page = client.get("/login?next=" + target.replace("\t", "%09"))
    hidden = re.search(r'name="next" value="([^"]*)"', page.text)
    assert hidden and hidden.group(1) == "/", f"the sign-in form carries next={hidden and hidden.group(1)!r}"
    response = client.post("/login", data={"username": "ann", "password": "ann-password-1", "next": target})
    assert response.status == 303 and response.headers["location"] == "/", response.headers.get("location")


def test_the_auth_pattern_keeps_a_local_next_and_passes_the_request(monkeypatch):
    module = load(PATTERN_DIR / "02_auth.py")
    migrate_with_auth()
    from jongo import auth
    from jongo.ratelimit import TooManyRequests

    auth.User.create_user("ann", "ann-password-1")
    calls = []
    real = module.authenticate

    def spy(username, password, request=None):
        calls.append(request)
        return real(username, password, request)

    monkeypatch.setattr(module, "authenticate", spy)  # the name the pattern calls
    client = TestClient(module.app)
    assert client.get("/account").headers["location"] == "/login?next=%2Faccount"
    wrong = client.post("/login", data={"username": "ann", "password": "nope", "next": "/account"})
    assert wrong.status == 303 and wrong.headers["location"] == "/login?error=1&next=/account"
    assert "Wrong username or password." in client.get(wrong.headers["location"]).text
    right = client.post("/login", data={"username": "ann", "password": "ann-password-1", "next": "/account"})
    assert right.status == 303 and right.headers["location"] == "/account"
    assert client.get("/account").status == 200
    # the request reaches authenticate(), so wrong passwords are counted per address as well
    assert len(calls) == 2 and all(request is not None for request in calls)

    def throttled(username, password, request=None):
        raise TooManyRequests(retry_after=60)

    monkeypatch.setattr(module, "authenticate", throttled)
    over = TestClient(module.app).post("/login", data={"username": "ann", "password": "x", "next": "/account"})
    assert over.status == 303 and over.headers["location"] == "/login?error=throttled&next=/account"
    assert "Too many attempts" in TestClient(module.app).get(over.headers["location"]).text


def test_the_sessions_pattern_refuses_a_case_variant_username():
    module = load(PATTERN_DIR / "16_sessions_and_limits.py")
    migrate_with_auth()
    from jongo import ServerError

    TestClient(module.app).rpc(module.sign_up, "ann", "password-1")
    with pytest.raises(ServerError) as taken:
        TestClient(module.app).rpc(module.sign_up, "Ann", "password-2")
    assert taken.value.errors == {"username": "That username is taken."}


def test_the_chat_pattern_shows_the_newest_messages():
    module = load(PATTERN_DIR / "06_live_chat.py")
    db.migrate()
    for i in range(60):
        module.Message.create(who="u", text=f"msg{i:02d}")
    html = TestClient(module.app).get("/").text
    assert "msg59" in html and "msg10" in html, "the newest 50 messages must be on the page"
    assert "msg09" not in html, "only 50 messages are rendered"
    assert html.index("msg10") < html.index("msg59"), "oldest first, newest at the bottom"
