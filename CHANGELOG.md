# Changelog

## 0.5.2 (2026-10-15)

0.5.1 was never published to PyPI; 0.5.2 ships its fixes too (see the 0.5.1 section below).

Fixes from a second review of 0.5.1: its own changes read critically, the test suite against a
real PostgreSQL server, the browser runtime in headless Chrome, and the status endpoint under
load and under production settings. No breaking changes.

### Fixed

- **`J114` no longer rejects code that merely sets an `on…` attribute.** 0.5.1 made any
  non-constant `onclick=`/`onX=` on an element a compile error, so `a("x", onclick=f"track({n})")`,
  `button(onclick=script)` or `div(one=x)` — which 0.5.0 compiled, dropping the attribute — stopped
  the app from starting. J114 now fires only when the value is certainly a function (a lambda, a
  `def` in the component, a server function or another module-level callable).
- **`JONGO_ENV=production` now wins over dev mode.** With a stray `JONGO_DEV=1` (or `dev=True`) on
  a deployed server, `jongo doctor` reported "checked as dev", all green, and `/_jongo/status` was
  open to anyone. Now doctor judges it as production and fails `J502`, the server logs a `[J502]`
  warning at start, and `/_jongo/status` still needs an admin or the token. (A hosting platform's
  variables alone still don't override dev mode, so `railway run jongo dev` stays local.)
- **Server calls with a JSON body that isn't an object** (`[]`, `null`, `0`, `false`, `""`) ran the
  function with no arguments; they are `J402` malformed calls. An empty body still means no arguments.
- **Request ids are 8 hex digits instead of 4.** The id is how an error in the browser, the log and
  `jongo status` are matched up, and 4 digits repeated within a few hundred requests.
- **The dev error overlay shows the code and hint** of a server-call error (`[J407] ServerError: …`,
  `hint: …`, `more: jongo explain J407`), like every other surface. Browser errors with a numeric
  `code` of their own (DOMException) are left alone.
- `/_jongo/status/` (trailing slash) answered a refused request with an HTML page instead of the
  JSON error the endpoint gives; it now answers like `/_jongo/status`.
- A `JONGO_STATUS_TOKEN` with surrounding whitespace (read from a file with its newline) never
  matched; it is stripped.

### Tests

- `TestQuerySet.test_ordering` failed on a PostgreSQL database with a non-C collation (the default
  on most installs: `walk` sorts before `Write`); it now accepts either order for those two rows.
  The suite passes on SQLite and on PostgreSQL 16 (en_GB.UTF-8 and C), forwards, reversed and
  shuffled. 371 → 386 tests (`tests/test_fixes_052.py`).

## 0.5.1 (2026-10-15, never published; ships in 0.5.2)

Fixes from a black-box run of 0.5.0 as a new user and a coding agent would use it (from PyPI,
`jongo new`, then breaking the app on purpose). Diagnostics that were wrong, misleading or
crashed are fixed; nothing else changes behaviour. No breaking changes.

### Fixed

- **`import jongo.server` no longer breaks `@server`.** Importing the server module (yourself,
  or by the CLI before your app loaded) rebound `jongo.server` from the decorator to the module,
  so a later `from jongo import server` / `@server` raised "'module' object is not callable".
  The module is now callable as the decorator, so both spellings work in any import order.
- **Database failures in the CLI are coded errors, not tracebacks.** `jongo migrate`,
  `jongo dev`, `jongo run --migrate` and `jongo createadmin` against an unreachable PostgreSQL
  server or an unwritable SQLite path printed a raw traceback; they now print
  `error: [J301] couldn't migrate the database: …` with the hint, and exit 1.
- **`jongo check` no longer creates the SQLite database** (and its directory) as a side effect;
  it plans against no database instead. An unwritable location is reported as `J301`.
- **`jongo doctor`** said a SQLite directory "doesn't exist and can't be created" when it existed
  but wasn't writable; it now says which.
- **Server calls:** a body that isn't JSON was reported as `J200` ("this is the response the app
  chose") instead of `J402`; an unknown keyword argument was reported as a *missing* argument
  instead of naming the typo; a failed CSRF check hinted at adding a hidden form input (it's the
  `X-CSRF-Token` header), and `GET` on the endpoint hinted at registering a method.
- **Production server-call errors keep their real code** (`J302`, `J305`, …) instead of a blanket
  `J407`, so the code in the browser matches the server log and `jongo status`. The message is
  still just "Server error", with no hint, traceback or location.
- **A page's query parameter of the wrong type** (`/list?page=abc` for `page: int`) was `J403`
  "Server function argument has the wrong type" with a hint about converting in the browser.
  It is now **`J211` Query parameter has the wrong type**, and the message shows the value.
- Request bodies that aren't valid JSON, an invalid `Content-Length` and a body over the size
  limit are **`J212` Malformed request body** (they had no code, so they read as `J200`).
- `jongo status --url localhost:8000` (no `http://`) said "unknown url type" and told you to
  start the app; a URL without a scheme now means `http://`.
- `jongo dev` on a port that's in use printed "Fix the error and save to restart" and waited
  for a save that can't help; it now exits at once, and both `dev` and `run` tag the message `[J505]`.
- The dev 404/400 page said "(shown in dev mode only)" in dev mode; it now says what it means.
- The README and guide said `jongo doctor --production` fails on a *missing* secret key; it
  warns when the key only lives in `.jongo/secret` and fails on a weak or unusable one. The docs
  now say so.

### Improved

- **`J114` Event handler passed under the wrong name:** `button("Go", onClick=save)` (or
  `onclick=`) compiled and rendered a button whose handler silently never ran. It is now a
  compile error with "write on_click= instead of onClick=", in `jongo check` and at start-up.
  Props of your own components (`Dialog(onclose=...)`) are unaffected. `jongo context` says so too.
- `jongo run` without `--migrate` logs a `[J302]` warning at start when the schema is behind
  the models, instead of failing on the first request that touches them.
- An app that fails to import because a module is missing gets a hint naming the module, the
  interpreter, and `pip install <module>`.
- `jongo explain 302` works (as `J302`).
- `TestClient.post(path, data=b"...")` sends a raw body, for testing malformed requests.

### Tests

- The suite no longer depends on file order: it passes forwards, reversed and shuffled. (The
  pattern tests now restore every global registry — components, server functions, styles — and
  the admin/agentic tests re-register their own models.) 355 → 371 tests.

## 0.5.0 (2026-10-15)

Diagnostics: every error says which one it is and what to do, one command checks the whole
setup, and a running server can tell you what it has been doing. Also the first PyPI release of
the 0.4.1 security fixes (below), which were tagged in the repository but never published —
**upgrade from 0.4.0 for those alone.** No breaking changes.

### An error catalogue with stable codes

- Every error Jongo raises or reports carries a **stable code** (`J101` an `import` in a
  component, `J302` a schema that needs migrating, `J403` a server-function argument of the wrong
  type, …), a one-line **hint**, and `to_dict()`. 58 codes in six ranges: `J0xx` general, `J1xx`
  components and the compiler, `J2xx` routing and HTTP, `J3xx` the database, `J4xx` server
  functions and channels, `J5xx` configuration and deployment. They live in
  `jongo.errors.CATALOGUE` (title, explanation, fix, example) and in the guide (§22).
- **`jongo explain J302`** prints the entry; `jongo explain --list [--json]` lists them all.
  `jongo.explain(code)` from Python.
- The codes reach every surface: the dev error page (code, hint, request id), server-call error
  responses (`code`, `hint`, `request_id`; `err.code`/`err.hint` on `ServerError` in the browser
  and from `TestClient.rpc`), `jongo check` problems, and every CLI error (`error: [J503] …`,
  `hint: …`). HTTP error pages show the code and hint in dev mode only.
- Driver errors are recognised: "no such table/column" (SQLite) and undefined table/column
  (PostgreSQL) are `J302` with "run `jongo migrate`"; unreachable databases `J301`; integrity
  violations `J305`.
- Errors point at your code: compile errors already had file and line; now route errors (an
  unknown converter names the decorator's line), duplicate and unresolvable server functions,
  async components, a component returning a list, and import failures in the CLI do too.
- Exception classes and hierarchy are unchanged. Codes are attributes. Two new subclasses keep
  the old bases: `RouteError` (a `ValueError`, for bad route paths/converters) and `URLBuildError`
  (a `KeyError`, for `url_for`). The ORM's exceptions (`ValidationError`, `DoesNotExist`,
  `MultipleObjectsReturned`, `MigrationError`, `FieldDoesNotExist`) gain `code`/`to_dict()` but
  keep their bases. `str(CompileError)` now starts with its code: `[J101] …`.

### `jongo doctor`

- One report on whether the app, here, is healthy: Python, Jongo, the app imports, every
  component compiles, every server function's hints resolve, the database is reachable (without
  creating it), pending migrations, the static directory, an admin user when the admin is
  mounted, a free port (`--port`), node (information only). Judged as production
  (`--production`, `JONGO_ENV=production`, or a hosting platform's variables): the secret key
  (missing, file-based or shorter than 32 characters), dev mode, Secure cookies behind a proxy,
  and the status endpoint's token.
- Each check is `{id, status: ok|warn|fail, message, hint, code}`; `--json` for tools; exit 1 on
  any failure. `jongo.doctor(app)` from Python. Never prints a secret or a database password.

### A live system view: `jongo status`

- The app records, in process, the last 200 requests (method, path, status, duration, request
  id, route, error code, and the SQL query count while SQL timing is on), the last 50 unhandled errors (code, message, short
  traceback, app file and line, request id), a counter per route and per server function,
  channel subscribers, and likely N+1 queries (the same statement five or more times in one
  request, while SQL timing is on — the log gets a matching warning). Standard library only;
  one locked append per request (~0.3 µs).
- `jongo.status(app)` returns it; **`/_jongo/status`** serves it as JSON; **`jongo status
  [--url] [--json]`** reads it from a running server.
- Secure by default: open in dev mode only. In production it answers a signed-in admin, or
  `Authorization: Bearer $JONGO_STATUS_TOKEN` (16+ characters, compared in constant time);
  everyone else gets 403 (`J508`). `JONGO_STATUS=off` removes it. Query strings, headers,
  cookies, bodies and SQL parameters are never recorded; credentials in URLs are redacted.
- A crashing server function in production still answers only `"Server error"`, now with
  `code: "J407"` and the `request_id` that finds the full error in `jongo status`; the production
  500 page shows the request id as a reference.

### For coding agents

- `jongo context` and the `AGENTS.md` that `jongo new` writes now say to run `jongo explain`,
  `jongo doctor --json` and `jongo status --json` when something goes wrong.
- `jongo check --json` reports an app that fails to import as a `load` problem with a code, file
  and line, instead of a traceback.

### Measured

- `benchmarks/diagnostics/`: 19 scripted broken apps, scored on whether the failure output
  carries a stable code, a fix hint, the file and line, and JSON. 0.4.0 → 0.5.0: codes 0/19 →
  19/19, hints 3/19 → 19/19, file:line 8/9 → 9/9, JSON 14/19 → 18/19. Method and caveats in
  `benchmarks/diagnostics/RESULTS.md`. No measurable change in requests per second.

### Fixes

- `jongo context` / `describe()` no longer include a PostgreSQL password from the database URL.
- An unsupported database URL in an error message has its password redacted.

## 0.4.1 (2026-10-07, never published to PyPI — ships in 0.5.0)

A security patch release: the findings of a proactive audit, and hardened release workflows.
No breaking changes; upgrading is recommended.

### Fixes

- **Open redirect after admin login.** The `next` check refused `//evil.com` but not
  `/\evil.com` or `/<tab>/evil.com`, which browsers treat as links to another site. A crafted
  login link could send an admin elsewhere right after they signed in. The check now normalises
  the URL the way browsers do; it is available as `jongo.http.is_local_url()` for your own
  login forms.
- **`javascript:` URLs through upper-case attribute names.** The URL-scheme filter matched
  attribute names case-sensitively, so a user-controlled URL under a name like `HREF` (written
  that way, or arriving as a `**props` key from data) skipped it. HTML attribute names are case-insensitive; the server renderer and the
  browser runtime now compare them that way.
- **Page text could derail the boot data block.** Text containing `<!--<script>` put the HTML
  parser in a state where the page's `<script id="jongo-data">` swallowed the rest of the
  document, so the page never hydrated (one comment could break a page for everyone who viewed
  it). Every `<` in that block is now written as `\u003c`.
- **An empty `.jongo/secret` file meant an empty signing key**, and session cookies anyone could
  forge. An empty file is now replaced with a fresh key. The file is also created owner-only
  from the start instead of being chmod-ed after it was written.

### Hardening

- Every response gets `X-Content-Type-Options: nosniff` (unless you set it), and admin pages
  send `X-Frame-Options: DENY` and `Content-Security-Policy: frame-ancestors 'none'` so they
  can't be framed for clickjacking.
- `jongo run`'s built-in server drops a connection whose socket stalls for 30 seconds, so
  clients that open connections and never finish a request can't tie up threads indefinitely.
  Live streams are unaffected (they send a keepalive every 15 seconds).
- Cache-busting SHA-1 hashes are marked `usedforsecurity=False`.

### Project

- GitHub workflows: read-only token by default, `id-token: write` only on the publish job,
  every action pinned to a commit SHA, checkout without persisted credentials, job timeouts and
  concurrency groups. The build job (no OIDC token) is separate from the minimal publish job.
  A new `Tests` workflow runs the suite on pushes and pull requests. `zizmor` reports no
  findings.
- Dependabot keeps the pinned actions current (weekly, grouped, 7-day cooldown).
- `SECURITY.md`: report vulnerabilities through GitHub private vulnerability reporting.

## 0.4.0 (2026-09-29)

Built for the author who is a model.

### The app describes itself

- **`jongo context`** prints a context pack for the coding agent working on the app: a short
  statement of the framework's rules (what a component may do, how a server call works, what
  compiles to the browser and what differs there), then the app's own map — every model with its
  fields, every `@server` function with its signature and where it lives, pages and routes with
  their parameters and guards, components with their props, channels, the admin mount and the
  database — read from the live registries and type hints, so it cannot go stale. `--json` gives
  the map as data; `-o AGENTS.md` writes it.
