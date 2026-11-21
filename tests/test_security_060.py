"""The three security items left open by the 0.4.1 audit, closed in 0.6.0.

1. Nothing slowed password guessing        -> authenticate() throttles; rate_limit= everywhere.
2. A session couldn't be revoked           -> every sign-in is a row; delete it and the cookie is dead.
3. A channel guard returning None allowed  -> only a truthy answer allows; None is J412.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import use_test_database
from jongo import Forbidden, HTTPError, Jongo, ServerError, auth, db, ratelimit, redirect, rpc, server
from jongo.http import SESSION_COOKIE
from jongo.introspect import check
from jongo.live import authorize, hub
from jongo.ratelimit import Limiter, RateLimit, TooManyRequests, parse
from jongo.testing import TestClient

HERE = Path(__file__).parent


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    saved_models = dict(db.models_registry)
    saved_functions = dict(rpc.SERVER_FUNCTIONS)
    db.models_registry.clear()
    db.models_registry.update({"User": auth.User, "LoginSession": auth.LoginSession})
    use_test_database()
    monkeypatch.setattr(auth, "ITERATIONS", 1000)
    ratelimit.limiter.reset()
    auth.limiter.reset()
    yield
    ratelimit.limiter.reset()
    auth.limiter.reset()
    hub.clear()
    db.close_connections()
    db.models_registry.clear()
    db.models_registry.update(saved_models)
    rpc.SERVER_FUNCTIONS.clear()
    rpc.SERVER_FUNCTIONS.update(saved_functions)


def make_app(**options):
    app = Jongo("security_060", root=HERE, secret_key="k" * 40, **options)

    @server
    def sign_in(request, username: str, password: str):
        user = auth.authenticate(username, password, request)
        if user is None:
            raise HTTPError(400, "Wrong username or password")
        auth.login(request, user)
        return redirect("/")

    @server
    def sign_out(request) -> bool:
        auth.logout(request)
        return True

    @server
    def whoami(request):
        return request.user.username if request.user else None

    @server(login_required=True)
    def my_sessions(request) -> list:
        return [s.to_dict() for s in auth.sessions(request.user)]

    @server(login_required=True)
    def sign_out_elsewhere(request) -> int:
        return auth.revoke_sessions(request.user, keep=request)

    db.migrate()
    app.fns = type("F", (), dict(sign_in=sign_in, sign_out=sign_out, whoami=whoami, my_sessions=my_sessions,
                                 sign_out_elsewhere=sign_out_elsewhere))
    return app


# ============================================================================ rate limits ==


@pytest.mark.parametrize("text, expected", [
    ("5/minute", (5, 60)), ("100/hour", (100, 3600)), ("3/s", (3, 1)), ("10 per 30 seconds", (10, 30)),
    ("7 per day", (7, 86400)), ("1/2h", (1, 7200)), ("5 / minutes", (5, 60)), ((4, 2.5), (4, 2.5)),
])
def test_rates_are_parsed(text, expected):
    limit = parse(text)
    assert (limit.count, limit.seconds) == expected
    assert parse(limit) is limit and parse(str(limit)) == limit      # str() round-trips


@pytest.mark.parametrize("text", ["x", "0/minute", "5/fortnight", "/minute", "5", (0, 60), (5, 0)])
def test_a_rate_that_cant_be_read_is_a_coded_error(text):
    with pytest.raises(Exception) as caught:
        parse(text)
    assert caught.value.code == "J513"
    assert parse(None) is None and parse(False) is None


def test_the_limiter_is_a_sliding_window():
    now = [1000.0]
    limiter = Limiter(clock=lambda: now[0])
    limit = RateLimit(3, 10)
    for _ in range(3):                       # hits at t=1000, 1001, 1002
        assert limiter.hit("k", limit) == (True, 0)
        now[0] += 1
    assert limiter.hit("k", limit) == (False, 7), "the oldest hit leaves the window at t=1010"
    assert limiter.hit("other", limit) == (True, 0), "keys are independent"
    now[0] = 1009.5
    assert limiter.peek("k", limit) == (False, 1)
    now[0] = 1010.5
    assert limiter.peek("k", limit) == (True, 0)
    assert limiter.hit("k", limit) == (True, 0), "the first hit has aged out, so one slot is free"
    assert limiter.hit("k", limit)[0] is False, "but only one: the window slides, it doesn't reset"
    limiter.reset("k")
    assert limiter.hit("k", limit) == (True, 0)


def test_the_limiter_forgets_old_keys_instead_of_growing_without_bound(monkeypatch):
    monkeypatch.setattr(ratelimit, "MAX_KEYS", 100)
    now = [0.0]
    limiter = Limiter(clock=lambda: now[0])
    for i in range(1000):
        now[0] += 1
        limiter.hit(f"ip:{i}", RateLimit(5, 60))
    assert len(limiter) <= 100
    assert limiter.peek("ip:999", RateLimit(1, 60)) == (False, 60), "the most recent callers are kept"


def test_a_server_function_over_its_limit_gets_429_and_does_not_run():
    app = make_app()
    calls = []

    @server(rate_limit="3/minute")
    def ping(request) -> int:
        calls.append(1)
        return len(calls)

    client = TestClient(app)
    assert [client.rpc(ping) for _ in range(3)] == [1, 2, 3]
    with pytest.raises(ServerError) as caught:
        client.rpc(ping)
    assert caught.value.status == 429 and caught.value.code == "J213"
    assert len(calls) == 3
    response = client.post(f"/_jongo/rpc/{ping.id}", json={})
    assert response.status == 429 and int(response.headers["retry-after"]) >= 1
    assert response.json()["error"]["code"] == "J213"


def test_limits_are_per_caller_user_when_signed_in_address_otherwise():
    app = make_app(trust_proxy=True)   # so each client below can come from its own address

    @server(rate_limit="2/minute")
    def ping(request) -> str:
        return "pong"

    def call(client, address):
        return client.post(f"/_jongo/rpc/{ping.id}", json={"args": [], "kwargs": {}},
                           headers={"X-Forwarded-For": address}).status

    ann, bob = auth.User.create_user("ann", "pw-ann"), auth.User.create_user("bob", "pw-bob")
    a, b, anonymous = TestClient(app), TestClient(app), TestClient(app)
    a.login(ann)
    b.login(bob)
    assert [call(a, "10.0.0.1") for _ in range(3)] == [200, 200, 429]
    assert call(b, "10.0.0.2") == 200, "another user has their own counter"
    assert call(anonymous, "10.0.0.3") == 200, "and so does an anonymous address"


def test_pages_and_routes_take_a_rate_limit_too():
    app = make_app()

    @app.route("/hook", methods=("POST",), csrf=False, rate_limit="2/minute")
    def hook(request):
        return {"ok": True}

    @app.page("/slow", rate_limit=(1, 60))
    def slow():
        return "hello"

    client = TestClient(app)
    assert [client.post("/hook").status for _ in range(3)] == [200, 200, 429]
    assert client.get("/slow").status == 200
    refused = client.get("/slow")
    assert refused.status == 429 and "retry-after" in refused.headers
    with pytest.raises(Exception) as caught:
        app.route("/bad", rate_limit="often")(lambda request: None)
    assert caught.value.code == "J513", "a bad rate fails when the route is registered, not on the first request"


def test_client_ip_ignores_forwarded_for_unless_the_proxy_is_trusted():
    seen = {}
    for trust in (False, True):
        app = Jongo(f"ip_{trust}", root=HERE, secret_key="k" * 40, trust_proxy=trust)

        @app.route("/ip")
        def ip(request):
            return {"ip": request.client_ip}

        seen[trust] = TestClient(app).get("/ip", headers={"X-Forwarded-For": "6.6.6.6, 10.0.0.9"}).json()["ip"]
    assert seen[False] == "127.0.0.1", "a client-supplied header must not choose the rate-limit key"
    assert seen[True] == "10.0.0.9", "with a trusted proxy, the entry the proxy appended (the last one)"


# ========================================================================= login throttle ==


def test_password_guessing_is_throttled_per_username(monkeypatch):
    monkeypatch.setattr(auth, "LOGIN_ATTEMPTS", 4)
    db.migrate()
    auth.User.create_user("ann", "right")
    assert all(auth.authenticate("ann", f"guess{i}") is None for i in range(4))
    with pytest.raises(TooManyRequests) as caught:
        auth.authenticate("ann", "right")
    assert caught.value.status == 429 and caught.value.code == "J213" and caught.value.retry_after >= 1
    assert "sign-in attempts" in caught.value.message
    # The key is the exact username the account is looked up by (0.7.0): "ANN " is another
    # name, not another spelling of ann, and can never sign in as ann.
    assert auth.authenticate("ANN ", "right") is None
    assert auth.authenticate("someone-else", "x") is None, "another username isn't locked"


def test_a_locked_username_does_no_password_hashing(monkeypatch):
    monkeypatch.setattr(auth, "LOGIN_ATTEMPTS", 2)
    db.migrate()
    auth.User.create_user("ann", "right")
    auth.authenticate("ann", "a"), auth.authenticate("ann", "b")
    hashed = []
    monkeypatch.setattr(auth, "verify_password", lambda *args: hashed.append(1) or False)
    with pytest.raises(TooManyRequests):
        auth.authenticate("ann", "c")
    assert hashed == [], "once throttled, a guess must cost the server nothing"


def test_a_correct_password_clears_the_count_and_the_window_expires(monkeypatch):
    monkeypatch.setattr(auth, "LOGIN_ATTEMPTS", 3)
    db.migrate()
    now = [0.0]
    monkeypatch.setattr(auth.limiter, "_clock", lambda: now[0])
    auth.User.create_user("ann", "right")
    auth.authenticate("ann", "x"), auth.authenticate("ann", "y")
    assert auth.authenticate("ann", "right").username == "ann"
    assert [auth.authenticate("ann", g) for g in "abc"] == [None, None, None], "the count started again"
    with pytest.raises(TooManyRequests):
        auth.authenticate("ann", "right")
    now[0] += auth.LOGIN_WINDOW + 1
    assert auth.authenticate("ann", "right").username == "ann"


def test_unknown_usernames_are_throttled_too_so_the_limit_reveals_nothing(monkeypatch):
    monkeypatch.setattr(auth, "LOGIN_ATTEMPTS", 2)
    db.migrate()
    assert auth.authenticate("ghost", "a") is None and auth.authenticate("ghost", "b") is None
    with pytest.raises(TooManyRequests):
        auth.authenticate("ghost", "c")


def test_guessing_across_many_usernames_is_throttled_by_address(monkeypatch):
    monkeypatch.setattr(auth, "LOGIN_ATTEMPTS_PER_ADDRESS", 5)
    app = make_app()
    client = TestClient(app)
    for i in range(5):
        with pytest.raises(ServerError) as caught:
            client.rpc(app.fns.sign_in, f"user{i}", "password")
        assert caught.value.status == 400
    with pytest.raises(ServerError) as caught:
        client.rpc(app.fns.sign_in, "user-new", "password")
    assert caught.value.status == 429 and caught.value.code == "J213"


def test_a_burst_of_parallel_guesses_cannot_outrun_the_limit(monkeypatch):
    """Attempts are counted before the (slow) password check, not after it."""
    import threading

    monkeypatch.setattr(auth, "LOGIN_ATTEMPTS", 5)
    db.migrate()
    auth.User.create_user("ann", "right")
    checked, gate = [], threading.Event()
    real = auth.verify_password

    def slow_verify(password, encoded):
        gate.wait(5)                      # every guess is in flight at once
        checked.append(password)
        return real(password, encoded)

    monkeypatch.setattr(auth, "verify_password", slow_verify)
    results = []

    def guess(i):
        try:
            results.append(auth.authenticate("ann", f"guess{i}"))
        except TooManyRequests:
            results.append("throttled")

    threads = [threading.Thread(target=guess, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(10)
    assert len(checked) == 5 and results.count("throttled") == 35


def test_correct_passwords_are_not_counted_against_an_address(monkeypatch):
    monkeypatch.setattr(auth, "LOGIN_ATTEMPTS_PER_ADDRESS", 3)
    app = make_app()
    for i in range(6):                    # six people behind one office address, all signing in correctly
        auth.User.create_user(f"user{i}", "pw")
        assert TestClient(app).rpc(app.fns.sign_in, f"user{i}", "pw") == {"redirect": "/"}


def test_the_throttle_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(auth, "LOGIN_ATTEMPTS", None)
    db.migrate()
    monkeypatch.setattr(auth, "LOGIN_ATTEMPTS_PER_ADDRESS", None)
    auth.User.create_user("ann", "right")
    assert all(auth.authenticate("ann", "wrong") is None for _ in range(30))
    assert auth.authenticate("ann", "right") is not None


def test_the_admin_login_says_when_it_is_throttled(monkeypatch):
    monkeypatch.setattr(auth, "LOGIN_ATTEMPTS", 2)
    app = Jongo("admin_throttle", root=HERE, dev=True, secret_key="k" * 40)
    app.admin()
    db.models_registry.update({"User": auth.User, "LoginSession": auth.LoginSession})
    db.migrate()
    auth.User.create_user("root", "pw", is_admin=True)
    client = app.test_client()
    client.get("/admin/login")
    form = {"username": "root", "csrf_token": client.cookies["jongo_csrf"]}
    assert [client.post("/admin/login", data={**form, "password": "no"}).status for _ in range(2)] == [400, 400]
    refused = client.post("/admin/login", data={**form, "password": "pw"})
    assert refused.status == 429 and "Too many failed sign-in attempts" in refused.text
    assert "retry-after" in refused.headers
    assert SESSION_COOKIE not in client.cookies


# ============================================================================== sessions ==


def test_logging_in_records_the_session_and_logging_out_deletes_it():
    app = make_app()
    ann = auth.User.create_user("ann", "pw")
    client = TestClient(app)
    client.rpc(app.fns.sign_in, "ann", "pw")
    rows = auth.sessions(ann)
    assert len(rows) == 1 and rows[0].ip == "127.0.0.1" and rows[0].last_seen is not None
    assert "key" not in rows[0].to_dict(), "the stored hash never leaves the server"
    assert client.rpc(app.fns.whoami) == "ann"
    client.rpc(app.fns.sign_out)
    assert auth.sessions(ann) == []


def test_a_copied_cookie_is_dead_after_logout():
    """The point of server-side sessions: before 0.6.0 a stolen cookie outlived logout."""
    app = make_app()
    auth.User.create_user("ann", "pw")
    client, thief = TestClient(app), TestClient(app)
    client.rpc(app.fns.sign_in, "ann", "pw")
    thief.cookies[SESSION_COOKIE] = client.cookies[SESSION_COOKIE]
    assert thief.rpc(app.fns.whoami) == "ann"
    client.rpc(app.fns.sign_out)
    assert thief.rpc(app.fns.whoami) is None, "the cookie is still validly signed, and still refused"


def test_revoking_one_session_signs_out_only_that_browser():
    app = make_app()
    ann = auth.User.create_user("ann", "pw")
    laptop, phone = TestClient(app), TestClient(app)
    laptop.rpc(app.fns.sign_in, "ann", "pw")
    phone.rpc(app.fns.sign_in, "ann", "pw")
    listed = laptop.rpc(app.fns.my_sessions)
    assert len(listed) == 2 and {"id", "created", "last_seen", "ip", "user_agent", "user_id"} <= set(listed[0])
    oldest = min(s["id"] for s in listed)
    assert auth.revoke_session(oldest) is True and auth.revoke_session(oldest) is False
    assert laptop.rpc(app.fns.whoami) is None and phone.rpc(app.fns.whoami) == "ann"
    assert len(auth.sessions(ann)) == 1


def test_revoke_session_can_be_scoped_to_its_owner():
    app = make_app()
    ann, bob = auth.User.create_user("ann", "pw"), auth.User.create_user("bob", "pw")
    client = TestClient(app)
    client.login(ann)
    target = auth.sessions(ann)[0]
    assert auth.revoke_session(target.id, user=bob) is False, "bob can't end ann's session by guessing its id"
    assert client.rpc(app.fns.whoami) == "ann"
    assert auth.revoke_session(target, user=ann) is True


def test_sign_out_everywhere_and_everywhere_else():
    app = make_app()
    ann, bob = auth.User.create_user("ann", "pw"), auth.User.create_user("bob", "pw")
    clients = [TestClient(app) for _ in range(3)]
    for client in clients:
        client.login(ann)
    other = TestClient(app)
    other.login(bob)
    assert clients[0].rpc(app.fns.sign_out_elsewhere) == 2
    assert [c.rpc(app.fns.whoami) for c in clients] == ["ann", None, None]
    assert auth.revoke_sessions(ann) == 1
    assert clients[0].rpc(app.fns.whoami) is None
    assert other.rpc(app.fns.whoami) == "bob", "another user's sessions are untouched"


def test_a_cookie_from_before_060_is_signed_out():
    app = make_app()
    ann = auth.User.create_user("ann", "pw")
    client = TestClient(app)
    legacy = {"_uid": ann.id, "_pw": auth._fingerprint(ann)}        # what 0.5 put in the cookie
    client.cookies[SESSION_COOKIE] = app.signer.dumps(legacy)
    assert client.rpc(app.fns.whoami) is None
    forged = {**legacy, "_sid": "made-up"}
    client.cookies[SESSION_COOKIE] = app.signer.dumps(forged)
    assert client.rpc(app.fns.whoami) is None, "an id with no row behind it is nobody"


def test_changing_the_password_still_signs_out_other_sessions():
    app = make_app()
    ann = auth.User.create_user("ann", "pw")
    client = TestClient(app)
    client.login(ann)
    ann.set_password("new-password")
    ann.save()
    assert client.rpc(app.fns.whoami) is None


def test_a_session_older_than_session_max_age_is_refused_and_removed():
    app = make_app(session_max_age=3600)
    ann = auth.User.create_user("ann", "pw")
    client = TestClient(app)
    client.login(ann)
    assert client.rpc(app.fns.whoami) == "ann"
    auth.LoginSession.filter(user_id=ann.id).update(created=datetime.now(timezone.utc) - timedelta(hours=2))
    assert client.rpc(app.fns.whoami) is None
    assert auth.sessions(ann) == []


def test_last_seen_is_refreshed_but_not_on_every_request(monkeypatch):
    app = make_app()
    ann = auth.User.create_user("ann", "pw")
    client = TestClient(app)
    client.login(ann)
    stale = datetime.now(timezone.utc) - timedelta(hours=1)
    auth.LoginSession.filter(user_id=ann.id).update(last_seen=stale)
    client.rpc(app.fns.whoami)
    first = auth.sessions(ann)[0].last_seen
    assert first > stale
    client.rpc(app.fns.whoami)
    assert auth.sessions(ann)[0].last_seen == first, "a request inside the 5 minutes writes nothing"


def test_logging_in_again_replaces_the_session_instead_of_stacking_rows():
    app = make_app()
    ann = auth.User.create_user("ann", "pw")
    client = TestClient(app)
    for _ in range(3):
        client.rpc(app.fns.sign_in, "ann", "pw")
    assert len(auth.sessions(ann)) == 1


def test_an_app_upgraded_from_05_gets_the_sessions_table_on_first_login():
    """`jongo run` doesn't migrate unless asked; login must not 500 until someone does."""
    app = Jongo("upgraded", root=HERE, secret_key="k" * 40)
    db.migrate([auth.User])                                     # a 0.5 database: users, no sessions table
    ann = auth.User.create_user("ann", "pw")

    @server
    def sign_in_upgraded(request, username: str, password: str) -> str:
        auth.login(request, auth.authenticate(username, password, request))
        return request.user.username

    @server
    def who(request):
        return request.user.username if request.user else None

    client = TestClient(app)
    assert client.rpc(who) is None
    assert client.rpc(sign_in_upgraded, "ann", "pw") == "ann"
    assert client.rpc(who) == "ann" and len(auth.sessions(ann)) == 1


