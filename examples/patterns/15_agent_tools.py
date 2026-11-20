"""Hand the running app to a coding agent: MCP tools and an OpenAPI document, from the type hints.

Run it:  jongo dev examples/patterns/15_agent_tools.py --port 8784

Then, from the same machine:

    claude mcp add --transport http tasks http://127.0.0.1:8784/_jongo/mcp
    curl http://127.0.0.1:8784/_jongo/openapi.json

or, with no server running at all, let the agent start the app itself over stdio:

    claude mcp add tasks -- jongo mcp examples/patterns/15_agent_tools.py

What to notice
--------------
* `app.mcp(tools=[...])` is the whole integration. Each listed `@server` function becomes
  a tool; its name, description and input schema are read from the function itself.
* A tool call goes through the same door as a call from the browser: arguments are checked
  against the type hints, `login_required` is enforced, rate limits are counted. A bad
  call comes back as a tool error with a Jongo code (`J403`) and a hint, not a traceback.
* `delete_task` is deliberately not in the list. A function is reachable from MCP only if
  you put it there (`jongo mcp`, on your own machine, exposes them all).
* Under `jongo dev` the endpoint answers this machine only. In production it needs
  `Authorization: Bearer $JONGO_MCP_TOKEN`, and refuses everyone until that is set.
* The page below shows the tool list the agent sees, built from the same registry.
"""

from typing import Literal

from jongo import Jongo, component, db, server, state
from jongo.html import button, code, div, form, h1, h2, input_, li, p, span, ul

app = Jongo(__name__, title="Tasks")


class Task(db.Model):
    """One thing to do."""

    title = db.Text(max_length=120)
    status = db.Text(max_length=10, default="open", choices=["open", "done"])


@server
def add_task(title: str) -> dict:
    """Add a task and return it."""
    title = title.strip()
    if not title:
        raise db.ValidationError({"title": "A task needs a title."})
    return Task.create(title=title).to_dict()


@server
def list_tasks(status: Literal["open", "done", "all"] = "all") -> list:
    """List tasks, optionally only the open or the done ones."""
    found = Task.all() if status == "all" else Task.filter(status=status)
    return [task.to_dict() for task in found]


@server
def complete_task(task: Task) -> dict:
    """Mark a task done. `task` is the task's id."""
    task.status = "done"
    task.save()
    return task.to_dict()


@server
def delete_task(task: Task) -> bool:
    """Delete a task. Not exposed to agents: see app.mcp() below."""
    task.delete()
    return True


mcp = app.mcp(tools=[add_task, list_tasks, complete_task])


@component
def Tasks(initial):
    tasks = state(initial)
    draft = state("")

    async def add(event):
        if draft.value.strip():
            task = await add_task(draft.value)
            tasks.set([*tasks.value, task])
            draft.set("")

    async def reload(event):
        tasks.set(await list_tasks())

    return div(
        form(input_(value=draft.value, placeholder="A new task", on_input=lambda e: draft.set(e.target.value)),
             button("Add"), on_submit=add),
        ul([li(span("✓ " if t["status"] == "done" else "○ "), t["title"], key=t["id"]) for t in tasks.value]),
        button("Reload (after an agent changed something)", on_click=reload),
    )


@app.page("/")
def home():
    tools = [tool for tool in mcp.tools() if not tool["name"].startswith("jongo_")]
    return div(
        h1("Tasks"),
        Tasks(initial=[task.to_dict() for task in Task.all()]),
        h2("What an agent sees"),
        p("POST ", code("/_jongo/mcp"), " · ", code("/_jongo/openapi.json")),
        ul([li(code(tool["name"]), " — ", tool["description"], " ",
               code(", ".join(tool["inputSchema"]["properties"]) or "no arguments"))
            for tool in tools]),
    )
