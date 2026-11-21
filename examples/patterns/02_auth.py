"""Auth: sign in, sign out, and pages only a signed-in user can see.

Run it:  jongo dev examples/patterns/02_auth.py --port 8771
         (then `jongo createadmin` in another shell, or use the sign-up form)

What to notice
--------------
* `login_required=True` on a page redirects anonymous visitors to `login_url`.
* `request.user` is available in pages, routes and `@server` functions.
* The sign-in form is a classic HTML POST, so it works with JavaScript switched off.
  A classic form needs the CSRF token as a hidden input; browser code sends it itself.
* `?next=` comes from the URL, so anyone can write a link with their own value in it.
  `is_local_url()` keeps the redirect after sign-in on this site: `https://evil.example`,
  `//evil.example` and `/\\evil.example` all fall back to `/`.
* `authenticate(username, password, request)` gets the request, so wrong passwords are
  also counted per address; over the limit it raises a 429, shown here as a message.
"""

from urllib.parse import quote

from jongo import Jongo, component, redirect, state
from jongo.auth import User, authenticate, login, logout
from jongo.html import a, button, div, form, h1, input_, label, p
from jongo.http import is_local_url
from jongo.ratelimit import TooManyRequests

app = Jongo(__name__, title="Accounts", login_url="/login")


@app.page("/")
def home(request):
    if request.user is None:
        return div(h1("Welcome"), p("You are signed out."), _link("/login", "Sign in"))
    return div(
        h1(f"Hello, {request.user.username}"),
        _link("/account", "Your account"),
        form(input_(type="hidden", name="csrf_token", value=request.csrf_token),
             button("Sign out"), method="post", action="/logout"),
    )


@app.page("/account", login_required=True)          # anonymous -> /login?next=/account
def account(request):
    return div(h1("Your account"), p(f"Signed in as {request.user.username}."))


def safe_next(value) -> str:
    """Where to go after signing in: a path on this site, never somewhere else."""
    return value if value and is_local_url(value) else "/"


ERRORS = {"1": "Wrong username or password.", "throttled": "Too many attempts. Try again in a few minutes."}


@app.page("/login")
def login_page(request):
    error = ERRORS.get(request.query.get("error", ""))
    return div(h1("Sign in"), p(error, role="alert") if error else None,
               SignInForm(next=safe_next(request.query.get("next")), csrf=request.csrf_token))


@app.route("/login", methods=("POST",))
def do_login(request):
    """A plain form POST: no JavaScript involved."""
    destination = safe_next(request.form.get("next"))
    try:
        user = authenticate(request.form.get("username", ""), request.form.get("password", ""), request)
    except TooManyRequests:
        return redirect("/login?error=throttled&next=" + quote(destination))
    if user is None:
        return redirect("/login?error=1&next=" + quote(destination))
    login(request, user)
    return redirect(destination)


@app.route("/logout", methods=("POST",))
def do_logout(request):
    logout(request)
    return redirect("/")


@component
def SignInForm(next, csrf):
    username = state("")
    password = state("")
    return form(
        label("Username", input_(name="username", value=username.value,
                                 on_input=lambda e: username.set(e.target.value))),
        label("Password", input_(name="password", type="password", value=password.value,
                                 on_input=lambda e: password.set(e.target.value))),
        input_(type="hidden", name="next", value=next),
        input_(type="hidden", name="csrf_token", value=csrf),
        button("Sign in"),
        method="post", action="/login",
    )


def _link(href, text):
    return a(text, href=href)


@app.route("/signup", methods=("POST",))
def signup(request):
    """Creating the first account; in a real app, validate and rate-limit this."""
    User.create_user(username=request.form["username"], password=request.form["password"])
    return redirect("/login")
