"""Users, password hashing and login sessions.

    from jongo.auth import User, authenticate, login, logout

    @server
    def sign_in(request, username: str, password: str):
        user = authenticate(username, password)
        if user is None:
            raise HTTPError(400, "Wrong username or password")
        login(request, user)
        return redirect("/")

``request.user`` is the logged-in User (or None) everywhere: pages, routes and
server functions.

Two protections are on without configuration:

* **Password guessing is throttled.** After :data:`LOGIN_ATTEMPTS` wrong passwords for one
  username inside :data:`LOGIN_WINDOW` seconds, ``authenticate()`` raises a 429
  (:class:`jongo.ratelimit.TooManyRequests`, code ``J213``) instead of checking another one.
  The count is kept per exact username (the lookup is case-sensitive too) on a limiter of
  its own, :data:`limiter`.
* **Passwords are stored hashed.** Set them with :meth:`User.create_user` or
  :meth:`User.set_password`; saving a ``User`` whose ``password`` isn't a hash raises
  ``ValidationError`` rather than writing the raw password to the database.
* **Sign-ins can be revoked from the server.** Every ``login()`` is a row in
  ``jongo_sessions``; ``logout()`` deletes it, and so can you: :func:`sessions` lists a
  user's sign-ins, :func:`revoke_session` ends one and :func:`revoke_sessions` ends them
  all. A cookie whose row is gone is signed out on its next request, even though the
  cookie itself is still correctly signed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sys
import threading
from datetime import datetime, timedelta, timezone

from . import db
from .ratelimit import Limiter, RateLimit, TooManyRequests

__all__ = [
    "User", "LoginSession", "authenticate", "login", "logout", "get_user", "sessions", "revoke_session",
    "revoke_sessions", "hash_password", "verify_password", "session_data_for",
    "LOGIN_ATTEMPTS", "LOGIN_WINDOW", "LOGIN_ATTEMPTS_PER_ADDRESS", "limiter",
]

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 480_000

#: Wrong passwords allowed for one username inside :data:`LOGIN_WINDOW` before
#: ``authenticate()`` refuses to check another. Set to ``None`` to turn the throttle off.
LOGIN_ATTEMPTS: int | None = 10
#: Wrong passwords allowed from one client address inside :data:`LOGIN_WINDOW`, across all
#: usernames (only counted when ``authenticate()`` is given the request).
LOGIN_ATTEMPTS_PER_ADDRESS: int | None = 50
#: The length of the throttle window, in seconds.
LOGIN_WINDOW: int = 300

#: The limiter that counts wrong passwords. It is separate from :data:`jongo.ratelimit.limiter`
#: (``rate_limit=``), so a flood of other rate-limited calls can't crowd the sign-in counters
#: out of memory. ``auth.limiter.reset()`` forgets every count (useful between tests).
limiter = Limiter()

#: How stale ``last_seen`` may get before a request refreshes it (seconds). Keeps an
#: authenticated request to reads only, most of the time.
_TOUCH_EVERY = 300


def hash_password(password: str, *, iterations: int | None = None) -> str:
    iterations = iterations or ITERATIONS
    salt = secrets.token_urlsafe(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return f"{ALGORITHM}${iterations}${salt}${base64.b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != ALGORITHM:
            return False
        rounds = int(iterations)
        if rounds < 1:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), rounds)
    except (ValueError, AttributeError, TypeError):
        return False  # a corrupt/tampered stored hash fails auth cleanly, never 500s
    return hmac.compare_digest(base64.b64encode(digest).decode(), expected)


_dummy_hash: str | None = None


def _dummy() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(12))
    return _dummy_hash


#: ``<algorithm>$<iterations>$<salt>$<digest>``: the shape of a stored hash (any algorithm name,
#: so hashes imported from elsewhere in the same layout can be stored and upgraded later).
_HASH_SHAPE = re.compile(r"[A-Za-z0-9_]+\$[0-9]{1,10}\$[^$]+\$[^$]+")


def _is_hash(value) -> bool:
    return isinstance(value, str) and _HASH_SHAPE.fullmatch(value) is not None


def _checkable(encoded) -> bool:
    """True when ``verify_password`` would really hash against ``encoded``."""
    if not _is_hash(encoded):
        return False
    algorithm, iterations, _ = encoded.split("$", 2)
    return algorithm == ALGORITHM and int(iterations) >= 1


class User(db.Model):
    username = db.Text(max_length=150, unique=True)
    email = db.Text(max_length=254, blank=True, default="")
    name = db.Text(max_length=150, blank=True, default="")
    password = db.Text(blank=True, default="", editable=False)
    is_admin = db.Bool(default=False, label="Admin", help="Can use the admin site")
    created = db.DateTime(auto_now_add=True)
    last_login = db.DateTime(null=True, editable=False)

    class Meta:
        table = "jongo_users"
        ordering = ["username"]

    def set_password(self, raw_password: str) -> None:
        self.password = hash_password(raw_password)

    def check_password(self, raw_password: str) -> bool:
        if not _checkable(self.password):
            # No password set (or not one this module can check): still do one hash's work,
            # so the reply takes as long as a wrong password and timing reveals nothing.
            verify_password(raw_password, _dummy())
            return False
        return verify_password(raw_password, self.password)

    def clean(self) -> None:
        # Model.create(password=...) and assign-then-save go through here; without this check
        # they would write the raw password to the database, where no login can match it.
        if self.password and not _is_hash(self.password):
            raise db.ValidationError({
                "password": "User.password must be a hash: use User.create_user(...) or user.set_password(...)",
            })

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.pop("password", None)  # never send password hashes to the browser
        return data

    def __str__(self):
        return self.username

    @classmethod
    def create_user(cls, username: str, password: str, **fields) -> "User":
        user = cls(username=username, **fields)
        user.set_password(password)
        user.save()
        return user


def _throttle_keys(username: str, request) -> list[tuple[str, RateLimit]]:
    keys = []
    if LOGIN_ATTEMPTS:
        # Exactly the name the account is looked up by (that lookup is case-sensitive). A
        # normalised key would let a look-alike account ("Ann" for "ann") clear the victim's
        # count with its own correct password.
        keys.append((f"login|user:{str(username)[:150]}", RateLimit(LOGIN_ATTEMPTS, LOGIN_WINDOW)))
    if LOGIN_ATTEMPTS_PER_ADDRESS and request is not None:
        address = getattr(request, "client_ip", None) or getattr(request, "remote_addr", "") or "unknown"
        keys.append((f"login|ip:{address}", RateLimit(LOGIN_ATTEMPTS_PER_ADDRESS, LOGIN_WINDOW)))
    return keys


def authenticate(username: str, password: str, request=None) -> User | None:
    """Check a username and password; return the User, or None if they don't match.

    Wrong passwords are counted per username (and per client address when ``request`` is
    passed). Past :data:`LOGIN_ATTEMPTS` failures in :data:`LOGIN_WINDOW` seconds the
    password is no longer checked at all: the call raises, so guessing gains nothing and
    costs the server no hashing. A correct password clears that exact username's count.

    Parameters:
        username: the account name.
        password: the password as typed.
        request: the current request (optional). Passing it also throttles by client
            address, which stops one machine from trying a few passwords on many accounts.

    Returns:
        The :class:`User`, or ``None`` for a wrong username or password.

    Raises:
        jongo.ratelimit.TooManyRequests: a 429 with code ``J213`` and ``retry_after``
            seconds, once the caller is over the limit. Raised from a page, route or
            server function it becomes the response; catch it to show your own message.

    Example::

        @server
        def sign_in(request, username: str, password: str):
            user = authenticate(username, password, request)
            if user is None:
                raise HTTPError(400, "Wrong username or password")
            login(request, user)
            return redirect("/")
    """
    keys = _throttle_keys(username, request)
    # Count the attempt before checking it. Checking first and counting afterwards would let
    # a burst of parallel guesses all read "under the limit" and all be tried.
    counted = []
    for key, limit in keys:
        allowed, retry_after = limiter.hit(key, limit)
        if not allowed:
            for earlier in counted:
                limiter.refund(earlier)
            raise TooManyRequests(
                f"Too many failed sign-in attempts. Try again in {retry_after}s.", retry_after=retry_after,
                hint="wait for the window to pass; jongo.auth.LOGIN_ATTEMPTS and LOGIN_WINDOW set the limit",
            )
        counted.append(key)
    user = User.filter(username=username).first()
    if user is None:
        verify_password(password, _dummy())  # same work either way, so timing reveals nothing
        return None
    if not user.check_password(password):
        return None
    for key in counted:  # a correct password was never a guess
        limiter.refund(key)
    if LOGIN_ATTEMPTS:  # and it clears this exact username's count, nobody else's
        limiter.reset(keys[0][0])
    return user


def _fingerprint(user: User) -> str:
    # Changing the password changes the fingerprint, which signs out other sessions.
    return hashlib.sha256(f"jongo-auth:{user.password}".encode()).hexdigest()[:20]


# ---------------------------------------------------------------------------
# Server-side sign-in records


class LoginSession(db.Model):
    """One sign-in: the server's record of a session cookie it issued.

    The cookie carries a random id; the table stores only its SHA-256, so the rows are no
    use to anyone who reads the database. Deleting a row signs that browser out.
    """

    key = db.Text(max_length=64, unique=True, editable=False)
    user = db.ForeignKey(User, on_delete="CASCADE", related_name="login_sessions")
    created = db.DateTime(auto_now_add=True)
    last_seen = db.DateTime(null=True, editable=False)
    ip = db.Text(max_length=64, blank=True, default="", editable=False)
    user_agent = db.Text(max_length=200, blank=True, default="", editable=False)

    class Meta:
        table = "jongo_sessions"
        ordering = ["-id"]
        verbose_name = "login session"

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.pop("key", None)  # the hash stays on the server
        return data

    def __str__(self):
        return f"session {self.pk} (user {self.user_id})"


def _key_for(sid: str) -> str:
    return hashlib.sha256(f"jongo-session:{sid}".encode()).hexdigest()


_table_lock = threading.Lock()
_table_ready: tuple | None = None


def _ensure_table() -> None:
    """Create ``jongo_sessions`` if this database doesn't have it yet.

    An app upgraded from 0.5 has users but not this table, and ``jongo run`` doesn't
    migrate unless asked. Creating one framework-owned table is additive and safe, and the
    alternative is every login failing until someone runs ``jongo migrate``.
    """
    global _table_ready
    from .db import connection
    from .db.migrate import MigrationError, _apply, live_tables, plan_migrations

    marker = (connection.database_path(), connection._generation)
    if _table_ready == marker:
        return
    table, users = LoginSession._meta.table.lower(), User._meta.table.lower()
    with _table_lock:  # one thread per process; other processes are handled below
        if _table_ready == marker:
            return
        with connection.locked() as conn:
            tables = live_tables(conn)
        if table not in tables and users in tables:
            try:
                # Decide and create inside one write transaction (BEGIN IMMEDIATE on SQLite), so
                # two processes signing in at once after an upgrade can't both see "missing".
                with connection.transaction() as conn:
                    tables = live_tables(conn)
                    if table not in tables and users in tables:
                        for op in plan_migrations([User, LoginSession]):
                            if op.table.lower() == table and not op.destructive:
                                _apply(conn, op)
            except MigrationError:
                # Another process created it first (on PostgreSQL BEGIN takes no lock): fine.
                with connection.locked() as conn:
                    if table not in live_tables(conn):
                        raise
        _table_ready = (connection.database_path(), connection._generation)


def _open_session(user: User, request=None) -> str:
    """Record a sign-in and return the random id that goes into the cookie."""
    _ensure_table()
    sid = secrets.token_urlsafe(24)
    max_age = getattr(getattr(request, "app", None), "session_max_age", None)
    if max_age:  # rows whose cookie has expired anyway
        LoginSession.filter(created__lt=datetime.now(timezone.utc) - timedelta(seconds=max_age)).delete()
    headers = getattr(request, "headers", None)
    LoginSession.create(
        key=_key_for(sid), user_id=user.id, last_seen=datetime.now(timezone.utc),
        ip=(getattr(request, "client_ip", "") or "")[:64],
        user_agent=((headers.get("user-agent") if headers is not None else "") or "")[:200],
    )
    return sid


def session_data_for(user: User, request=None) -> dict:
    """The session contents that sign ``user`` in, recording the sign-in on the server.

    ``login()`` uses this; so does ``TestClient.login()``. Call it yourself only when you
    build a session cookie by hand.

    Parameters:
        user: the account to sign in.
        request: the current request, if there is one (its address and user agent are
            stored with the sign-in so the user can recognise it in a list).

    Returns:
        ``{"_uid": ..., "_pw": ..., "_sid": ...}`` to put in ``request.session``.
    """
    return {"_uid": user.id, "_pw": _fingerprint(user), "_sid": _open_session(user, request)}


def login(request, user: User) -> None:
    """Sign ``user`` in on this request's session.

    Clears whatever the session held (so a pre-login session id can't be reused), issues a
    new CSRF token (so one minted, or planted, before sign-in stops working), records the
    sign-in in ``jongo_sessions`` and sets ``request.user``.

    Example::

        user = authenticate(username, password, request)
        if user is not None:
            login(request, user)
    """
    _end_current(request)
    request.session.clear()
    _rotate_csrf(request)
    request.session.update(session_data_for(user, request))
    user.last_login = datetime.now(timezone.utc)
    # Write just this column: a full save() would also rewrite (and re-validate) every other
    # field, failing for a row stored by an older version with a raw password.
    User.filter(id=user.id).update(last_login=user.last_login)
    request.user = user


def _rotate_csrf(request) -> None:
    rotate = getattr(request, "rotate_csrf", None)
    if callable(rotate):
        rotate()


def _end_streams(ids) -> None:
    """End the open ``live()`` streams of these sign-ins now (if ``jongo.live`` is in use).

    Without this a revoked sign-in's stream still ends, but only on its next re-check
    (``live.SESSION_CHECK_SECONDS``); this makes signing out take effect at once.
    """
    live = sys.modules.get("jongo.live")
    hub = getattr(live, "hub", None)
    if hub is None:
        return
    for pk in ids:
        try:
            hub.drop_session(pk)
        except Exception:  # ending a stream must never make signing out fail
            pass


def _end_current(request) -> None:
    sid = request.session.get("_sid")
    if sid:
        try:
            _ensure_table()
            found = LoginSession.filter(key=_key_for(str(sid)))
            ids = found.values_list("id", flat=True)
            found.delete()
            _end_streams(ids)
        except Exception:  # signing out must not fail because the record couldn't be removed
            pass


def logout(request) -> None:
    """Sign this request's session out and delete its server-side record.

    The cookie is cleared in the browser *and* stops working on the server, so a copy of
    it taken earlier is dead too. The CSRF token is replaced as well.
    """
    _end_current(request)
    request.session.clear()
    _rotate_csrf(request)
    request.user = None
    request.login_session = None


def get_user(request) -> User | None:
    """The user this request's session belongs to, or None.

    A session counts only if its cookie is correctly signed, the password hasn't changed
    since it was issued, **and** its row in ``jongo_sessions`` still exists and hasn't
    outlived ``session_max_age``. Cookies issued before 0.6.0 carry no server-side id and
    are treated as signed out.
    """
    uid = request.session.get("_uid")
    sid = request.session.get("_sid")
    if not uid or not sid:
        return None
    _ensure_table()
    record = LoginSession.filter(key=_key_for(str(sid)), user_id=uid).first()
    if record is None:
        return None
    now = datetime.now(timezone.utc)
    max_age = getattr(getattr(request, "app", None), "session_max_age", None)
    if max_age and record.created is not None and now - _aware(record.created) > timedelta(seconds=max_age):
        record.delete()
        return None
    user = User.filter(id=uid).first()
    if user is None or not hmac.compare_digest(str(request.session.get("_pw", "")), _fingerprint(user)):
        return None
    if record.last_seen is None or (now - _aware(record.last_seen)).total_seconds() > _TOUCH_EVERY:
        LoginSession.filter(id=record.id).update(last_seen=now)
        record.last_seen = now
    request.login_session = record
    return user


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def sessions(user: User) -> list[LoginSession]:
    """A user's active sign-ins, newest first.

    Each has ``id``, ``created``, ``last_seen``, ``ip`` and ``user_agent`` (``to_dict()``
    gives them JSON-ready), enough for a "where you're signed in" list.

    Example::

        @server(login_required=True)
        def my_sessions(request) -> list:
            return [s.to_dict() for s in sessions(request.user)]
    """
    _ensure_table()
    return list(LoginSession.filter(user_id=user.id))


def revoke_session(session, user: User | None = None) -> bool:
    """End one sign-in. The browser holding it is signed out on its next request, and any
    ``live()`` stream it has open ends at once.

    Parameters:
        session: a :class:`LoginSession` or its ``id``.
        user: when given, only a session belonging to this user is revoked. Pass
            ``request.user`` when the id came from the browser, so nobody can end someone
            else's session by guessing ids.

    Returns:
        True if a session was ended, False if there was none to end.

    Example::

        @server(login_required=True)
        def sign_out_device(request, session_id: int) -> bool:
            return revoke_session(session_id, user=request.user)
    """
    _ensure_table()
    pk = session.pk if isinstance(session, LoginSession) else session
    found = LoginSession.filter(id=pk)
    if user is not None:
        found = found.filter(user_id=user.id)
    ids = found.values_list("id", flat=True)
    ended = found.delete()
    _end_streams(ids)
    return ended > 0


def revoke_sessions(user: User, *, keep=None) -> int:
    """End every sign-in a user has: "sign out everywhere". Their open ``live()`` streams
    end at once (except those of the sign-in ``keep`` spares).

    Parameters:
        user: whose sessions to end.
        keep: a request (or a :class:`LoginSession`) to spare, so "sign out my *other*
            devices" doesn't sign out the one asking.

    Returns:
        How many sessions were ended.

    Example::

        revoke_sessions(user)                       # an admin locking an account
        revoke_sessions(request.user, keep=request) # everywhere but here
    """
    _ensure_table()
    found = LoginSession.filter(user_id=user.id)
    spare = keep if isinstance(keep, LoginSession) else getattr(keep, "login_session", None)
    if keep is not None and spare is None and hasattr(keep, "session"):
        sid = keep.session.get("_sid")  # a request whose user hasn't been loaded yet
        spare = LoginSession.filter(key=_key_for(str(sid))).first() if sid else None
    if spare is not None:
        found = found.exclude(id=spare.pk)
    ids = found.values_list("id", flat=True)
    ended = found.delete()
    _end_streams(ids)
    return ended