- **`jongo check`** is the feedback loop an agent can act on: it compiles every component,
  resolves every server function's type hints and plans the migrations without starting a
  server, then reports each problem as data — `kind`, `message`, `file`, `line`, `hint` — and
  exits 1. `jongo build` still writes the bundle.
- **`jongo routes --json`.**
- **`jongo new` writes `AGENTS.md`** (the convention Codex, Cursor and most agents read) and a
  `CLAUDE.md` that imports it with Claude Code's `@AGENTS.md` syntax: the commands, the rules
  that matter most, and the instruction to run `jongo context` before starting and
  `jongo check --json` after every change.
- Python API: `jongo.describe(app)` and `jongo.context(app)`.
- The CLI's `--version` now reads `jongo.__version__` instead of carrying its own copy.

## 0.3.0 (2026-09-29)

Four additions, and the bugs found while making them.

### PostgreSQL

- `Jongo(database="postgres://user@host/db")` (or `JONGO_DATABASE`) runs the same models,
  queries, migrations and tests on PostgreSQL. `pip install "jongo[postgres]"` adds the
  psycopg driver; the core stays dependency-free and SQLite stays the default.
- A new `jongo/db/dialect.py` holds the whole difference: paramstyle, `LIKE`/`GLOB`/`ILIKE`,
  `IS DISTINCT FROM`, `LIMIT ALL`, DDL types, identity columns vs `AUTOINCREMENT`,
  `RETURNING`, and integrity-error classification.
