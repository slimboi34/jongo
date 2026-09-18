"""A modal form with per-field server validation.

Run it:  jongo dev examples/patterns/08_forms_and_modals.py --port 8777

What to notice
--------------
* Validation lives on the server, where it can see the database (is this email taken?).
  There is no second copy of the rules in the browser to drift out of step.
* The server returns errors as ordinary data — `{"errors": {"email": "..."}}` — and the
  component renders each message next to its field.
* The modal is just conditional UI. No portal, no library: `if open.value` or nothing.
"""

from jongo import Jongo, component, db, server, state
from jongo.html import button, div, form, h1, h2, input_, label, li, p, span, ul

app = Jongo(__name__, title="Invites")


class Invite(db.Model):
    email = db.Text(max_length=200, unique=True)
    role = db.Text(max_length=20, default="member", choices=[("member", "Member"),
                                                             ("admin", "Admin")])


@server
def send_invite(request, email: str, role: str) -> dict:
    """Validate against the database, then create. Errors come back as data."""
    errors = {}
    if "@" not in email:
        errors["email"] = "Enter an email address."
    elif Invite.filter(email__iexact=email).exists():
        errors["email"] = "That address is already invited."
    if role not in ("member", "admin"):
        errors["role"] = "Pick a role."
    if errors:
        return {"errors": errors}
    return {"invite": Invite.create(email=email, role=role).to_dict()}


@component
def InviteDialog(on_created):
    open_ = state(False)
    email = state("")
    role = state("member")
    errors = state({})
    saving = state(False)

    async def submit(event):
        saving.set(True)
        result = await send_invite(email.value, role.value)
        saving.set(False)
        if result.get("errors"):
            errors.set(result["errors"])
            return
        errors.set({})
        email.set("")
        open_.set(False)
        on_created(result["invite"])

    if not open_.value:
        return button("Invite someone", on_click=lambda e: open_.set(True))

    return div(
        div(style={"position": "fixed", "inset": 0, "background": "rgba(0,0,0,.4)"},
            on_click=lambda e: open_.set(False)),
        div(
            h2("Invite someone"),
            form(
                label("Email",
                      input_(value=email.value, on_input=lambda e: email.set(e.target.value))),
                Error(errors=errors.value, field="email"),
                label("Role",
                      input_(value=role.value, on_input=lambda e: role.set(e.target.value))),
                Error(errors=errors.value, field="role"),
                button("Send invite", disabled=saving.value),
                button("Cancel", type="button", on_click=lambda e: open_.set(False)),
                on_submit=submit,
            ),
            style={"position": "fixed", "top": "20%", "left": "50%", "transform": "translateX(-50%)",
                   "background": "white", "padding": "24px", "border_radius": "12px"},
        ),
    )


@component
def Error(errors, field):
    message = errors.get(field)
    return span(message, style={"color": "crimson"}) if message else None


@component
def Invites(initial):
    invites = state(initial)
    return div(
        InviteDialog(on_created=lambda invite: invites.set([invite, *invites.value])),
        ul([li(f"{i['email']} ({i['role']})", key=i["id"]) for i in invites.value]),
        p("No invites yet.") if not invites.value else None,
    )


@app.page("/")
def home():
    return div(h1("Invites"), Invites(initial=[i.to_dict() for i in Invite.all()]))
