"""CRUD: list, create, edit and delete — the shape most apps start from.

Run it:  jongo dev examples/patterns/01_crud.py --port 8770

What to notice
--------------
* One file holds the table, the server functions and the UI.
* `@server` functions are called from the browser with a plain `await`. There is no
  endpoint, no fetch and no JSON parsing.
* The list lives in component state, so the page never reloads: each server call
  returns the new row and the component splices it into what it already has.
* Validation errors come back from the server as data and render next to the field.
"""

from jongo import Jongo, component, db, server, state
from jongo.html import article, button, div, form, h1, h2, input_, li, p, span, ul

app = Jongo(__name__, title="Notes")


class Note(db.Model):
    title = db.Text(max_length=120)
    body = db.Text(blank=True, default="")

    class Meta:
        ordering = ["-id"]


# -- server -----------------------------------------------------------------------------


@server
def create_note(request, title: str, body: str = "") -> dict:
    """Create a row. A ValidationError is returned to the browser as data, not a crash."""
    try:
        return Note.create(title=title, body=body).to_dict()
    except db.ValidationError as error:
        return {"errors": error.errors}


@server
def update_note(request, note: Note, title: str, body: str) -> dict:
    """`note: Note` loads the row by id — or 404s before your code runs."""
    note.title, note.body = title, body
    try:
        note.save()
    except db.ValidationError as error:
        return {"errors": error.errors}
    return note.to_dict()


@server
def delete_note(request, note: Note) -> dict:
    note.delete()
    return {"ok": True}


# -- ui ---------------------------------------------------------------------------------


@component
def NoteRow(note, on_change):
    editing = state(False)
    title = state(note["title"])
    body = state(note["body"])
    errors = state({})

    async def save(event):
        result = await update_note(note["id"], title.value, body.value)
        if result.get("errors"):
            errors.set(result["errors"])
            return
        errors.set({})
        editing.set(False)
        on_change("update", result)

    async def remove(event):
        await delete_note(note["id"])
        on_change("delete", note)

    if not editing.value:
        return li(
            div(
                h2(note["title"]),
                p(note["body"] or "—"),
                button("Edit", on_click=lambda e: editing.set(True)),
                button("Delete", on_click=remove),
            ),
        )
    return li(
        form(
            input_(value=title.value, on_input=lambda e: title.set(e.target.value)),
            _field_error(errors.value, "title"),
            input_(value=body.value, on_input=lambda e: body.set(e.target.value)),
            button("Save"),
            button("Cancel", type="button", on_click=lambda e: editing.set(False)),
            on_submit=save,
        ),
    )


@component
def NoteList(notes):
    items = state(notes)
    draft = state("")
    errors = state({})

    async def add(event):
        result = await create_note(draft.value)
        if result.get("errors"):
            errors.set(result["errors"])
            return
        errors.set({})
        draft.set("")
        items.set([result, *items.value])

    def changed(action, note):
        if action == "delete":
            items.set([n for n in items.value if n["id"] != note["id"]])
        else:
            items.set([note if n["id"] == note["id"] else n for n in items.value])

    return article(
        form(
            input_(value=draft.value, placeholder="New note",
                   on_input=lambda e: draft.set(e.target.value)),
            button("Add"),
            on_submit=add,
        ),
        _field_error(errors.value, "title"),
        ul([NoteRow(note=note, on_change=changed, key=note["id"]) for note in items.value]),
    )


def _field_error(errors, field):
    """A server-side validation message, rendered where the field is."""
    message = errors.get(field)
    return span(message, style={"color": "crimson"}) if message else None


@app.page("/")
def home():
    return div(h1("Notes"), NoteList(notes=[n.to_dict() for n in Note.all()]))