- Migrations use native `ALTER` on PostgreSQL, so a type, nullability or foreign-key change
  no longer rebuilds the table (SQLite still rebuilds, because it must).
- Table creation is ordered by foreign-key dependency — PostgreSQL rejects a forward
  reference that SQLite tolerated.
- **Fixed:** an explicitly inserted id now advances PostgreSQL's identity sequence, which
  would otherwise hand out a colliding id on the next insert.
- **Fixed:** a lookup value the column could never hold (`Todo.get(id="not a number")`)
  matches nothing on both backends instead of erroring on PostgreSQL.
- The suite runs against either backend: `JONGO_TEST_DATABASE=postgres://… pytest`.

### Real-time channels

- `@app.channel("room:<int:id>")` declares a channel and authorises each subscription. A
  channel that is not declared cannot be subscribed to, the same rule that keeps
  undecorated functions off the RPC boundary.
- `broadcast(channel, data)` pushes to every subscribed browser, from anywhere — a request
  handler, a thread, a cron job, a queue worker.
- `live(channel, handler)` is a hook alongside `state()`/`effect()`/`ref()`: it subscribes
  on mount, moves when the channel changes, and unsubscribes on unmount. One SSE connection
  per tab carries every channel the page asked for and reconnects with backoff.
- Data goes through the same `to_json_data` conversion as RPC results, so datetimes and
  Decimals work and the `$v` sentinel stays escaped.
