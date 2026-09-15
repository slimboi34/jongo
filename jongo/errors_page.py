"""HTML for error responses: a friendly page in production, a detailed one in dev."""
from __future__ import annotations

import html
import linecache
import traceback
from pathlib import Path

from .errors import CompileError

_STYLE = """
:root { color-scheme: light dark; --bg:#fbfaf7; --fg:#1d1b20; --muted:#6c6772; --card:#fff; --line:#e7e2dc;
        --accent:#e4572e; --code:#f4f1ec; --hl:#fff1e6; }
@media (prefers-color-scheme: dark) { :root { --bg:#141216; --fg:#eeeaf2; --muted:#a39dab; --card:#1d1a21;
        --line:#2e2a33; --code:#221f27; --hl:#3a2419; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.6 ui-sans-serif,system-ui,-apple-system,sans-serif; }
main { max-width:980px; margin:0 auto; padding:48px 20px 80px; }
.tag { display:inline-block; font:600 12px/1 ui-monospace,Menlo,monospace; letter-spacing:.08em; text-transform:uppercase;
       color:var(--accent); border:1px solid var(--accent); border-radius:999px; padding:6px 10px; }
h1 { font-size:clamp(26px,4vw,38px); line-height:1.15; margin:18px 0 8px; letter-spacing:-.02em; word-break:break-word; }
p.lead { color:var(--muted); margin:0 0 28px; white-space:pre-wrap; }
.card { background:var(--card); border:1px solid var(--line); border-radius:14px; margin:14px 0; overflow:hidden; }
.card h3 { margin:0; padding:12px 16px; font:500 13px/1.4 ui-monospace,Menlo,monospace; border-bottom:1px solid var(--line);
           display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; }
.card h3 span { color:var(--muted); }
.card.lib { opacity:.72; }
pre { margin:0; padding:10px 0; overflow-x:auto; font:13px/1.65 ui-monospace,Menlo,monospace; background:var(--code); }
pre div { padding:0 16px; white-space:pre; }
pre .hl { background:var(--hl); box-shadow: inset 3px 0 0 var(--accent); }
pre .no { display:inline-block; width:3.2em; color:var(--muted); user-select:none; }
.hint { border-left:3px solid var(--accent); background:var(--hl); padding:12px 16px; border-radius:8px; margin:16px 0; }
table { width:100%; border-collapse:collapse; font:13px/1.5 ui-monospace,Menlo,monospace; }
td { padding:6px 16px; border-top:1px solid var(--line); vertical-align:top; word-break:break-all; }
td:first-child { color:var(--muted); width:30%; }
a { color:var(--accent); }
.big { font-size:clamp(64px,14vw,140px); font-weight:800; letter-spacing:-.05em; margin:0; line-height:1; }
"""


def _page(title: str, body: str) -> str:
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>'
        f"<style>{_STYLE}</style></head><body><main>{body}</main></body></html>"
    )


def http_error_page(exc) -> str:
    return _page(
        f"{exc.status} {exc.message}",
        f'<p class="big">{exc.status}</p><h1>{html.escape(exc.message)}</h1>'
        '<p class="lead"><a href="/">Go to the home page</a></p>',
    )


def _source(filename: str, lineno: int, context: int = 4) -> str:
    rows = []
    for n in range(max(1, lineno - context), lineno + context + 1):
        line = linecache.getline(filename, n)
        if not line:
            continue
        cls = ' class="hl"' if n == lineno else ""
        rows.append(f'<div{cls}><span class="no">{n}</span>{html.escape(line.rstrip())}</div>')
    return f"<pre>{''.join(rows)}</pre>" if rows else ""


def _is_app_file(filename: str, root: Path) -> bool:
    try:
        path = Path(filename).resolve()
    except OSError:
        return False
    return path.is_relative_to(root) and ".venv" not in path.parts and "site-packages" not in path.parts


def debug_page(exc: BaseException, request, root: Path) -> str:
    parts = [f'<span class="tag">{html.escape(type(exc).__name__)}</span>']
    if isinstance(exc, CompileError):
        parts.append("<h1>This code can't run in the browser</h1>")
        parts.append(f'<p class="lead">{html.escape(exc.message)}</p>')
        if exc.filename:
            display = exc.filename
            try:
                display = str(Path(exc.filename).resolve().relative_to(root))
            except ValueError:
                pass
            parts.append(
                f'<div class="card"><h3>{html.escape(display)}:{exc.lineno}</h3>{_source(exc.filename, exc.lineno or 1)}</div>'
            )
        if exc.hint:
            parts.append(f'<div class="hint"><strong>Try this:</strong> {html.escape(exc.hint)}</div>')
    else:
        message = str(exc) or type(exc).__name__
        parts.append(f"<h1>{html.escape(message.splitlines()[0])}</h1>")
        rest = "\n".join(message.splitlines()[1:] + list(getattr(exc, "__notes__", [])))
        parts.append(f'<p class="lead">{html.escape(rest) or "Unhandled exception while handling this request."}</p>')

    chain = []
    current = exc
    while current is not None and len(chain) < 5:
        chain.append(current)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    for i, error in enumerate(chain):
        if i:
            parts.append(f"<h2>Caused by {html.escape(type(error).__name__)}: {html.escape(str(error))}</h2>")
        for frame in reversed(traceback.extract_tb(error.__traceback__)):
            mine = _is_app_file(frame.filename, root)
            name = frame.filename
            if mine:
                name = str(Path(frame.filename).resolve().relative_to(root))
            parts.append(
                f'<div class="card{"" if mine else " lib"}"><h3>{html.escape(name)}:{frame.lineno}'
                f"<span>in {html.escape(frame.name)}()</span></h3>"
                f"{_source(frame.filename, frame.lineno, 4 if mine else 1)}</div>"
            )

    if request is not None:
        rows = [("Method", request.method), ("Path", request.full_path)]
        rows += [(k, "…" if k in ("cookie", "authorization") else v) for k, v in request.headers.items()]
        table = "".join(f"<tr><td>{html.escape(str(k))}</td><td>{html.escape(str(v))}</td></tr>" for k, v in rows)
        parts.append(f'<div class="card"><h3>Request</h3><table>{table}</table></div>')
    parts.append('<p class="lead">You see this page because the app runs in dev mode.</p>')
    return _page(f"{type(exc).__name__} · Jongo", "".join(parts))
