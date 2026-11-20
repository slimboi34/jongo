"""One set of validation rules, enforced in the browser and on the server.

Run it:  jongo dev examples/patterns/14_shared_validation.py --port 8783

What to notice
--------------
* `Signup` is declared once. `forms.validate(Signup, values)` in the component is that
  definition compiled to JavaScript, so a typo is caught before any request is made.
* The server does not trust the browser. `data: Signup` on the `@server` function runs
  the same rules again and hands the function a cleaned dict: text stripped, `seats` an
  int, and only the fields the schema declares (an extra `is_admin` is dropped).
* What only the server can know (is this address taken?) is raised as a
  `db.ValidationError` and lands in the same `errors` state as everything else.
* `forms.Field` renders the label, the right control and the message; `required`,
  `maxlength`, `min` and `type="email"` on the inputs come from the schema too.
* `rate_limit="10/minute"` caps how often one visitor can submit.
"""

from jongo import Jongo, ServerError, component, db, form_values, forms, global_css, server, state
from jongo.html import button, div, form, h1, li, p, ul

app = Jongo(__name__, title="Sign up")

global_css({
    "body": {"font_family": "system-ui, sans-serif", "max_width": 460, "margin": "40px auto", "padding": "0 16px"},
    ".jongo-field": {"display": "block", "margin": "0 0 14px"},
    ".jongo-field-label": {"display": "block", "font_weight": 600, "margin_bottom": 4},
    ".jongo-field input:not([type=checkbox]), .jongo-field select":
        {"width": "100%", "padding": 8, "box_sizing": "border-box", "font": "inherit"},
    ".jongo-field input[type=checkbox]": {"margin_right": 8},
    ".jongo-field input[type=checkbox] + .jongo-field-label": {"display": "inline", "font_weight": 400},
    ".jongo-field [aria-invalid=true]": {"border": "1px solid crimson"},
    ".jongo-field-error": {"display": "block", "color": "crimson", "font_size": 14, "margin_top": 4},
    ".jongo-field-help": {"display": "block", "color": "#666", "font_size": 13, "margin_top": 4},
})

Signup = forms.Schema(
    "Signup",
    email=forms.Email(max_length=120),
    team=forms.Text(min_length=2, max_length=30, pattern=r"[a-z][a-z0-9-]*",
                    help="Lowercase letters, digits and dashes.",
                    messages={"pattern": "Use lowercase letters, digits and dashes, starting with a letter."}),
    plan=forms.Choice([("free", "Free"), ("team", "Team"), ("scale", "Scale")], default="free"),
    seats=forms.Int(min=1, max=50, default=1),
    password=forms.Text(min_length=8, secret=True),
    confirm=forms.Text(same_as="password", secret=True, label="Password again"),
    terms=forms.Bool(required=True, label="I accept the terms"),
)


class Account(db.Model):
    email = db.Text(max_length=120, unique=True)
    team = db.Text(max_length=30)
    plan = db.Text(max_length=10)
    seats = db.Int(default=1)


@server(rate_limit="10/minute")
def sign_up(request, data: Signup) -> dict:
    """Create an account. `data` has already passed every rule in Signup."""
    if Account.filter(email__iexact=data["email"]).exists():
        raise db.ValidationError({"email": "That address already has an account."})
    account = Account.create(email=data["email"], team=data["team"], plan=data["plan"], seats=data["seats"])
    return account.to_dict()


@component
def SignupForm(initial):
    errors = state({})
    accounts = state(initial)
    saving = state(False)

    def check_field(event):
        """As-you-leave-the-field feedback: one rule set, one field."""
        name = event.target.name
        if name not in Signup:
            return
        values = form_values(event)
        value = values.get(name)
        message = forms.check(Signup[name], value, values)
        found = {k: v for k, v in errors.value.items() if k != name}
        if message:
            found[name] = message
        errors.set(found)

    async def submit(event):
        target = event.target
        values = form_values(event)
        found = forms.validate(Signup, values)          # in the browser: no request yet
        errors.set(found)
        if found:
            return
        saving.set(True)
        try:
            account = await sign_up(values)             # on the server: the same rules, then the database
            accounts.set([account, *accounts.value])
            target.reset()
        except ServerError as exc:
            errors.set(exc.errors or {"email": str(exc)})
        saving.set(False)

    return div(
        form(
            [forms.Field(schema=Signup, name=name, error=errors.value.get(name), on_change=check_field, key=name)
             for name in Signup],
            button("Create account", disabled=saving.value),
            on_submit=submit, novalidate=True,
        ),
        p(f"{len(accounts.value)} account(s) so far:") if accounts.value else p("No accounts yet."),
        ul([li(f"{a['email']} · {a['team']} · {a['plan']} · {a['seats']} seat(s)", key=a["id"])
            for a in accounts.value]),
    )


@app.page("/")
def home():
    return div(h1("Create an account"), SignupForm(initial=[a.to_dict() for a in Account.all()]))