- A subscriber that falls more than 100 messages behind sheds its oldest rather than growing
  the server's memory. The hub is per process; see the guide before running several workers.
- `TestClient.stream()` opens a streaming response without consuming it.

### Compiler

- **Tuples are real tuples.** A tuple compiles to a frozen array with a non-enumerable
  marker: `(1, 2) != [1, 2]`, `repr`/`isinstance`/`type` tell it from a list, `enumerate()`,
  `zip()`, `.items()` and `divmod()` produce tuples, item assignment raises `TypeError`,
  mutating list methods raise `AttributeError`, `+` and `*` keep the type, and concatenating
  a tuple with a list is a `TypeError`.
- **Strings are measured and indexed in code points**, so `len("😀")` is 1 and `s[0]` is a
  whole character. A native regex checks for surrogates first, so only strings that contain
  them pay for the conversion.
- Both are covered by the Python/Node parity suite. Two of the five documented browser
  limitations are therefore gone; int-vs-float and int-keyed dicts remain (see 0.2.2).

### Security

- **`@server` no longer silently drops validation when it cannot resolve a function's
  annotations.** `typing.get_type_hints` raises for an annotation naming a class it cannot
  see — a model defined inside a function, for instance — and that exception was caught and
  turned into "no hints", leaving the function to receive raw browser input: an unvalidated
  string where an `int` was declared, and a bare id where a loaded row was expected, so an
  ownership check such as `note.owner_id != request.user.id` would silently compare against
  an integer. Hints are now resolved against the model registry, and a parameter that still
  cannot be resolved raises rather than running unchecked.

