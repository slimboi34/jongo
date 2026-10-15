"""Score how well Jongo reports realistic mistakes.

    python benchmarks/diagnostics/run.py --python /path/to/venv/bin/python --label 0.5.0
    python benchmarks/diagnostics/run.py --table results-0.4.0.json results-0.5.0.json

For every scenario in scenarios.py the app is written to a fresh directory and probed the
same way for every version — the commands a developer or a coding agent would reach for:

1. ``jongo check --json``  2. ``jongo check``  3. ``jongo doctor --json``
4. the app run as ``jongo dev`` would (logs on stderr, piped), with the scenario's requests,
   and ``jongo.status(app)`` where it exists.

A probe that the version doesn't have (``doctor`` on 0.4.0) just produces an argparse error.
All the output is then scored on four questions, each yes/no, looking only at output that
is about *this* failure (it mentions one of the scenario's needles):

* **code** — a stable error code (``J`` + 3 digits) is attached to the failure: in the same
  JSON object, or within a few lines of it in text.
* **hint** — an explicit fix hint is attached the same way (a ``hint`` field, ``hint:`` or
  ``Try this:``). Advice written into the error message itself does not count.
* **location** — where the scenario's mistake has a line, ``app.py:LINE`` (or a Python
  traceback's ``"app.py", line LINE``, or JSON ``file``/``line``) appears anywhere.
* **json** — some output parses as JSON (whole, or one JSON log line) and contains the failure.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from scenarios import SCENARIOS  # noqa: E402

CODE = re.compile(r"\bJ\d{3}\b")
WINDOW = 8


def run_scenario(python: str, scenario: dict) -> list[dict]:
    work = Path(tempfile.mkdtemp(prefix="jongo-diag-"))
    for name, text in scenario["files"].items():
        (work / name).write_text(text)
    env = {**os.environ, "JONGO_DEV": "1", "NO_COLOR": "1"}
    env.pop("JONGO_LOG", None)
    env.pop("JONGO_APP", None)
    env.update(scenario.get("env", {}))
    if scenario.get("setup"):
        subprocess.run([python, "-c", scenario["setup"]], cwd=work, env=env, capture_output=True, timeout=60)
    entries = []
    for args in (["check", "--json"], ["check"], ["doctor", "--json"]):
        proc = subprocess.run([python, "-m", "jongo", *args], cwd=work, env=env, capture_output=True,
                              text=True, timeout=120)
        entries.append({"probe": "jongo " + " ".join(args), "text": proc.stdout})
        entries.append({"probe": "jongo " + " ".join(args) + " (stderr)", "text": proc.stderr})
    spec = work / "_spec.json"
    spec.write_text(json.dumps({"requests": scenario["requests"], "migrate": scenario.get("migrate", True)}))
    proc = subprocess.run([python, str(HERE / "runtime_probe.py"), str(spec)], cwd=work, env=env,
                          capture_output=True, text=True, timeout=120)
    out, _, tail = proc.stdout.partition("@@ENTRIES@@")
    try:
        entries += json.loads(tail)
    except ValueError:
        pass
    entries.append({"probe": "runtime (stdout)", "text": out})
    entries.append({"probe": "runtime (stderr)", "text": proc.stderr})
    return entries


# -------------------------------------------------------------------------------- scoring --

def _json_docs(text: str) -> list:
    docs = []
    try:
        docs.append(json.loads(text))
        return docs
    except ValueError:
        pass
    for line in text.splitlines():  # JSON log lines
        line = line.strip()
        if line.startswith("{"):
            try:
                docs.append(json.loads(line))
            except ValueError:
                pass
    return docs


def _objects(doc):
    """Every dict in a JSON document, innermost included."""
    if isinstance(doc, dict):
        yield doc
        for v in doc.values():
            yield from _objects(v)
    elif isinstance(doc, list):
        for v in doc:
            yield from _objects(v)


def _strings(obj) -> str:
    return " ".join(str(v) for v in obj.values() if isinstance(v, (str, int)))


def _text_lines(text: str) -> list[str]:
    if "<html" in text[:300].lower() or "<!doctype" in text[:300].lower():
        text = re.sub(r"<style.*?</style>", "", text, flags=re.S)
        text = html.unescape(re.sub(r"<[^>]+>", "\n", text))
    return [line for line in (l.strip() for l in text.splitlines()) if line]


def score(scenario: dict, entries: list[dict]) -> dict:
    needles = [n.lower() for n in scenario["needles"]]
    mentions = lambda s: any(n in s.lower() for n in needles)  # noqa: E731
    result = {"code": False, "hint": False, "location": None, "json": False, "codes_seen": []}

    lines = scenario["files"]["app.py"].splitlines()
    marked = [i + 1 for i, l in enumerate(lines) if "# <-- HERE" in l]
    want_lines = set()
    if scenario.get("location") and marked:
        want_lines = {marked[0] + d for d in [0, *scenario.get("alt_lines", [])]}
        result["location"] = False

    for entry in entries:
        text = entry.get("text") or ""
        for doc in _json_docs(text):
            for obj in _objects(doc):
                body = _strings(obj)
                if mentions(body):
                    result["json"] = True
                    if isinstance(obj.get("code"), str) and CODE.fullmatch(obj["code"]):
                        result["code"] = True
                        result["codes_seen"].append(obj["code"])
                    if isinstance(obj.get("hint"), str) and obj["hint"].strip():
                        result["hint"] = True
                if want_lines and str(obj.get("file", "")).endswith("app.py") and obj.get("line") in want_lines:
                    result["location"] = True
        tl = _text_lines(text)
        for i, line in enumerate(tl):
            if not mentions(line):
                continue
            near = tl[max(0, i - WINDOW): i + WINDOW + 1]
            for n in near:
                found = CODE.findall(n)
                if found:
                    result["code"] = True
                    result["codes_seen"] += found
                if re.match(r"^(hint:|try this:)", n.lower()):
                    rest = n.split(":", 1)[1].strip()
                    if rest or near.index(n) + 1 < len(near):
                        result["hint"] = True
        if want_lines:
            for line_no in want_lines:
                if f"app.py:{line_no}" in text or re.search(rf'app\.py", line {line_no}\b', text):
                    result["location"] = True
    result["codes_seen"] = sorted(set(result["codes_seen"]))
    result["expected_code_seen"] = scenario["expect"] in result["codes_seen"]
    return result


def run(python: str, label: str) -> dict:
    version = subprocess.run([python, "-c", "import jongo; print(jongo.__version__)"],
                             capture_output=True, text=True, cwd=tempfile.gettempdir()).stdout.strip()
    rows = []
    for scenario in SCENARIOS:
        entries = run_scenario(python, scenario)
        rows.append({"name": scenario["name"], "expect": scenario["expect"], **score(scenario, entries)})
        print(f"  {scenario['name']:<52} " + " ".join(
            f"{k}={'-' if rows[-1][k] is None else ('Y' if rows[-1][k] else 'n')}"
            for k in ("code", "hint", "location", "json")), file=sys.stderr)
    return {"label": label, "jongo": version, "scenarios": rows}


def totals(result: dict) -> dict:
    out = {}
    for key in ("code", "hint", "location", "json"):
        applicable = [r for r in result["scenarios"] if r[key] is not None]
        out[key] = (sum(1 for r in applicable if r[key]), len(applicable))
    return out


def table(before: dict, after: dict) -> str:
    mark = lambda v: "–" if v is None else ("✓" if v else "✗")  # noqa: E731
    lines = [
        f"| # | Scenario | code {before['jongo']} → {after['jongo']} | hint | file:line | JSON |",
        "|---|---|---|---|---|---|",
    ]
    for i, (b, a) in enumerate(zip(before["scenarios"], after["scenarios"]), 1):
        cells = [f"{mark(b[k])} → {mark(a[k])}" for k in ("code", "hint", "location", "json")]
        code = f" ({a['expect']})" if a["expected_code_seen"] else ""
        lines.append(f"| {i} | {a['name']}{code} | " + " | ".join(cells) + " |")
    tb, ta = totals(before), totals(after)
    cells = [f"**{tb[k][0]}/{tb[k][1]} → {ta[k][0]}/{ta[k][1]}**" for k in ("code", "hint", "location", "json")]
    lines.append("| | **Total** | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", help="the interpreter whose jongo to test")
    parser.add_argument("--label", default="run")
    parser.add_argument("--out", help="write results JSON here")
    parser.add_argument("--table", nargs=2, metavar=("BEFORE", "AFTER"), help="print the before/after table")
    args = parser.parse_args()
    if args.table:
        before, after = (json.loads(Path(p).read_text()) for p in args.table)
        print(table(before, after))
        return
    result = run(args.python or sys.executable, args.label)
    text = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n")
    t = totals(result)
    print(f"jongo {result['jongo']}: " + ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in t.items()), file=sys.stderr)


if __name__ == "__main__":
    main()
