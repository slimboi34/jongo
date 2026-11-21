"""0.7.0 fixes to auth, rate limiting, @server argument checking and the OpenAPI body.

Each section is one confirmed finding from the 0.6.0 stress test:

1. A look-alike account ("Ann") could clear the login throttle of "ann".
2. rate_limit= counted a signed-in caller by user only, so a sign-up loop never got a 429.
4. User.create(password=...) and assign-then-save stored the raw password.
6. Filling the shared limiter with fresh keys evicted a throttled caller's count.
7. login()/logout() kept the CSRF token minted before sign-in.
8. A user with no password answered a wrong guess ~1000x faster than an unknown username.
9. Two processes signing in at once after an upgrade raced to create jongo_sessions.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from conftest import sqlite_only, use_test_database
from jongo import HTTPError, Jongo, auth, db, ratelimit, rpc, server
from jongo.http import CSRF_COOKIE, Request
from jongo.ratelimit import Limiter, RateLimit, TooManyRequests
from jongo.testing import TestClient

HERE = Path(__file__).parent
ROOT = HERE.parent


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    saved_models = dict(db.models_registry)
    saved_functions = dict(rpc.SERVER_FUNCTIONS)
    db.models_registry.clear()
    db.models_registry.update({"User": auth.User, "LoginSession": auth.LoginSession})
    rpc.SERVER_FUNCTIONS.clear()
    use_test_database()
    monkeypatch.setattr(auth, "ITERATIONS", 1000)
    ratelimit.limiter.reset()
    auth.limiter.reset()
    yield
    ratelimit.limiter.reset()
    auth.limiter.reset()
    db.close_connections()
    db.models_registry.clear()
    db.models_registry.update(saved_models)
    rpc.SERVER_FUNCTIONS.clear()
    rpc.SERVER_FUNCTIONS.update(saved_functions)


def make_app(**options):
    app = Jongo("fixes_070_auth", root=HERE, secret_key="k" * 40, **options)
    db.migrate()
    return app


def post(client, fn, *args, headers=None, **kwargs):
    """A raw server call: the response, without TestClient.rpc turning errors into exceptions."""
    return client.post(f"/_jongo/rpc/{fn.id}", json={"args": list(args), "kwargs": kwargs}, headers=headers)


# ============================================================ 1. exact login-throttle key ==


def test_a_look_alike_account_cannot_clear_another_accounts_login_throttle():
    """9 wrong guesses on ann, a correct login as Ann, then 2 more guesses on ann: the 2nd is a 429."""
    app = make_app()

    @server
    def sign_in(request, username: str, password: str) -> bool:
        user = auth.authenticate(username, password, request)
        if user is None:
            raise HTTPError(400, "Wrong username or password")
        auth.login(request, user)
        return True

    auth.User.create_user("ann", "victim-secret")
    auth.User.create_user("Ann", "attacker-pw")          # the lookup is exact, so this is another account
    client = TestClient(app)
    assert [post(client, sign_in, "ann", f"guess{i}").status for i in range(9)] == [400] * 9
    assert client.rpc(sign_in, "Ann", "attacker-pw") is True
    client.logout()
    assert post(client, sign_in, "ann", "guess9").status == 400
    refused = post(client, sign_in, "ann", "guess10")
    assert refused.status == 429 and refused.json()["error"]["code"] == "J213", \
        "Ann's correct password must not have reset ann's count"


def test_the_throttle_key_is_the_exact_username(monkeypatch):
    monkeypatch.setattr(auth, "LOGIN_ATTEMPTS", 3)
    db.migrate()
    auth.User.create_user("ann", "right")
    auth.User.create_user("Ann", "other")
    assert [auth.authenticate("ann", g) for g in "abc"] == [None] * 3
    with pytest.raises(TooManyRequests):
        auth.authenticate("ann", "right")
    assert auth.authenticate("Ann", "other").username == "Ann", "a different exact name has its own count"
    with pytest.raises(TooManyRequests):
        auth.authenticate("ann", "right")                 # ...and Ann signing in didn't clear ann's
    assert auth.authenticate("ANN", "right") is None, "a variant spelling is not ann's account"


def test_a_correct_password_clears_only_its_own_count(monkeypatch):
    monkeypatch.setattr(auth, "LOGIN_ATTEMPTS", 3)
    db.migrate()
    auth.User.create_user("ann", "right")
    auth.User.create_user("Ann", "other")
    auth.authenticate("ann", "x"), auth.authenticate("ann", "y")
    auth.authenticate("Ann", "x"), auth.authenticate("Ann", "y")
    assert auth.authenticate("ann", "right").username == "ann"
    assert [auth.authenticate("ann", g) for g in "abc"] == [None] * 3, "ann's count started again"
    assert auth.authenticate("Ann", "z") is None
    with pytest.raises(TooManyRequests):
        auth.authenticate("Ann", "other")                 # Ann's three failures still stand


# ================================================= 2. rate limits count address and user ==


def test_a_sign_up_loop_from_one_address_is_refused_on_the_sixth_call():
    app = make_app()
    created = []

    @server(rate_limit="5/minute")
    def sign_up(request, username: str) -> int:
        user = auth.User.create_user(username, "pw")
        auth.login(request, user)                         # each call is signed in as the newest account
        created.append(user.id)
        return user.id

    client = TestClient(app)
    assert [post(client, sign_up, f"user{i}").status for i in range(5)] == [200] * 5
    refused = post(client, sign_up, "user5")
    assert refused.status == 429 and refused.json()["error"]["code"] == "J213"
    assert len(created) == 5, "the refused call did not run"


def test_a_refused_request_is_not_charged_to_the_other_identity():
    app = make_app()

    @server(rate_limit="2/minute")
    def ping(request) -> str:
        return "pong"

    ann = auth.User.create_user("ann", "pw")
    anonymous, signed_in = TestClient(app), TestClient(app)
    signed_in.login(ann)
    assert [post(anonymous, ping).status for _ in range(2)] == [200, 200]   # the address is now full
    assert post(signed_in, ping).status == 429, "the same address, so the same address counter"
    user_key = f"@server {ping.id}|user:{ann.id}"
    assert ratelimit.limiter.peek(user_key, RateLimit(1, 60)) == (True, 0), "ann was refunded"


def test_the_same_user_from_another_address_is_still_counted_per_user():
    app = make_app(trust_proxy=True)

    @server(rate_limit="2/minute")
    def ping(request) -> str:
        return "pong"

    ann = auth.User.create_user("ann", "pw")
    client = TestClient(app)
    client.login(ann)
    statuses = [post(client, ping, headers={"X-Forwarded-For": f"10.0.0.{i}"}).status for i in range(3)]
    assert statuses == [200, 200, 429], "switching address doesn't reset the user's window"


def test_address_factor_loosens_or_turns_off_the_address_side(monkeypatch):
    app = make_app()

    @server(rate_limit="2/minute")
    def ping(request) -> str:
        return "pong"

    users = [auth.User.create_user(f"user{i}", "pw") for i in range(4)]
    clients = []
    for user in users:
        clients.append(TestClient(app))
        clients[-1].login(user)

    monkeypatch.setattr(ratelimit, "ADDRESS_FACTOR", 3)  # an office: the address may make 6 calls
    statuses = [post(c, ping).status for c in clients for _ in range(2)]
    assert statuses == [200] * 6 + [429, 429]
    ratelimit.limiter.reset()
    monkeypatch.setattr(ratelimit, "ADDRESS_FACTOR", None)  # 0.6 behaviour: users only
    assert [post(c, ping).status for c in clients for _ in range(2)] == [200] * 8


def test_client_keys_and_an_explicit_key():
    class Req:
        client_ip = "203.0.113.9"
        user = None

    request = Req()
    assert ratelimit.client_keys(request) == ["ip:203.0.113.9"]
    assert ratelimit.client_key(request) == "ip:203.0.113.9"
    request.user = type("U", (), {"id": 42})()
    assert ratelimit.client_keys(request) == ["ip:203.0.113.9", "user:42"]
    assert ratelimit.client_key(request) == "user:42", "unchanged, for code that reads it"

    for _ in range(2):
        ratelimit.check(request, "2/minute", "reset", key="ann@example.com")
    with pytest.raises(TooManyRequests):
        ratelimit.check(request, "2/minute", "reset", key="ann@example.com")
    assert ratelimit.limiter.peek("reset|ip:203.0.113.9", RateLimit(1, 60)) == (True, 0), \
        "with key= only that key is counted"


# =============================================================== 4. passwords are hashed ==


def test_creating_a_user_with_a_raw_password_is_refused():
    db.migrate([auth.User])
    with pytest.raises(db.ValidationError) as caught:
        auth.User.create(username="ann", password="s3cret-pass")
    assert "create_user" in caught.value.errors["password"]
    assert auth.User.filter(username="ann").count() == 0, "nothing was written"


def test_assigning_a_raw_password_and_saving_is_refused():
    db.migrate([auth.User])
    user = auth.User.create_user("ann", "s3cret-pass")
    stored = user.password
    user.password = "other-pass"
    with pytest.raises(db.ValidationError):
        user.save()
    assert auth.User.get(id=user.id).password == stored
    user.set_password("other-pass")
    user.save()
    assert auth.authenticate("ann", "other-pass").id == user.id


def test_hashed_and_blank_passwords_still_save():
    db.migrate([auth.User])
    invited = auth.User.create(username="invited")                       # no password yet: fine
    assert invited.password == "" and not invited.check_password("")
    imported = auth.User.create(username="imported", password=auth.hash_password("pw"))
    assert auth.authenticate("imported", "pw").id == imported.id
    legacy = auth.User.create(username="legacy", password="pbkdf2_sha1$1000$salt$ZGlnZXN0")
    assert legacy.pk is not None, "a hash in the same layout from elsewhere can be stored"


def test_signing_in_a_user_whose_row_holds_an_old_raw_password_still_works():
    """Rows written by 0.6 (User.create(password=...)) mustn't make login() fail on save."""
    app = make_app()
    user = auth.User.create_user("ann", "pw")
    auth.User.filter(id=user.id).update(password="raw-from-0.6")

    @server
    def magic_link(request, username: str) -> str:
        auth.login(request, auth.User.get(username=username))
        return request.user.username

    client = TestClient(app)
    assert client.rpc(magic_link, "ann") == "ann"
    assert auth.User.get(id=user.id).last_login is not None