def test_deleting_a_user_deletes_their_sessions():
    make_app()
    ann = auth.User.create_user("ann", "pw")
    auth.session_data_for(ann)
    assert auth.LoginSession.all().count() == 1
    ann.delete()
    assert auth.LoginSession.all().count() == 0


# ======================================================================== channel guards ==


class _Request:
    session = {}


def channel_app():
    app = Jongo("guards", root=HERE, secret_key="k" * 40)

    @app.channel("open")
    def open_channel(request):
        return True

    @app.channel("closed")
    def closed(request):
        return False

    @app.channel("forgot")
    def forgot(request):
        request.session.get("rooms")        # the bug: computes something, returns nothing

    @app.channel("room:<int:id>")
    def room(request, id):
        if id < 10:
            return True

    @app.channel("raises")
    def raises(request):
        raise Forbidden("members only")

    @app.channel("empty-list")
    def empty(request):
        return []

    return app


def test_only_a_truthy_answer_allows_a_subscription():
    app = channel_app()
    assert authorize(["open", "room:3"], app.channels, _Request()) == ["open", "room:3"]
    for name, code in [("closed", "J410"), ("empty-list", "J410"), ("forgot", "J412"), ("room:50", "J412"),
                       ("raises", "J210"), ("undeclared", "J410")]:
        with pytest.raises(Forbidden) as caught:
            authorize([name], app.channels, _Request())
        assert caught.value.code == code, name


def test_a_guard_that_returns_nothing_is_refused_over_http_with_its_own_code():
    app = channel_app()
    client = TestClient(app)
    refused = client.get("/_jongo/live?channels=open,forgot")
    assert refused.status == 403, "one refused channel refuses the whole connection"
    assert hub.subscriber_count() == 0, "nothing may stay subscribed after a refusal"


def test_jongo_check_finds_a_guard_that_can_never_answer():
    app = channel_app()
    report = check(app)
    problems = [p for p in report["problems"] if p["kind"] == "channel"]
    assert [p["name"] for p in problems] == ["forgot"], "only the guard with no return at all is certain"
    problem = problems[0]
    assert problem["code"] == "J412" and problem["file"] == "test_security_060.py" and problem["line"]
    assert "never returns a value" in problem["message"] and "return True" in problem["hint"]
    assert report["ok"] is False
