# Diagnostics benchmark: 0.4.0 → 0.5.0

How well does Jongo tell you — or the coding agent working for you — what went wrong and
what to do about it? 19 realistic broken apps, one mistake each, probed the same way for both
versions. Measured 2026-10-15 on one Mac (Python 3.12): 0.4.0 from PyPI in a throwaway venv,
0.5.0 from this tree.

```bash
python benchmarks/diagnostics/run.py --python /path/to/venv/bin/python --label X --out results-X.json
python benchmarks/diagnostics/run.py --table results-0.4.0.json results-0.5.0.json
```

## Results

| # | Scenario | code 0.4.0 → 0.5.0 | hint | file:line | JSON |
|---|---|---|---|---|---|
| 1 | import inside a component (J101) | ✗ → ✓ | ✓ → ✓ | ✓ → ✓ | ✓ → ✓ |
| 2 | database model used in a component (J102) | ✗ → ✓ | ✓ → ✓ | ✓ → ✓ | ✓ → ✓ |
| 3 | undefined name in a component (J103) | ✗ → ✓ | ✗ → ✓ | ✓ → ✓ | ✓ → ✓ |
| 4 | hook called in a page function (J107) | ✗ → ✓ | ✗ → ✓ | ✓ → ✓ | ✓ → ✓ |
| 5 | component returns a list (J110) | ✗ → ✓ | ✗ → ✓ | ✗ → ✓ | ✓ → ✓ |
| 6 | model changed, migration not run (J302) | ✗ → ✓ | ✗ → ✓ | – → – | ✓ → ✓ |
| 7 | server function called with the wrong type (J403) | ✗ → ✓ | ✗ → ✓ | – → – | ✓ → ✓ |
| 8 | server function called with too many arguments (J404) | ✗ → ✓ | ✗ → ✓ | – → – | ✓ → ✓ |
| 9 | call to a server function that does not exist (J401) | ✗ → ✓ | ✗ → ✓ | – → – | ✓ → ✓ |
| 10 | unknown route converter (J201) | ✗ → ✓ | ✗ → ✓ | ✓ → ✓ | ✗ → ✓ |
| 11 | SQLite volume not mounted (unwritable path) (J301) | ✗ → ✓ | ✗ → ✓ | – → – | ✓ → ✓ |
| 12 | PostgreSQL server not running (J301) | ✗ → ✓ | ✗ → ✓ | – → – | ✓ → ✓ |
| 13 | production deploy with a weak secret key (J501) | ✗ → ✓ | ✗ → ✓ | – → – | ✗ → ✓ |
| 14 | server function hint names an undefined type (J405) | ✗ → ✓ | ✓ → ✓ | ✓ → ✓ | ✓ → ✓ |
| 15 | two server functions built by one factory (J406) | ✗ → ✓ | ✗ → ✓ | ✓ → ✓ | ✗ → ✓ |
| 16 | HTML form posted without the CSRF token (J207) | ✗ → ✓ | ✗ → ✓ | – → – | ✗ → ✗ |
| 17 | module without a Jongo app (J504) | ✗ → ✓ | ✗ → ✓ | – → – | ✗ → ✓ |
| 18 | duplicate value for a unique field (J304) | ✗ → ✓ | ✗ → ✓ | – → – | ✓ → ✓ |
| 19 | url_for with a route name that does not exist (J203) | ✗ → ✓ | ✗ → ✓ | ✓ → ✓ | ✓ → ✓ |
| | **Total** | **0/19 → 19/19** | **3/19 → 19/19** | **8/9 → 9/9** | **14/19 → 18/19** |

✓ yes · ✗ no · – not applicable (the mistake has no line in the app's code). The code in
brackets is the one 0.5.0 attached; every scenario got the code it was designed to get.

| | 0.4.0 | 0.5.0 |
|---|---|---|
| failure carries a stable error code | 0 / 19 | 19 / 19 |
| failure carries an explicit fix hint | 3 / 19 | 19 / 19 |
| file:line of the mistake, where it has one | 8 / 9 | 9 / 9 |
| failure available as machine-readable JSON | 14 / 19 | 18 / 19 |

## Method

Each scenario (in [scenarios.py](scenarios.py)) is an `app.py` with one mistake: compile
errors (an `import` in a component, a model used in a component, a typo'd name), runtime
component errors (a hook in a page function, a component returning a list), a model changed
without migrating, server functions called with the wrong type / too many arguments / a name
that doesn't exist, an unknown route converter, an unwritable SQLite path, a PostgreSQL server
that isn't running, a production deploy with a weak secret key, an unresolvable server-function
hint, two server functions colliding on one id, a form posted without its CSRF token, a module
without an app, a duplicate value for a unique field, and `url_for` with a wrong name.

Every scenario is probed identically for both versions, with the commands a developer or an
agent would reach for: `jongo check --json`, `jongo check`, `jongo doctor --json` (0.4.0
answers with an argparse error), then the app run the way `jongo dev` runs it — logs to a
pipe, auto-migrate (except the "not migrated" scenario, which is a `jongo run`/gunicorn
deploy), compile at start-up — sending the scenario's requests and, where the version has it,
reading `jongo.status(app)` (what `jongo status --json` returns from a running server).

All output — stdout, stderr, response bodies — is then scored with four yes/no questions,
looking only at output about *this* failure (text that contains one of the scenario's
"needles", substrings of the failure's own message that both versions share):

- **code**: a `J` + three-digit code in the same JSON object as the failure, or within 8 lines
  of it in text.
- **hint**: an explicit hint attached the same way — a non-empty `hint` field, a `hint:`
  line, or the error page's "Try this:". Advice inside the message itself does not count.
- **file:line**: the marked line of `app.py` appears anywhere — `app.py:LINE`, a Python
  traceback's `"app.py", line LINE`, or a JSON `file`/`line` pair. Where a decorator and
  the `def` under it are equally right, both are accepted.
- **JSON**: some output parses as JSON (a whole output, or one JSON log line) and contains the
  failure. 0.4.0 already scores well here, mostly through its JSON log lines and server-call
  error bodies.

## Read these numbers honestly

- **The scenarios and the scorer were written by the same author as the 0.5.0 changes.** The
  needles use wording both versions share and the scoring rules are the same for both, but the
  set of mistakes is mine; a different set would give different numbers. The 0.5.0 column is
  "every failure in this set", not "every failure".
- The 0.4.0 location score is high because Python tracebacks already name the line; 0.5.0's
  gain there is one scenario (a component returning a list, where 0.4.0's traceback has no app
  frame).
- The one remaining JSON miss: a plain HTML form posted without its CSRF token gets an HTML 403
  page (with the code and hint in dev). The status view records the request with its code
  (`J207`) but not the message, so the scorer, correctly, doesn't count it.
- Hints are only as good as their text; the scorer checks one is there, not that it is right.
  Each hint was read by hand for these 19 cases.

## Cost

The recorder behind `jongo status` appends one tuple to a ring buffer under a lock per
request: 266 ns per `record_request` call plus ~110 ns reading the request's SQL counters
(timeit, 200k calls). In-process requests per second on `examples/todo` (no sockets, 2000
requests × 7 runs, median, three interleaved before/after runs): `GET /` 1687–1836 before vs
1761–1828 after; `GET /todos/1` 3363–3584 before vs 3171–3600 after. The difference is inside
the run-to-run noise; this is a measurement of "no visible cost", not a speed claim.