### Performance

- Rendering a page no longer round-trips its serialised tree through `json.loads` before
  building the HTML, and tag-name validation runs once per distinct tag instead of once per
  node: about 10% off a render-heavy page (0.799 → 0.715 ms in-process, best of 7 × 300).
  The round trip through `serialize()` is kept — it is what stops the server's HTML and the
  browser's hydration diverging.
- **The HTML is now rendered from that serialised tree itself, not from a second VNode tree
  rebuilt out of it.** `build()` was a whole extra pass and a whole extra tree on every page
  render, and reading the data directly is the stronger form of the same guarantee: the HTML
  and the browser's hydration data are one object, with nothing in between that could differ.
- **The per-node work that depends only on a name is memoised.** Attribute validation (two
  regexes and two set lookups for every attribute of every node), `prop_name`, and
  `serialize`'s per-prop key handling each cost one dict lookup now, and the error-context
  string `serialize` built for every prop is built only when a prop actually fails. Escaping
  tests for the characters before calling `html.escape`, `_render_children` reports what it
  emitted instead of measuring the output list around every child, and each tag's `"<tag"` /
  `"</tag>"` strings are built once per distinct tag. Every cache is bounded, because prop
  names can arrive from data.
- Together, on a page shaped like a real app — 100 rows, every element carrying classes,
  styles, `data-*` and a link — 1.769 → 0.967 ms in-process (best of 7 × 200): rendering
  2.3× faster, serialising 1.5× faster. End to end under gunicorn, `/rows` went from 633 to
  782 req/s (means of four interleaved runs per arm; `/json`, which renders nothing, did not
  move). Output is byte-identical: HTML and JSON hashes match the previous implementation
  across components, adjacent text nodes, forms, unicode, and the escaping, dangerous-URL
  and `srcdoc` cases.

### Documentation

