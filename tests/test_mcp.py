"""The MCP server: an agent lists the app's tools and calls them, over stdio and over HTTP."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import use_test_database
from jongo import Forbidden, Jongo, auth, db, forms, ratelimit, redirect, rpc, server
from jongo.errors import JongoError
from jongo.html import div, h1
from jongo.mcp import PROTOCOL_VERSIONS, McpServer, serve_stdio
from jongo.schema import function_schema
from jongo.testing import TestClient

HERE = Path(__file__).parent
BUILTIN = ["jongo_context", "jongo_describe", "jongo_check", "jongo_doctor", "jongo_explain", "jongo_status",
           "jongo_openapi", "jongo_get"]


@pytest.fixture()
def app(monkeypatch):
    saved_models, saved_functions = dict(db.models_registry), dict(rpc.SERVER_FUNCTIONS)
    db.models_registry.clear()
    rpc.SERVER_FUNCTIONS.clear()      # the registry is global: other test modules' functions are in it
    db.models_registry.update({"User": auth.User, "LoginSession": auth.LoginSession})
    use_test_database()
    monkeypatch.setattr(auth, "ITERATIONS", 1000)
    monkeypatch.delenv("JONGO_MCP_TOKEN", raising=False)
    monkeypatch.delenv("JONGO_ENV", raising=False)
    ratelimit.limiter.reset()
    auth.limiter.reset()
    app = Jongo("mcp_app", root=HERE, title="Notes", secret_key="k" * 40, dev=True)

    class Note(db.Model):
        text = db.Text(max_length=100)
        pinned = db.Bool(default=False)

    NoteForm = forms.Schema("NoteForm", text=forms.Text(min_length=2, max_length=100), pinned=forms.Bool())

    @server
    def add_note(request, text: str, pinned: bool = False) -> dict:
        """Add a note and return it."""
        return Note.create(text=text, pinned=pinned).to_dict()

    @server
    def list_notes() -> list:
        return [n.to_dict() for n in Note.all()]

    @server
    def pin(request, note: Note) -> dict:
        note.pinned = True
        note.save()
        return note.to_dict()

    @server
    def save_form(request, data: NoteForm) -> dict:
        return Note.create(**data).to_dict()

    @server(login_required=True)
    def whoami(request) -> str:
        return request.user.username

    @server(admin_required=True, rate_limit="2/minute")
    def wipe(request) -> int:
        return Note.all().delete()

    @server
    def explode(request) -> int:
        raise RuntimeError("the secret database password is hunter2")

    @server
    def refuse(request) -> int:
        raise Forbidden("not for you")

    @server
    def go(request):
        return redirect("/elsewhere")

    @app.page("/")
    def home(request):
        return div(h1("Notes"), f"{Note.all().count()} notes for {request.user.username if request.user else 'nobody'}")

    @app.page("/private", login_required=True)
    def private(request):
        return div("secret")

    db.migrate()
    app.t = type("T", (), dict(Note=Note, add_note=add_note, list_notes=list_notes, pin=pin, save_form=save_form,
                               whoami=whoami, wipe=wipe, explode=explode, refuse=refuse, go=go))
    yield app
    ratelimit.limiter.reset()
    auth.limiter.reset()
    db.close_connections()
    db.models_registry.clear()
    db.models_registry.update(saved_models)
    rpc.SERVER_FUNCTIONS.clear()
    rpc.SERVER_FUNCTIONS.update(saved_functions)


def mine(app):
    """The app's own functions (the registry is global, so other test modules' are there too)."""
    t = app.t
    return [t.add_note, t.list_notes, t.pin, t.save_form, t.whoami, t.wipe, t.explode, t.refuse, t.go]


def rpc_call(server_, method, params=None, id_=1):
    message = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        message["params"] = params
    return server_.handle(message)


# ================================================================================ protocol ==


def test_initialize_negotiates_the_protocol_version(app):
    mcp = McpServer(app, tools=mine(app))
    for version in PROTOCOL_VERSIONS:
        result = rpc_call(mcp, "initialize", {"protocolVersion": version, "capabilities": {},
                                              "clientInfo": {"name": "t", "version": "0"}})["result"]
        assert result["protocolVersion"] == version
    result = rpc_call(mcp, "initialize", {"protocolVersion": "1999-01-01"})["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSIONS[0], "an unknown version is answered with the newest"
    assert result["serverInfo"]["name"] == "jongo-mcp_app" and result["serverInfo"]["title"] == "Notes"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert "jongo_context" in result["instructions"]


def test_notifications_get_no_reply_and_unknown_methods_get_an_error(app):
    mcp = McpServer(app, tools=None)
    assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert mcp.handle({"jsonrpc": "2.0", "method": "no/such", "params": {}}) is None, "even a bad notification"
    assert rpc_call(mcp, "ping")["result"] == {}
    unknown = rpc_call(mcp, "no/such")
    assert unknown["error"]["code"] == -32601 and unknown["id"] == 1
    assert mcp.handle({"id": 3, "method": "ping"})["error"]["code"] == -32600        # no "jsonrpc"
    assert mcp.handle("nonsense")["error"]["code"] == -32600
    assert mcp.handle({"jsonrpc": "2.0", "id": 9, "result": {}}) is None, "a stray response is ignored"
    bad = rpc_call(mcp, "tools/call", {"arguments": {}})
    assert bad["error"]["code"] == -32602 and bad["error"]["data"]["code"] == "J422"
    assert rpc_call(mcp, "tools/call", [1])["error"]["data"]["code"] == "J422"
    assert rpc_call(mcp, "prompts/list")["result"] == {"prompts": []}


def test_the_tool_list_is_the_builtins_plus_the_exposed_functions(app):
    assert [t["name"] for t in McpServer(app, tools=None).tools()] == BUILTIN
    mcp = McpServer(app, tools=[app.t.add_note, app.t.wipe])
    tools = {t["name"]: t for t in rpc_call(mcp, "tools/list")["result"]["tools"]}
    assert list(tools) == BUILTIN + ["add_note", "wipe"]
    add = tools["add_note"]
    assert add["description"] == "Add a note and return it."
    assert add["inputSchema"] == function_schema(app.t.add_note) == {
        "type": "object", "properties": {"text": {"type": "string"},
                                         "pinned": {"type": "boolean", "default": False}},
        "required": ["text"], "additionalProperties": False}
    assert add["_meta"] == {"jongo/function": app.t.add_note.id}
    assert tools["wipe"]["description"].endswith("Admins only. Rate limit: 2/minute.")
    for tool in tools.values():
        assert tool["inputSchema"]["type"] == "object" and len(tool["name"]) <= 64
        assert tool["name"].replace("_", "").replace("-", "").isalnum()
    assert tools["jongo_check"]["annotations"]["readOnlyHint"] is True
    json.dumps(tools)


def test_all_exposes_every_function_the_app_defines_and_qualifies_clashes(app):
    names = {t["name"] for t in McpServer(app, tools="all").tools()}
    assert {"add_note", "list_notes", "pin", "whoami"} <= names
    assert not any(n.startswith("jongo_") for n in names - set(BUILTIN))

    def make(module):
        def add_note(text: str) -> str:
            return text
        add_note.__module__ = module
        add_note.__qualname__ = "add_note"
        return server(add_note)

    clash = make("other_module")
    tools = [t["name"] for t in McpServer(app, tools=[app.t.add_note, clash]).tools()]
    assert set(tools[-2:]) == {app.t.add_note.id.replace(".", "_"), "other_module_add_note"}

    with pytest.raises(JongoError) as caught:
        McpServer(app, tools=[lambda: None])
    assert caught.value.code == "J421"


# ============================================================================ calling tools ==


def test_a_tool_call_runs_the_server_function_with_validated_arguments(app):
    mcp = McpServer(app, tools=mine(app))
    result = rpc_call(mcp, "tools/call", {"name": "add_note", "arguments": {"text": "from an agent"}})["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == {"id": 1, "text": "from an agent", "pinned": False}
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert app.t.Note.get(id=1).text == "from an agent"

    listed = mcp.call("list_notes")
    assert listed["structuredContent"] == {"result": [{"id": 1, "text": "from an agent", "pinned": False}]}, \
        "a list is wrapped, because structuredContent must be an object"
    assert mcp.call("pin", {"note": 1})["structuredContent"]["pinned"] is True      # a model argument
    assert mcp.call("go")["structuredContent"] == {"redirect": "/elsewhere"}


@pytest.mark.parametrize("name, arguments, code, status", [
    ("add_note", {"text": 5}, "J403", 400),
    ("add_note", {}, "J404", 400),
    ("add_note", {"text": "x", "colour": "red"}, "J404", 400),
    ("pin", {"note": 999}, "J411", 404),
    ("whoami", {}, "J208", 401),
    ("wipe", {}, "J208", 401),
    ("refuse", {}, "J210", 403),
])
def test_a_failed_call_is_a_tool_error_with_the_jongo_code(app, name, arguments, code, status):
    result = McpServer(app, tools=mine(app)).call(name, arguments)
    assert result["isError"] is True
    error = result["structuredContent"]["error"]
    assert error["code"] == code and error["status"] == status and error["explain"] == f"jongo explain {code}"
    assert result["content"][0]["text"].startswith(f"[{code}] ")


def test_a_schema_parameter_is_validated_and_field_errors_come_back(app):
    mcp = McpServer(app, tools=mine(app))
    tool = [t for t in mcp.tools() if t["name"] == "save_form"][0]
    assert tool["inputSchema"]["properties"]["data"]["properties"]["text"]["minLength"] == 2
    bad = mcp.call("save_form", {"data": {"text": "x"}})
    assert bad["isError"] and bad["structuredContent"]["error"]["code"] == "J304"
    assert bad["structuredContent"]["error"]["errors"] == {"text": "Text must be at least 2 characters."}
    good = mcp.call("save_form", {"data": {"text": "fine", "pinned": True, "id": 77}})
    assert good["structuredContent"] == {"id": 1, "text": "fine", "pinned": True}


def test_an_exception_shows_its_details_in_dev_and_nothing_in_production(app):
    dev = McpServer(app, tools=mine(app), detail=True).call("explode")
    error = dev["structuredContent"]["error"]
    assert error["code"] == "J407" and error["type"] == "RuntimeError" and "hunter2" in error["message"]
    assert error["file"] == "test_mcp.py" and "Traceback" in error["traceback"]

    prod = McpServer(app, tools=mine(app), detail=False).call("explode")
    error = prod["structuredContent"]["error"]
    assert error["message"] == "Server error" and "hunter2" not in json.dumps(prod)
    assert "traceback" not in error and "file" not in error and error["code"] == "J407"
    recorded = app.monitor.snapshot()["errors"]
    assert recorded and recorded[0]["code"] == "J407" and recorded[0]["method"] == "MCP"


def test_act_as_decides_who_a_call_runs_as(app):
    ann = auth.User.create_user("ann", "pw")
    root = auth.User.create_user("root", "pw", is_admin=True)
    assert McpServer(app, tools=mine(app)).call("whoami")["isError"]
    assert McpServer(app, tools=mine(app), act_as=ann).call("whoami")["structuredContent"] == {"result": "ann"}
    seen = []
    chosen = McpServer(app, tools=mine(app), act_as=lambda request: seen.append(request) or root)
    assert chosen.call("whoami")["structuredContent"] == {"result": "root"}
    assert seen and seen[0].app is app
    assert McpServer(app, tools=mine(app), act_as=ann).call("wipe")["structuredContent"]["error"]["code"] == "J209"


def test_rate_limits_apply_to_tool_calls(app):
    root = auth.User.create_user("root", "pw", is_admin=True)
    mcp = McpServer(app, tools=mine(app), act_as=root)
    assert [mcp.call("wipe")["isError"] for _ in range(3)] == [False, False, True]
    error = mcp.call("wipe")["structuredContent"]["error"]
    assert error["code"] == "J213" and error["status"] == 429 and error["retry_after"] >= 1


def test_unknown_tools_and_unexposed_functions_are_protocol_errors(app):
    mcp = McpServer(app, tools=[app.t.add_note])
    for name in ("nope", "wipe"):
        reply = rpc_call(mcp, "tools/call", {"name": name, "arguments": {}})
        assert reply["error"]["code"] == -32602 and reply["error"]["data"]["code"] == "J421"
    assert app.t.Note.all().count() == 0


# =========================================================================== built-in tools ==


def test_the_builtin_tools_return_the_same_reports_as_the_cli(app):
    mcp = McpServer(app, tools=mine(app))
    context = mcp.call("jongo_context")
    assert context["content"][0]["text"].startswith("# Notes — a Jongo app") and "structuredContent" not in context
    described = mcp.call("jongo_describe")["structuredContent"]
    assert described["app"]["name"] == "mcp_app" and "server_functions" in described
    assert list(mcp.call("jongo_describe", {"section": "models"})["structuredContent"]) == ["models"]
    assert mcp.call("jongo_describe", {"section": "nope"})["isError"]
    check = mcp.call("jongo_check")["structuredContent"]
    assert set(check) == {"jongo", "app", "ok", "problems", "counts", "pending_migrations"}
    doctor = mcp.call("jongo_doctor", {"production": True})["structuredContent"]
    assert doctor["production"] is True and "checks" in doctor
    explained = mcp.call("jongo_explain", {"code": "J302"})["structuredContent"]
    assert explained["code"] == "J302" and "jongo migrate" in explained["fix"]
    codes = mcp.call("jongo_explain")["structuredContent"]["codes"]
    assert {"J213", "J412", "J420"} <= {c["code"] for c in codes}
    assert mcp.call("jongo_explain", {"code": "J999"})["isError"]
    assert mcp.call("jongo_status")["structuredContent"]["app"] == "mcp_app"
    assert f"/_jongo/rpc/{app.t.add_note.id}" in mcp.call("jongo_openapi")["structuredContent"]["paths"]


def test_jongo_get_renders_a_page_in_process(app):
    ann = auth.User.create_user("ann", "pw")
    mcp = McpServer(app, tools=mine(app))
    mcp.call("add_note", {"text": "one"})
    page = mcp.call("jongo_get", {"path": "/"})["structuredContent"]
    assert page["status"] == 200 and page["content_type"].startswith("text/html")
    assert "<h1>Notes</h1>" in page["body"] and "1 notes for nobody" in page["body"] and not page["truncated"]
    assert mcp.call("jongo_get", {"path": "/nope"})["structuredContent"]["status"] == 404
    private = mcp.call("jongo_get", {"path": "/private"})["structuredContent"]
    assert private["status"] == 302 and private["location"].startswith("/login?next=")
    as_ann = McpServer(app, tools=mine(app), act_as=ann)
    assert "notes for ann" in as_ann.call("jongo_get", {"path": "/"})["structuredContent"]["body"]
    assert as_ann.call("jongo_get", {"path": "/private"})["structuredContent"]["status"] == 200
    for bad in ("https://example.com/", "//example.com", "relative", 5):
        assert mcp.call("jongo_get", {"path": bad})["isError"]


def test_resources_are_the_context_pack_and_the_openapi_document(app):
    mcp = McpServer(app, tools=None)
    listed = rpc_call(mcp, "resources/list")["result"]["resources"]
    assert [r["uri"] for r in listed] == ["jongo://context", "jongo://openapi"]
    context = rpc_call(mcp, "resources/read", {"uri": "jongo://context"})["result"]["contents"][0]
    assert context["mimeType"] == "text/markdown" and "## The rules of the framework" in context["text"]
    document = rpc_call(mcp, "resources/read", {"uri": "jongo://openapi"})["result"]["contents"][0]
    assert json.loads(document["text"])["openapi"] == "3.1.0"
    assert rpc_call(mcp, "resources/read", {"uri": "jongo://nope"})["error"]["code"] == -32002


# ==================================================================================== HTTP ==


def post(client, message, **headers):
    headers.setdefault("Host", "127.0.0.1:8000")       # how a local MCP client addresses a dev server
    return client.post("/_jongo/mcp", json=message, headers=headers)


CALL = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "add_note", "arguments": {"text": "hi"}}}


def test_the_endpoint_does_not_exist_until_it_is_mounted(app):
    assert post(TestClient(app), CALL).status == 404
    mounted = app.mcp(tools=[app.t.add_note])
    assert isinstance(mounted, McpServer) and app.mcp_server is mounted and app.mcp_path == "/_jongo/mcp"
    reply = post(TestClient(app), CALL)
    assert reply.status == 200 and reply.headers["content-type"] == "application/json"
    assert reply.json() == {"jsonrpc": "2.0", "id": 7, "result": reply.json()["result"]}
    assert reply.json()["result"]["structuredContent"]["text"] == "hi"


def test_http_carries_one_message_per_post_and_no_csrf_token_is_needed(app):
    app.mcp(tools=[app.t.add_note])
    client = TestClient(app)
    local = {"Host": "localhost:8000"}
    raw = client.request("POST", "/_jongo/mcp", data=json.dumps(CALL),
                         headers={"Content-Type": "application/json", "X-CSRF-Token": "", **local})
    assert raw.status == 200, "an MCP client has no CSRF cookie; the endpoint is guarded differently"
    assert post(client, {"jsonrpc": "2.0", "method": "notifications/initialized"}).status == 202
    assert client.get("/_jongo/mcp", headers=local).status == 405
    assert client.get("/_jongo/mcp", headers=local).headers["allow"] == "POST"
    garbage = client.request("POST", "/_jongo/mcp", data="{not json",
                             headers={"Content-Type": "application/json", **local})
    assert garbage.status == 400 and garbage.json()["error"]["code"] == -32700
    batch = post(client, [CALL, CALL])
    assert batch.status == 400 and batch.json()["error"]["data"]["code"] == "J422"
    assert app.t.Note.all().count() == 1, "the refused batch ran nothing"


def test_a_browser_on_another_site_cannot_reach_a_local_server(app):
    app.mcp(tools=[app.t.add_note])
    client = TestClient(app)
    for origin in ("https://evil.example", "null", "http://127.0.0.1:8000.evil.example", "http://127.0.0.1:9"):
        refused = post(client, CALL, Origin=origin)
        assert refused.status == 403 and refused.json()["error"]["code"] == "J420", origin
    assert post(client, CALL, Origin="http://127.0.0.1:8000").status == 200
    assert app.t.Note.all().count() == 1


@pytest.mark.parametrize("host, allowed", [
    ("127.0.0.1:8000", True), ("localhost:8000", True), ("localhost", True), ("[::1]:8000", True),
    ("LOCALHOST:8000", True), ("evil.example:8000", False), ("127.0.0.1.evil.example", False),
    ("192.168.1.5:8000", False), ("testserver", False),
])
def test_dns_rebinding_is_refused_by_checking_the_host_header(app, host, allowed):
    """A page that rebinds its domain to 127.0.0.1 connects from loopback, same-origin, naming its domain."""
    app.mcp(tools=[app.t.add_note])
    reply = post(TestClient(app), CALL, Host=host, Origin=f"http://{host}")
    assert (reply.status == 200) is allowed
    assert app.t.Note.all().count() == (1 if allowed else 0)


def test_a_session_cookie_gives_no_identity_on_the_csrf_exempt_endpoint(app):
    ann = auth.User.create_user("ann", "pw")
    app.mcp(tools=[app.t.whoami])
    client = TestClient(app)
    client.login(ann)
    reply = post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "whoami"}}).json()
    assert reply["result"]["isError"] and reply["result"]["structuredContent"]["error"]["code"] == "J208"


def test_production_needs_the_bearer_token(app, monkeypatch):
    app.mcp(tools=[app.t.add_note])
    app.dev = False
    client = TestClient(app)
    refused = post(client, CALL)
    assert refused.status == 403 and refused.json()["error"]["code"] == "J420"
    assert "JONGO_MCP_TOKEN" in refused.json()["error"]["hint"]

    monkeypatch.setenv("JONGO_MCP_TOKEN", "short")
    assert post(client, CALL, Authorization="Bearer short").status == 403, "a toy token never opens it"

    token = "t" * 32
    monkeypatch.setenv("JONGO_MCP_TOKEN", f" {token}\n")
    assert post(client, CALL, Authorization=f"Bearer {token}").status == 200
    assert post(client, CALL, Authorization=f"Bearer {token}x").status == 403
    assert post(client, CALL, Authorization=token).status == 403
    assert post(client, CALL, Authorization=f"Bearer {token}", Origin="https://agent.example",
                Host="notes.example.com").status == 200, "a caller holding the token may come from anywhere"
    assert app.t.Note.all().count() == 2


def test_dev_mode_answers_this_machine_only(app):
    app.mcp(tools=[app.t.add_note])
    client = TestClient(app)
    environ = client._environ("POST", "/_jongo/mcp", json=CALL, headers={"Host": "127.0.0.1:8000"})
    environ["REMOTE_ADDR"] = "192.168.1.50"
    seen = {}
    b"".join(app(environ, lambda status, headers, exc=None: seen.update(status=status)))
    assert seen["status"].startswith("403"), "jongo dev --host 0.0.0.0 must not hand the LAN a tool that writes"


def test_jongo_env_production_keeps_it_locked_even_with_dev_on(app, monkeypatch):
    app.mcp(tools=[app.t.add_note])
    monkeypatch.setenv("JONGO_ENV", "production")
    assert post(TestClient(app), CALL).status == 403


def test_doctor_reports_the_mounted_endpoint(app, monkeypatch):
    from jongo import doctor

    def mcp_check(**kwargs):
        return [c for c in doctor(app, **kwargs)["checks"] if c["id"] == "mcp_endpoint"]

    assert mcp_check() == []
    app.mcp(tools=[app.t.add_note])
    assert mcp_check()[0]["status"] == "ok" and "this machine only" in mcp_check()[0]["message"]
    warned = mcp_check(production=True)[0]
    assert warned["status"] == "warn" and warned["code"] == "J514" and "1 server function exposed" in warned["message"]
    monkeypatch.setenv("JONGO_MCP_TOKEN", "t" * 32)
    assert mcp_check(production=True)[0]["status"] == "ok"
    from jongo.introspect import describe

    assert describe(app)["app"]["mcp"] == "/_jongo/mcp"


# =================================================================================== stdio ==


def test_serve_stdio_answers_line_by_line(app):
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "",
        "{broken",
        json.dumps([{"jsonrpc": "2.0", "id": 2, "method": "ping"}]),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "add_note", "arguments": {"text": "stdio"}}}),
    ]
    out = io.StringIO()
    serve_stdio(McpServer(app, tools=mine(app)), io.StringIO("\n".join(lines) + "\n"), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, None, None, 3]
    assert replies[1]["error"]["code"] == -32700 and replies[2]["error"]["code"] == -32600
    assert replies[3]["result"]["structuredContent"]["text"] == "stdio"
    assert all("\n" not in line for line in out.getvalue().splitlines())


APP = '''
import sys
from jongo import Jongo, db, server
print("booting the app")                      # must not corrupt the protocol stream
app = Jongo(__name__, title="Stdio notes", database="notes.sqlite3")

class Note(db.Model):
    text = db.Text(max_length=100)

@server
def add_note(text: str) -> dict:
    """Add a note."""
    print("adding", text)                     # nor must this
    return Note.create(text=text).to_dict()

@server(login_required=True)
def mine(request) -> str:
    return request.user.username

@app.page("/")
def home():
    return f"{Note.all().count()} notes"
'''


def cli(tmp_path, *args, stdin=None):
    env = {**os.environ, "PYTHONPATH": str(HERE.parent), "JONGO_TRACEBACK": "0"}
    for name in ("JONGO_DEV", "JONGO_ENV", "JONGO_DATABASE"):
        env.pop(name, None)
    return subprocess.run([sys.executable, "-m", "jongo", *args], cwd=tmp_path, env=env, input=stdin,
                          capture_output=True, text=True, timeout=120)


def talk(tmp_path, messages, *args):
    done = cli(tmp_path, "mcp", *args, stdin="".join(json.dumps(m) + "\n" for m in messages))
    assert done.returncode == 0, done.stderr
    return [json.loads(line) for line in done.stdout.splitlines()], done.stderr


def test_jongo_mcp_is_a_working_stdio_server(tmp_path):
    (tmp_path / "app.py").write_text(APP)
    assert cli(tmp_path, "migrate").returncode == 0
    replies, stderr = talk(tmp_path, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "add_note", "arguments": {"text": "a"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "jongo_get", "arguments": {"path": "/"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "add_note", "arguments": {"text": 1}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "mine"}},
    ])
    assert [r["id"] for r in replies] == [1, 2, 3, 4, 5, 6], "stdout holds protocol messages and nothing else"
    assert "booting the app" in stderr and "adding a" in stderr
    assert replies[0]["result"]["serverInfo"]["title"] == "Stdio notes"
    assert [t["name"] for t in replies[1]["result"]["tools"]] == BUILTIN + ["add_note", "mine"]
    assert replies[2]["result"]["structuredContent"] == {"id": 1, "text": "a"}
    assert "1 notes" in replies[3]["result"]["structuredContent"]["body"]
    error = replies[4]["result"]["structuredContent"]["error"]
    assert replies[4]["result"]["isError"] and error["code"] == "J403" and "hint" in error
    assert replies[5]["result"]["structuredContent"]["error"]["code"] == "J208"


def test_jongo_mcp_flags(tmp_path):
    (tmp_path / "app.py").write_text(APP)
    listed = cli(tmp_path, "mcp", "--list")
    assert listed.returncode == 0, listed.stderr
    data = json.loads(listed.stdout)
    assert data["server"] == "jongo-app" and [t["name"] for t in data["tools"]][-2:] == ["add_note", "mine"]
    read_only = json.loads(cli(tmp_path, "mcp", "--list", "--read-only").stdout)
    assert [t["name"] for t in read_only["tools"]] == BUILTIN

    assert cli(tmp_path, "migrate").returncode == 0
    assert cli(tmp_path, "createadmin", "--username", "ann", "--email", "", "--password", "pw").returncode == 0
    replies, _ = talk(tmp_path, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "mine"}}],
                      "--user", "ann")
    assert replies[0]["result"]["structuredContent"] == {"result": "ann"}
    missing = cli(tmp_path, "mcp", "--user", "ghost", stdin="")
    assert missing.returncode == 1 and "no user called 'ghost'" in missing.stderr and missing.stdout == ""
    help_text = cli(tmp_path, "mcp", "--help").stdout
    assert "claude mcp add" in help_text and "--read-only" in help_text and "--user" in help_text


def test_jongo_sessions_lists_and_revokes(tmp_path):
    (tmp_path / "app.py").write_text(APP.replace('print("booting the app")', ""))
    assert json.loads(cli(tmp_path, "sessions", "--json").stdout) == {"sessions": []}
    assert cli(tmp_path, "migrate").returncode == 0
    for name in ("ann", "bob"):
        assert cli(tmp_path, "createadmin", "--username", name, "--email", "", "--password", "pw").returncode == 0
    script = ("import app; from jongo import auth\n"
              "for n in ('ann', 'ann', 'bob'): auth.session_data_for(auth.User.get(username=n))\n")
    assert subprocess.run([sys.executable, "-c", script], cwd=tmp_path, capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(HERE.parent)}).returncode == 0
    listed = json.loads(cli(tmp_path, "sessions", "--json").stdout)["sessions"]
    assert sorted(s["username"] for s in listed) == ["ann", "ann", "bob"] and "key" not in listed[0]
    assert len(json.loads(cli(tmp_path, "sessions", "--user", "bob", "--json").stdout)["sessions"]) == 1
    table = cli(tmp_path, "sessions").stdout
    assert "LAST SEEN" in table and "bob" in table
    target = [s["id"] for s in listed if s["username"] == "ann"][0]
    one = json.loads(cli(tmp_path, "sessions", "--revoke", str(target), "--json").stdout)
    assert one == {"revoked": 1, "session": target}
    assert "revoked 1 session(s) for ann" in cli(tmp_path, "sessions", "--revoke-user", "ann").stdout
    assert [s["username"] for s in json.loads(cli(tmp_path, "sessions", "--json").stdout)["sessions"]] == ["bob"]
    assert cli(tmp_path, "sessions", "--user", "ghost").returncode == 1
    assert "--revoke-user" in cli(tmp_path, "sessions", "--help").stdout


# ============================================================================== agent docs ==


def test_the_scaffold_and_the_context_pack_tell_an_agent_about_060(app):
    import jongo
    from jongo.introspect import RULES

    scaffold = (Path(jongo.__file__).parent / "scaffold" / "AGENTS.md.tmpl").read_text()
    for needle in ("jongo mcp", "jongo openapi", "jongo sessions --json", "forms.Schema", "return True", "rate_limit="):
        assert needle in scaffold, needle
    for needle in ("forms.Schema", "forms.validate", "J412", "rate_limit=", "revoke_sessions", "J213"):
        assert needle in RULES, needle
    text = jongo.context(app)
    assert "jongo mcp" in text and "jongo openapi" in text
    app.mcp()
    assert "The running server also answers MCP at `/_jongo/mcp`" in jongo.context(app)


def test_every_new_public_name_is_documented():
    """0.6.0's public functions, classes and methods all carry a docstring with an example."""
    import inspect

    from jongo import auth, forms, mcp, ratelimit, schema

    missing, no_example = [], []
    for module in (forms, schema, mcp, ratelimit):
        for name in module.__all__:
            obj = getattr(module, name)
            target = getattr(obj, "fn", obj)                 # a @component wraps its function
            if not (inspect.isfunction(target) or inspect.isclass(target)):
                continue
            doc = inspect.getdoc(target)
            if not doc:
                missing.append(f"{module.__name__}.{name}")
            elif "Example" not in doc and name not in ("TooManyRequests", "RateLimit", "client_key", "Email"):
                no_example.append(f"{module.__name__}.{name}")
            if inspect.isclass(target):
                for method, fn in vars(target).items():
                    if inspect.isfunction(fn) and not method.startswith("_") and not inspect.getdoc(fn):
                        missing.append(f"{module.__name__}.{name}.{method}")
    for name in ("authenticate", "login", "logout", "get_user", "sessions", "revoke_session", "revoke_sessions",
                 "session_data_for", "LoginSession"):
        if not inspect.getdoc(getattr(auth, name)):
            missing.append(f"auth.{name}")
    assert not missing, f"undocumented: {missing}"
    assert not no_example, f"no example in the docstring: {no_example}"