def test_editing_a_user_in_the_admin_keeps_the_stored_hash():
    app = Jongo("admin_070", root=HERE, dev=True, secret_key="k" * 40)
    app.admin()
    db.models_registry.update({"User": auth.User, "LoginSession": auth.LoginSession})
    db.migrate()
    root = auth.User.create_user("root", "pw", is_admin=True)
    bob = auth.User.create_user("bob", "bob-pw")
    client = app.test_client()
    client.login(root)
    client.get("/admin/user")
    form = {"username": "bob", "email": "bob@example.com", "name": "Bob", "_password": "",
            "csrf_token": client.cookies[CSRF_COOKIE]}
    response = client.post(f"/admin/user/{bob.id}", data=form)
    assert response.status == 303, response.text
    assert auth.User.get(id=bob.id).name == "Bob" and auth.authenticate("bob", "bob-pw").id == bob.id


# ================================================= 6. a flood never lifts a caller's block ==


def test_flooding_the_limiter_never_evicts_a_blocked_counter(monkeypatch):
    """The stress test's cycle: 50k fresh keys used to evict a throttled login and buy 10 guesses."""
    monkeypatch.setattr(ratelimit, "MAX_KEYS", 100)
    now = [0.0]
    limiter = Limiter(clock=lambda: now[0])
    login, flood = RateLimit(10, 300), RateLimit(3, 3600)
    victim = "login|user:victim"
    while limiter.hit(victim, login)[0]:
        now[0] += 0.001
    for i in range(5000):                                   # fifty times the table
        now[0] += 0.001
        assert limiter.hit(f"reset|x{i}", flood) == (True, 0), "callers under their limit make room"
        assert limiter.peek(victim, login)[0] is False, f"the victim's block was lifted after {i} keys"
    assert len(limiter) <= 100
    assert limiter.peek("reset|x4999", flood) == (True, 0) and len(limiter._hits["reset|x4999"]) == 1, \
        "the most recent callers are kept"
    now[0] += 301
    assert limiter.peek(victim, login) == (True, 0), "the block ends when its window does"


