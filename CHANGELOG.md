# Changelog

## 0.2.1

Observability: a first-class logging system for humans *and* AI agents. No wiring
in your app required — the dev server, request lifecycle and SQL layer emit
through it automatically.

### Added

- **`jongo.log` — one clear, beautiful log stream.** Zero-dependency (stdlib
  `logging` + `contextvars` + ANSI). A **pretty** formatter (aligned columns:
  time · level · domain · a colour-coded per-request id · message · `key=value`)
  for humans, and a **json** formatter (one object per line) for machine/agent
  consumers. Auto-selects pretty on a TTY, json when piped; override with
  `JONGO_LOG=pretty|json|plain` and `JONGO_LOG_LEVEL`.
- **Request correlation.** Every line emitted while handling a request carries a
  short request id, so one request's whole lifecycle reads together.
- **Per-domain loggers** (`jongo.http/sql/rpc/render/compile/server`).
- **SQL visibility.** Each query logs with timing (DEBUG); the request summary
  rolls up `sql=N·Xms`, so an N+1 shows up at a glance.
- **App-frame-highlighted tracebacks** on unhandled errors, pointing at the
  offending source line.

### Changed

- The dev server, request lifecycle and SQL layer emit through `jongo.log`
  (`server.py`, `app.py`, `db/connection.py`); production (WARNING) pays no
  per-query timing overhead, plus a tidier start-up banner.

## 0.2.0

A correctness-and-security release from a stress audit of 0.1.0. Every fix ships with a
regression test (`tests/test_stress_fixes.py`); the suite grew from 117 to 143.

### Security

- **Rendering: closed a stored-XSS vector.** Ordinary prop/DB data shaped like `{"$v": ...}`
  was revived into a live element (e.g. `<img onerror=...>`). Such keys are now escaped on the
  wire and stay inert data. Attribute *names* are validated (a crafted name could inject
  handlers), `javascript:`/`vbscript:` URLs on url attributes are dropped, invalid tag names are
  rejected, and `css()` class names are validated against selector injection.
- **CSRF: tokens are now signed** with the app secret (a planted/forged double-submit pair is
  rejected), and a request carrying `Origin: null` is treated as untrusted for unsafe methods.
- **Auth: `verify_password` never raises** on a corrupt/tampered stored hash — it fails
  authentication cleanly instead of returning a 500.

### Correctness — compiler (browser code now matches CPython)

- Set `&`, `-`, `^` compile to real set intersection/difference/symmetric-difference (were
  silently `0`/`NaN`). `True == 1`, `1 == 1.0`, `x in [1]` now behave as in Python.
- `pow(a, b, mod)` does modular exponentiation; `round(x, n)` uses banker's rounding matching
  CPython (incl. `round(2.675, 2) == 2.67`); `str.format`/f-strings support `{x[0]}` subscripts,
  positional subscripts `{0[1]}`, and the `#` alternate form (`f"{255:#x}" == "0xff"`).
- Added `str.swapcase/casefold/rsplit/partition/rpartition/removeprefix/removesuffix`, the
  `bin`/`oct`/`hex`/`frozenset` builtins, and fixed `"aaa".count("")`.

### Correctness — server, data, and validation

- **Sessions:** in-place nested mutation (`request.session["x"].append(...)`) is now persisted;
  previously only top-level assignment was saved.
- **Routing:** a literal route (`/o/special`) beats a dynamic one (`/o/<thing>`) regardless of
  registration order (specificity-based matching).
- **`login_required`** builds the `?next=` redirect with proper percent-encoding.
- **RPC:** `Literal[...]` matches by value *and* type (JSON `false`/`1.0` no longer satisfy
  `Literal[0,1,2,3]`); over-long int strings and non-finite floats return a clean 400; async
  `@server` functions work when a host event loop is already running; two `@server` functions
  that would collide on one RPC id now fail loudly at import instead of silently shadowing.
- **ORM:** `DateTime` is normalised to UTC before storage, so ordering and range filters compare
  chronologically across timezones; `NaN`/`inf` floats raise instead of being silently stored as
  `NULL`; a non-callable mutable field default (`JSON(default=[])`) is copied per instance instead
  of shared across rows; a very large `field__in=[...]` no longer exceeds SQLite's variable limit;
  `filter(int_field=5.9)` no longer matches rows where the value is `5`.
- Serialization now converts non-finite floats to `null` so a page always hydrates and RPC
  responses always parse.

### Known limitations (documented, not bugs)

- Integers beyond 2⁵³ lose precision in browser code (JavaScript numbers are float64).
- `@server`/component code that closes over a loop variable captures each item (a deliberate
  divergence from Python's late-binding closures).
- `x__ne=v` / `.exclude(field=v)` include rows where the column is `NULL`, matching Python's
  `None != v` rather than SQL's three-valued logic.
- Browser `sum()` of floats uses a plain left fold, so it can differ in the last bit from
  CPython 3.12's compensated summation.

## 0.1.0

Initial release.
