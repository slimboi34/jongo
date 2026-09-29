"""The app describes itself, and reports its problems as data.

`jongo context` and `jongo check` exist for the coding agent working on an app: the map
it needs before it starts, and the feedback it needs after every change. These tests pin
what they promise.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from jongo import Jongo, broadcast, component, context, db, describe, server, state
from jongo.html import *
from jongo.introspect import check

app = Jongo(__name__, title="Agentic test app", database=":memory:")


class Author(db.Model):
    """Who wrote it."""

    name = db.Text(max_length=80)


class Post(db.Model):
    title = db.Text(max_length=120)
    body = db.Text(blank=True, default="")
    published = db.Bool(default=False)
    author = db.ForeignKey(Author, on_delete="cascade")

    class Meta:
        ordering = ["-id"]


@server
def publish(request, post: Post, note: str = "") -> dict:
    """Publish a post."""
    post.published = True
    post.save()
    return post.to_dict()


@server(login_required=True, refresh=True)
def rename(post: Post, title: str) -> dict:
    post.title = title
    post.save()
    return post.to_dict()


@app.channel("post:<int:id>")
def post_channel(request, id):
    return True


@component
def PostList(posts, heading="Posts"):
    """The list."""
    items = state(posts)
    return ul([li(p["title"], key=p["id"]) for p in items.value])


@app.layout
@component
def Shell(children):
    return div(children)


@app.page("/", title="Home")
def home():
    return PostList(posts=[p.to_dict() for p in Post.all()])


@app.page("/posts/<int:id>", login_required=True)
def post_page(id: int):
    return div(str(id))


@app.route("/api/ping", methods=("POST",), csrf=False)
def ping():
    return {"pong": True}


def test_describe_lists_what_the_app_declares():
    d = describe(app)
    assert d["app"]["title"] == "Agentic test app"
    assert d["app"]["layout"] == "Shell"
    assert d["app"]["database"] == {"backend": "sqlite", "database": ":memory:"}
    assert d["app"]["admin"] is None

    mine = lambda items: {i["name"]: i for i in items if i["file"] == "test_agentic.py"}  # noqa: E731
    names = mine(d["models"])
    assert set(names) >= {"Author", "Post"}
    post = names["Post"]
    assert post["table"] == "post" and post["ordering"] == ["-id"]
    fields = {f["name"]: f for f in post["fields"]}
    assert fields["title"]["type"] == "Text" and fields["title"]["max_length"] == 120
    assert fields["published"]["default"] is False
    assert fields["author"]["to"] == "Author" and fields["author"]["column"] == "author_id"
    assert names["Author"]["doc"] == "Who wrote it."

    fns = mine(d["server_functions"])
    assert fns["publish"]["signature"] == "publish(request, post: Post, note: str = '') -> dict"
    assert fns["publish"]["takes_request"] is True
    assert fns["publish"]["params"][0] == {"name": "post", "type": "Post", "model": "Post"}
    assert fns["publish"]["params"][1] == {"name": "note", "type": "str", "default": ""}
    assert fns["publish"]["doc"] == "Publish a post."
    assert fns["rename"]["login_required"] and fns["rename"]["refresh"]
    assert fns["publish"]["file"] == "test_agentic.py" and isinstance(fns["publish"]["line"], int)

    pages = {p["path"]: p for p in d["pages"]}
    assert pages["/"]["title"] == "Home" and pages["/"]["name"] == "home"
    assert pages["/posts/<int:id>"]["params"] == {"id": "int"}
    assert pages["/posts/<int:id>"]["login_required"] is True
    routes = {r["path"]: r for r in d["routes"]}
    assert routes["/api/ping"]["methods"] == ["POST"] and routes["/api/ping"]["csrf"] is False

    comps = mine(d["components"])
    assert comps["PostList"]["props"] == [{"name": "posts"}, {"name": "heading", "default": "Posts"}]
    assert comps["PostList"]["doc"] == "The list."
    assert comps["Shell"]["takes_children"] and comps["Shell"]["layout"] is True

    assert d["channels"] == [{"pattern": "post:<int:id>", "guard": "post_channel", "params": ["id"]}]
    # nothing the framework registers for itself leaks into the app's own map
    assert all(not s["id"].startswith("jongo.") for s in d["server_functions"])
    json.dumps(d)  # the whole thing is data


def test_context_is_the_rules_then_the_map():
    text = context(app)
    assert text.startswith("# Agentic test app — a Jongo app, described for a coding agent")
    for heading in ("## How to work here", "## The rules of the framework", "## Models (", "## Server functions (",
                    "## Pages (", "## Routes (", "## Components (", "## Channels ("):
        assert heading in text, heading
    assert "`publish(request, post: Post, note: str = '') -> dict`" in text
    assert "| `author` | ForeignKey | → `Author` (on delete cascade; column `author_id`) |" in text
    assert "GET `/posts/<int:id>` → `post_page` · params id:int · login required" in text
    assert "`PostList(posts, heading = 'Posts')`" in text
    assert "`Shell(children)`" in text and "the layout" in text
    assert "- `post:<int:id>` — guarded by `post_channel` · params id" in text
    assert "no database calls" in text  # the rule an agent most often breaks


def test_check_reports_pending_migrations_and_passes():
    db.configure(":memory:")  # a database with no tables yet, whatever ran before this test
    report = check(app)
    assert report["ok"] is True and report["problems"] == []
    assert report["counts"]["models"] >= 2 and report["counts"]["server_functions"] >= 2
    described = " ".join(op["describe"] for op in report["pending_migrations"])
    assert "post" in described.lower()
    json.dumps(report)


BROKEN = textwrap.dedent('''
    from jongo import Jongo, component, db
    from jongo.html import *

    app = Jongo(__name__, database="db.sqlite3")


    class Thing(db.Model):
        name = db.Text()


    @component
    def Bad():
        import os                      # there is no `os` in the browser
        return div(os.getcwd())


    @app.page("/")
    def home():
        return Bad()
''')


def _cli(*argv, cwd):
    return subprocess.run([sys.executable, "-m", "jongo.cli", *argv], cwd=cwd, capture_output=True, text=True,
                          timeout=120)


def test_cli_check_json_is_data_an_agent_can_act_on(tmp_path):
    (tmp_path / "app.py").write_text(BROKEN)
    proc = _cli("check", "--json", cwd=tmp_path)
    assert proc.returncode == 1, proc.stderr
    report = json.loads(proc.stdout)
    assert report["ok"] is False
    problem = report["problems"][0]
    assert problem["kind"] == "compile"
    assert problem["file"] and problem["file"].endswith("app.py")
    assert isinstance(problem["line"], int)
    assert problem["message"]
    assert report["counts"]["components"] == 1

    (tmp_path / "app.py").write_text(BROKEN.replace("    import os                      # there is no `os` in the browser\n"
                                                    "    return div(os.getcwd())", "    return div('fixed')"))
    proc = _cli("check", "--json", cwd=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["ok"] is True and report["problems"] == []
    assert any("thing" in op["describe"].lower() for op in report["pending_migrations"])


def test_cli_context_and_routes_json(tmp_path):
    (tmp_path / "app.py").write_text(BROKEN.replace("    import os                      # there is no `os` in the browser\n"
                                                    "    return div(os.getcwd())", "    return div('ok')"))
    proc = _cli("context", cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "## The rules of the framework" in proc.stdout and "### `Thing` — table `thing`" in proc.stdout
    proc = _cli("context", "-o", "AGENTS.md", cwd=tmp_path)
    assert proc.returncode == 0 and (tmp_path / "AGENTS.md").read_text().startswith("# ")
    proc = _cli("routes", "--json", cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["pages"][0]["path"] == "/"


def test_new_project_ships_agent_instructions(tmp_path):
    proc = _cli("new", "site", cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    agents = (tmp_path / "site" / "AGENTS.md").read_text()
    assert agents.startswith("# site — notes for coding agents")
    assert "jongo check --json" in agents and "jongo context" in agents
    assert (tmp_path / "site" / "CLAUDE.md").read_text().strip() == "@AGENTS.md"
    # and the scaffold itself checks clean
    proc = _cli("check", "--json", cwd=tmp_path / "site")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["ok"] is True