def test_a_table_full_of_blocked_callers_refuses_new_ones(monkeypatch):
    monkeypatch.setattr(ratelimit, "MAX_KEYS", 100)
    now = [0.0]
    limiter = Limiter(clock=lambda: now[0])
    once = RateLimit(1, 600)                                # one hit and the key is over its limit
    for i in range(100):
        now[0] += 0.001
        assert limiter.hit(f"blocked{i}", once) == (True, 0)
    allowed, retry_after = limiter.hit("newcomer", RateLimit(5, 60))
    assert not allowed and 590 <= retry_after <= 600, "fails closed until the first block ends"
    assert all(limiter.peek(f"blocked{i}", once)[0] is False for i in range(100)), "nobody was evicted"
    limiter.reset("blocked7")
    assert limiter.hit("newcomer", RateLimit(5, 60)) == (True, 0), "a reset frees a slot at once"
    assert limiter.hit("another", RateLimit(5, 60))[0] is False
    now[0] += 601
    assert limiter.hit("another", RateLimit(5, 60)) == (True, 0), "and room returns as blocks end"


def test_expired_keys_go_first_then_the_least_recent_under_their_limit(monkeypatch):
    monkeypatch.setattr(ratelimit, "MAX_KEYS", 10)
    now = [0.0]
    limiter = Limiter(clock=lambda: now[0])
    for i in range(3):
        limiter.hit(f"short{i}", RateLimit(5, 1))           # gone after a second
    for i in range(3):
        limiter.hit(f"long{i}", RateLimit(1, 600))          # over their limit for ten minutes
    for i in range(4):
        now[0] += 1
        limiter.hit(f"calm{i}", RateLimit(5, 600))          # well under their limit
    now[0] += 1
    assert limiter.hit("new0", RateLimit(5, 60)) == (True, 0)
    assert not any(f"short{i}" in limiter._hits for i in range(3)), "expired keys went first"
    assert all(f"calm{i}" in limiter._hits for i in range(4)), "...and were enough"
    for i in range(1, 4):
        assert limiter.hit(f"new{i}", RateLimit(5, 60)) == (True, 0)
    assert limiter.hit("new4", RateLimit(5, 60)) == (True, 0)
    assert "calm0" not in limiter._hits, "then the least recent caller under its limit"
    assert all(f"long{i}" in limiter._hits for i in range(3)), "never one that is over it"


