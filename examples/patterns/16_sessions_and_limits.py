"""Sign-ins you can see and revoke, and a login that can't be brute-forced.

Run it:  jongo dev examples/patterns/16_sessions_and_limits.py --port 8785
         (sign up with any username; open a second browser to see two sessions)

What to notice
--------------
* Every `login()` is a row the server keeps. `auth.sessions(user)` lists them with the
  address and browser, so the user can see where they are signed in.
* `auth.revoke_session(id, user=request.user)` signs one browser out; `revoke_sessions(
  user, keep=request)` signs out every browser but this one. The revoked browser still
  holds a correctly signed cookie, and is refused anyway on its next request.
* `authenticate(username, password, request)` counts wrong passwords by itself. After 10
  for one username in 5 minutes it stops checking and raises a 429 (code J213), which
  this page shows as a message. Nothing to configure.
* `rate_limit="5/minute"` on sign-up stops one visitor creating accounts in a loop.
* Passing `user=request.user` when revoking matters: the id comes from the browser, and
  without it anyone could end anyone's session by counting upwards.
"""

from jongo import Jongo, ServerError, auth, component, db, redirect, refresh, server, state
from jongo.html import b, button, div, form, h1, h2, input_, label, li, p, small, span, ul
from jongo.ratelimit import TooManyRequests

app = Jongo(__name__, title="Where you're signed in")


@server(rate_limit="5/minute")
def sign_up(request, username: str, password: str) -> dict:
    username = username.strip()
    if len(username) < 2 or len(password) < 6:
        raise db.ValidationError({"username": "Pick a username (2+) and a password (6+ characters)."})
    if auth.User.filter(username=username).exists():
        raise db.ValidationError({"username": "That username is taken."})
    auth.login(request, auth.User.create_user(username, password))
    return {"ok": True}


@server
def sign_in(request, username: str, password: str) -> dict:
    try:
        user = auth.authenticate(username, password, request)    # throttled: raises a 429 when over
    except TooManyRequests as exc:
        raise db.ValidationError({"username": exc.message}) from None
    if user is None:
        raise db.ValidationError({"username": "Wrong username or password."})
    auth.login(request, user)
    return {"ok": True}


@server(login_required=True)
def sign_out(request) -> bool:
    auth.logout(request)
    return True


@server(login_required=True)
def end_session(request, session_id: int) -> bool:
    """Sign one of *my* browsers out. `user=` keeps it to my own sessions."""
    return auth.revoke_session(session_id, user=request.user)


@server(login_required=True)
def end_other_sessions(request) -> int:
    return auth.revoke_sessions(request.user, keep=request)


@component
def Door():
    username = state("")
    password = state("")
    error = state("")

    async def go(action):
        error.set("")
        try:
            await action(username.value, password.value)
            refresh()
        except ServerError as exc:
            error.set((exc.errors or {}).get("username") or str(exc))

    return form(
        label("Username ", input_(value=username.value, on_input=lambda e: username.set(e.target.value))),
        label(" Password ", input_(type="password", value=password.value,
                                   on_input=lambda e: password.set(e.target.value))),
        button("Sign in", on_click=lambda e: go(sign_in), type="button"),
        button("Sign up", on_click=lambda e: go(sign_up), type="button"),
        p(error.value, style={"color": "crimson"}, role="alert") if error.value else None,
        on_submit=lambda e: go(sign_in),
    )


@component
def Sessions(sessions, current):
    async def end(session_id):
        await end_session(session_id)
        refresh()

    async def end_others(event):
        await end_other_sessions()
        refresh()

    async def leave(event):
        await sign_out()
        refresh()

    return div(
        ul([
            li(
                b("This browser" if s["id"] == current else "Another browser"),
                span(f" · {s['ip'] or 'unknown address'} · signed in {s['created'][:16].replace('T', ' ')} "),
                small((s["user_agent"] or "unknown browser")[:60]),
                None if s["id"] == current else button("Sign out", on_click=lambda e: end(s["id"])),
                key=s["id"],
            )
            for s in sessions
        ]),
        button("Sign out everywhere else", on_click=end_others, disabled=len(sessions) < 2),
        button("Sign out", on_click=leave),
    )


@app.page("/")
def home(request):
    if request.user is None:
        return div(h1("Sign in"), Door())
    rows = [s.to_dict() for s in auth.sessions(request.user)]
    return div(
        h1(f"Hello, {request.user.username}"),
        h2(f"You are signed in on {len(rows)} browser(s)"),
        Sessions(sessions=rows, current=request.login_session.id),
    )
