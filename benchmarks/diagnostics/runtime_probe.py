"""Run one scenario's app the way `jongo dev` would, send its requests, print what came back.

Works against any Jongo since 0.4.0 (it only uses APIs both versions have, and
``jongo.status`` when it exists). Prints one JSON list of {probe, text} on stdout; the
app's own log output goes to stderr, as it would in a terminal.
"""
import io
import json
import os
import sys
import traceback

spec = json.loads(open(sys.argv[1]).read())
sys.path.insert(0, os.getcwd())
entries = []


def emit():
    print("\n@@ENTRIES@@" + json.dumps(entries))


try:
    import jongo
    from jongo.cli import load_app
    from jongo.errors import CompileError

    app = load_app("app.py")  # before importing jongo.server, which shadows the `jongo.server` decorator
    from jongo.server import auto_migrate, setup_logging
except SystemExit:
    emit()
    raise SystemExit(1)
except Exception:
    traceback.print_exc()
    emit()
    raise SystemExit(1)

setup_logging(app.dev)
if app.dev and spec.get("migrate", True):
    try:
        auto_migrate()
    except Exception:
        traceback.print_exc()
try:
    app.check()
except CompileError as exc:
    if not app.dev:
        traceback.print_exc()
    else:
        import logging

        logging.getLogger("jongo").error("compile error\n%s", exc)

client = app.test_client()
for kind, target, payload in spec["requests"]:
    try:
        if kind == "GET":
            r = client.get(target)
        elif kind == "RPC":
            r = client.post("/_jongo/rpc/" + target, json=payload)
        elif kind == "RAWPOST":  # a plain HTML form post: no cookies, no CSRF token
            body = payload.encode()
            environ = {"REQUEST_METHOD": "POST", "PATH_INFO": target, "QUERY_STRING": "", "SERVER_NAME": "x",
                       "SERVER_PORT": "80", "wsgi.url_scheme": "http", "wsgi.input": io.BytesIO(body),
                       "CONTENT_LENGTH": str(len(body)), "CONTENT_TYPE": "application/x-www-form-urlencoded",
                       "HTTP_HOST": "x"}
            out = {}
            chunks = app(environ, lambda status, headers, exc_info=None: out.update(status=status))
            entries.append({"probe": f"{kind} {target}", "status": out.get("status"),
                            "text": b"".join(chunks).decode("utf-8", "replace")})
            continue
        entries.append({"probe": f"{kind} {target}", "status": r.status, "text": r.text})
    except Exception:
        entries.append({"probe": f"{kind} {target}", "status": None, "text": traceback.format_exc()})

if hasattr(jongo, "status"):  # 0.5.0+: the live system view, as `jongo status --json` would show it
    entries.append({"probe": "status", "text": json.dumps(jongo.status(app))})
emit()