def test_the_login_throttle_has_its_own_limiter(monkeypatch):
    monkeypatch.setattr(ratelimit, "MAX_KEYS", 50)
    monkeypatch.setattr(auth, "LOGIN_ATTEMPTS", 3)
    db.migrate()
    auth.User.create_user("victim", "right")
    assert auth.limiter is not ratelimit.limiter
    for guess in "abc":
        auth.authenticate("victim", guess)
    for i in range(500):                                    # fill the rate_limit= limiter many times over
        try:
            ratelimit.check(None, "3/hour", "reset-password", key=f"x{i}@example.com")
        except TooManyRequests:
            pass
    with pytest.raises(TooManyRequests):
        auth.authenticate("victim", "right")


# ================================================================== 7. CSRF at login/out ==


class _PlainRequest:
    def __init__(self):
        self.session = {}
        self.headers = {}
        self.client_ip = "127.0.0.1"


class _RotatingRequest(_PlainRequest):
    rotations = 0

    def rotate_csrf(self):
        self.rotations += 1


def test_login_and_logout_rotate_the_csrf_token():
    db.migrate()
    ann = auth.User.create_user("ann", "pw")
    request = _RotatingRequest()
    auth.login(request, ann)
    assert request.rotations == 1 and request.user is ann
    auth.logout(request)
    assert request.rotations == 2 and request.user is None


def test_login_works_on_a_request_without_rotate_csrf():
    db.migrate()
    ann = auth.User.create_user("ann", "pw")
    request = _PlainRequest()
    auth.login(request, ann)
    assert request.user is ann and request.session["_uid"] == ann.id
    auth.logout(request)
    assert request.user is None


@pytest.mark.skipif(not hasattr(Request, "rotate_csrf"), reason="Request.rotate_csrf arrives with the http batch")
def test_a_csrf_token_minted_before_sign_in_is_replaced():
    app = make_app()

    @server
    def sign_in(request, username: str, password: str) -> bool:
        auth.login(request, auth.authenticate(username, password, request))
        return True

    @server
    def sign_out(request) -> bool:
        auth.logout(request)
        return True

    auth.User.create_user("ann", "pw")
    client = TestClient(app)
    client.post(f"/_jongo/rpc/{sign_in.id}", json={"args": ["ghost", "x"], "kwargs": {}})  # mints a token
    before = client.cookies[CSRF_COOKIE]
    assert client.rpc(sign_in, "ann", "pw") is True
    after_login = client.cookies[CSRF_COOKIE]
    assert after_login != before
    assert client.rpc(sign_out) is True
    assert client.cookies[CSRF_COOKIE] != after_login


