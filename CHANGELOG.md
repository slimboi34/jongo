# Changelog

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