- `docs/guide.md`: the long-form guide.
- `examples/patterns/`: thirteen complete, runnable apps, each exercised by the test suite.
- `benchmarks/`: Jongo vs Django vs FastAPI, with the harness and the caveats.
- `docs/docs_site.py`: the documentation site, itself a Jongo app, with in-browser search.
- `tests/test_guide_api.py` checks that every API the docs promise exists and behaves as
  written — it found three wrong claims in the guide and the two bugs above.

## 0.2.3

**Relicensed from MIT to AGPL-3.0-or-later.** Jongo is now copyleft: it is free to
use, study and fork, but any modified version — including one offered to users over a
network — must make its complete source available under the same license. This protects
the project from being taken into closed, proprietary products. Copyright © 2026 Joshua
Harty. See `LICENSE`. (Versions 0.1.0–0.2.2 remain available under their original MIT terms.)

No code changes in this release.

## 0.2.2

A second stress-audit pass: 27 fixes (of 32 findings; the other 5 are documented as
inherent JS-value-model limits below). Regression tests added (suite → 167).

### Security

- **vdom:** `iframe(srcdoc=…)` is dropped (escaping still delivered runnable same-origin
  HTML → stored XSS); bare `on*` event-handler attribute *names* (`onerror`, `onfocus`) are
  rejected — only Jongo's `on:` directive form is allowed; `data:text/html` URLs are blocked
  (raster `data:image/*` still allowed); `Decimal('NaN')/('Infinity')` now serialize to `null`
  instead of putting a literal `NaN` on the wire; the `$v` sentinel is escaped for element prop
  names too (completing the 0.2.0 fix).
- **http:** a negative/bogus `Content-Length` is rejected before `read()` (it previously slipped
  past the 16 MB body cap → unauthenticated memory DoS); CR/LF are stripped from response header
  values (no header/response splitting via `redirect()`); `X-Forwarded-Proto` is trusted only
  with the new `Jongo(trust_proxy=True)` opt-in (a client can no longer drop the `Secure` cookie
  flag on HTTPS); exceptions in `after_request`/`_finish` are caught and return a 500 instead of
  escaping to the WSGI server; `set_cookie` validates name/path/samesite with a clean error.

### Correctness

- **compiler:** `bool` is treated as `int` for set/dict membership and lookup (`True in {1,2,3}`,
  `{1:'x'}[True]`, `.get`/`.setdefault`/`.pop`) and for `isinstance(True, int)`; set literals
  dedup bool/int (`len({1, True}) == 1`); bitwise `& | ^ << >>` are correct beyond 32 bits;
  `int()` string parsing rejects trailing garbage (`int("42px")`), honors base prefixes and
  underscores; `float("inf")`/`"nan"`/`"1_000"` parse; `bool * list`; and format-spec edges
  (`f"{255:#06x}"`, `:g`, `:c`).
- **rpc/orm:** naive datetimes now round-trip type-consistently (fixes a 0.2.0 regression where a
  naive value reloaded tz-aware and broke `==`/`<`) while keeping chronological ordering across
  offsets; a bad foreign-key id and `UNIQUE` violations (on `create`, `save` and `update`) raise
  `ValidationError` instead of a raw `sqlite3.IntegrityError`/500; RPC `dict[K, V]` coerces keys as
  well as values; integer RPC params are bounded to 64-bit for JSON numbers, not just strings;
  `Date` normalizes consistently with `DateTime`.

### Known limitations (documented, inherent to the browser's value model)

These are consequences of JavaScript having one number type and string-keyed objects; a clean
fix needs a boxed value model. Browser (post-hydration) code only — the server is always
correct. (Two more limitations listed here in 0.2.2 — tuples comparing equal to lists, and
`len()` counting UTF-16 units — are fixed in 0.3.0.)

- `str()`/f-string of an integral float drops the `.0` (`str(10/2)` → `"5"`); format explicitly,
  e.g. `f"{x:.2f}"`.
- A dict keyed by ints iterates *string* keys in the browser (`list({1:'a'}.keys())` → `["1"]`);
  lookups still work.
- `isinstance` can't distinguish int from float (`isinstance(5, float)` is True); the
  `isinstance(True, int)` case is correct.
- Adding a NOT-NULL column to a populated table keeps a `DEFAULT` in the column DDL (SQLite
  requires it to backfill), so such a table differs cosmetically from a freshly-created one.

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
