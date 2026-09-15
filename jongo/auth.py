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
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from . import db

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 480_000


def hash_password(password: str, *, iterations: int | None = None) -> str:
    iterations = iterations or ITERATIONS
    salt = secrets.token_urlsafe(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return f"{ALGORITHM}${iterations}${salt}${base64.b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
    except (ValueError, AttributeError):
        return False
    if algorithm != ALGORITHM:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
    return hmac.compare_digest(base64.b64encode(digest).decode(), expected)


_dummy_hash: str | None = None


def _dummy() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(12))
    return _dummy_hash


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
        return bool(self.password) and verify_password(raw_password, self.password)

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


def authenticate(username: str, password: str) -> User | None:
    user = User.filter(username=username).first()
    if user is None:
        verify_password(password, _dummy())  # same work either way, so timing reveals nothing
        return None
    return user if user.check_password(password) else None


def _fingerprint(user: User) -> str:
    # Changing the password changes the fingerprint, which signs out other sessions.
    return hashlib.sha256(f"jongo-auth:{user.password}".encode()).hexdigest()[:20]


def session_data_for(user: User) -> dict:
    return {"_uid": user.id, "_pw": _fingerprint(user)}


def login(request, user: User) -> None:
    request.session.clear()
    request.session.update(session_data_for(user))
    user.last_login = datetime.now(timezone.utc)
    user.save()
    request.user = user


def logout(request) -> None:
    request.session.clear()
    request.user = None


def get_user(request) -> User | None:
    uid = request.session.get("_uid")
    if not uid:
        return None
    user = User.filter(id=uid).first()
    if user is None or not hmac.compare_digest(str(request.session.get("_pw", "")), _fingerprint(user)):
        return None
    return user