# ========================================================= 8. constant work for blank users ==


def test_a_user_without_a_password_costs_one_hash_like_everyone_else(monkeypatch):
    db.migrate([auth.User])
    auth.User.create(username="invited")                          # exists, no password yet
    auth.User.create_user("known", "pw")
    auth.User.create_user("legacy", "pw")
    auth.User.filter(username="legacy").update(password="raw-from-0.6")
    hashed = []
    real = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda password, encoded: hashed.append(1) or real(password, encoded))
    for name in ("nobody", "known", "invited", "legacy"):
        hashed.clear()
        assert auth.authenticate(name, "guess") is None
        assert hashed == [1], f"{name}: exactly one password hash per attempt"


def test_check_password_on_a_blank_password_hashes_and_says_no(monkeypatch):
    calls = []
    monkeypatch.setattr(auth, "verify_password", lambda password, encoded: calls.append(encoded) or True)
    assert auth.User(username="x").check_password("") is False, "even if the hash check said yes"
    assert calls == [auth._dummy()]


# ================================================== 9. creating jongo_sessions concurrently ==


_CHILD = textwrap.dedent('''
    import os, sys, time
    from jongo import db, auth
    db.configure(sys.argv[1])
    user = auth.User.get(username="ann")
    open(os.path.join(sys.argv[2], "ready-" + sys.argv[3]), "w").close()
    go = os.path.join(sys.argv[2], "go")
    deadline = time.time() + 30
    while not os.path.exists(go) and time.time() < deadline:
        time.sleep(0.0005)
    try:
        auth.session_data_for(user)
        print("ok")
    except BaseException as exc:
        print("ERR", type(exc).__name__, exc)
''')


@sqlite_only
@pytest.mark.parametrize("round_", range(2))
def test_processes_signing_in_at_once_after_an_upgrade_all_succeed(tmp_path, round_):
    database = str(tmp_path / "app.sqlite3")
    env = {**os.environ, "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    setup = ("from jongo import db\nfrom jongo.auth import User\n"
             f"db.configure({database!r}); db.migrate([User]); User.create_user('ann', 'pw')")
    subprocess.run([sys.executable, "-c", setup], check=True, env=env, timeout=60)
    count = 6
    children = [subprocess.Popen([sys.executable, "-c", _CHILD, database, str(tmp_path), str(i)],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
                for i in range(count)]
    deadline = time.time() + 60
    while len(list(tmp_path.glob("ready-*"))) < count and time.time() < deadline:
        time.sleep(0.01)
    (tmp_path / "go").touch()
    results = [child.communicate(timeout=60)[0].strip().splitlines()[-1] for child in children]
    assert results == ["ok"] * count, results


def test_a_table_created_by_someone_else_mid_way_counts_as_success(monkeypatch):
    """As on PostgreSQL, where BEGIN takes no lock: both look, both create, one CREATE fails."""
    migrate = sys.modules["jongo.db.migrate"]

    db.migrate()                                            # the table really exists...
    monkeypatch.setattr(auth, "_table_ready", None)
    real_live_tables, calls = migrate.live_tables, []

    def stale_view(conn):                                   # ...but both looks before CREATE say it doesn't
        calls.append(1)
        tables = real_live_tables(conn)
        if len(calls) <= 2:
            tables.pop(auth.LoginSession._meta.table, None)
        return tables

    monkeypatch.setattr(migrate, "live_tables", stale_view)
    monkeypatch.setattr(migrate, "plan_migrations", lambda models=None: [migrate._create_table(auth.LoginSession)])
    auth._ensure_table()                                    # CREATE TABLE fails; the re-check sees it: fine
    assert auth._table_ready is not None and len(calls) == 3


def test_a_create_that_really_failed_is_still_reported(monkeypatch):
    migrate = sys.modules["jongo.db.migrate"]

    db.migrate([auth.User])                                 # a 0.5 database: no sessions table
    monkeypatch.setattr(auth, "_table_ready", None)

    def broken(conn, op):
        raise migrate.MigrationError(f"{op.describe()} failed: disk full")

    monkeypatch.setattr(migrate, "_apply", broken)
    with pytest.raises(migrate.MigrationError):
        auth._ensure_table()
    assert auth._table_ready is None
