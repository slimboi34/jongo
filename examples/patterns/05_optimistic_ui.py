"""Optimistic updates: show the result immediately, roll back if the server disagrees.

Run it:  jongo dev examples/patterns/05_optimistic_ui.py --port 8774

What to notice
--------------
* The checkbox flips the instant you click it, before the round trip finishes.
* If the server call fails, the previous value is put back and the error is shown —
  which is the half people usually forget.
* Because the old value is captured before the call, two quick clicks can't
  leave the UI showing something the server never agreed to.
"""

from jongo import Jongo, ServerError, component, db, server, state
from jongo.html import div, h1, input_, label, li, p, ul

app = Jongo(__name__, title="Tasks")


class Task(db.Model):
    title = db.Text(max_length=200)
    done = db.Bool(default=False)


@server
def set_done(request, task: Task, done: bool) -> dict:
    task.done = done
    task.save()
    return task.to_dict()


@component
def TaskRow(task):
    done = state(task["done"])
    error = state("")

    async def toggle(event):
        previous = done.value
        done.set(not previous)                 # optimistic: paint it now
        error.set("")
        try:
            result = await set_done(task["id"], not previous)
            done.set(result["done"])           # settle on what the server stored
        except ServerError as exc:
            done.set(previous)                 # roll back
            error.set(str(exc) or "Could not save — try again.")

    return li(
        label(
            input_(type="checkbox", checked=done.value, on_change=toggle),
            task["title"],
        ),
        p(error.value, style={"color": "crimson"}) if error.value else None,
    )


@app.page("/")
def home():
    return div(
        h1("Tasks"),
        ul([TaskRow(task=t.to_dict(), key=t.pk) for t in Task.all()]),
    )
