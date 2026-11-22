# Changelog

## 0.7.1 (2026-11-22)

**A machine-driven test of 0.7.0.** 0.7.0's fixes came from probing by hand; this release
comes from machines: differential fuzzing of the compiler on about 125,000 programs (CPython
against the compiled code under Node), forms parity over about 145,000 inputs, an ORM oracle
over about 200,000 runs, about 187,000 raw HTTP requests against a production and a dev
server, and mutation testing of 422 mutants of the lines 0.7.0 changed. It confirmed 12
defects (1 high, 6 medium and 5 low severity): 11 are fixed and one is documented. Re-running
the compiler and forms campaigns against the fixes found more of the same kind, fixed here
too, and a review of the new header parser found a memory leak beside it. The HTTP servers
held up: no 500s and no tracebacks across all the raw requests. Still zero dependencies.
Tests: 977 → 1073 (`tests/test_fixes_071.py`, `test_properties_071.py` and
`test_mutation_killers_071.py`), passing forwards and in reversed file order on Python 3.12.

Read before upgrading: browser code that relied on JavaScript's coercion (`"n=" + 1`, `-"a"`,
`[1, 2][1.5:]`) now raises `TypeError`, as the server already did; a str, list or dict method
the browser lacks, called on a literal, is a compile error (`J103`); `portable_pattern()`
refuses two more constructs (`J115`); and a multipart part with more than 64 header lines, or
a header with more than 64 parameters, is a 400.

### Security

- **One multipart form could cost about 30 CPU-minutes, before authentication.** Each part's
  headers were read with the email package, whose parameter splitter re-scans a header for
  every `;`: one 14 KB `Content-Disposition` of `;"` pairs took about 2 s, and a 16 MB
  request holds 1000 parts. Form routes parse the body inside the CSRF check, so a cookie
  from any GET was enough. Part headers are now read by a small parser that is linear in their
  size (one regex scan per header value); that request is refused in about 10 ms, and a part
  with more than 64 header lines, or a header with more than 64 parameters, is a 400
  (`J212`). Quoted strings, RFC 2231/5987 `filename*` values (with continuations) and RFC 2047
  encoded words in a quoted name or filename are decoded as before. When a part sends both
  `filename` and `filename*`, the extended one now wins, as RFC 6266 says. The request's own
  `boundary=` goes through the same parser.
- **Charset names in a form grew the server's memory without bound.** `codecs.lookup()`
  caches every name it fails to find, forever (`encodings._cache`), and a part's `charset=`
  was looked up as sent: five requests naming a fresh 8 KB charset per part kept 40 MB. Only
  names the standard library knows are looked up now; any other is read as UTF-8, as an
  unusable charset already was.

### Fixed

- **ORM: `filter(col__in=[x])` missed rows that `filter(col=x)` matched** for some floats
  (`1e-300`, `-2.9e-258`). The float was inlined as `repr(x)`, which SQLite reads back as a
  neighbouring double. Floats are now bound parameters. Ints and strings are still inlined,
  so a long list can't pass SQLite's bound-variable limit, and a list that mixes the two binds
  only the values that need it.
- **Forms: `portable_pattern()` passed patterns the browser can't run.** A lone `]` outside a
  character class (`[A-Z]+]`) is a literal in Python and a syntax error in a u-mode RegExp, so
  every browser-side `check()` on that field threw; it is now escaped to `\]`. A repeated
  lookahead or lookbehind (`(?!x){2}`) and `\B` (which matches an empty value in JavaScript
  only) are refused with `J115`.
- **Forms: `Choice([2])` accepted `2.0` in the browser and refused it on the server**, because
  `str(2.0)` differs between them. An integral float is now compared, and cleaned, as its int
  on both sides.
- **Browser code raised other errors than CPython, or none**, so an `except` clause behaved
  differently. Each now raises what CPython raises:
  - `ord("ab")` returned the first code point: `TypeError`.
  - `abs("a")` and `abs([1, 2])` returned `NaN`: `TypeError`.
  - `8 >> -1` and `8 << -1` shifted the other way: `ValueError`.
  - `round(x, ndigits=n)` raised `TypeError`; the keyword works.
  - `round(float("inf"))` and `round(float("nan"))` returned inf and nan: `OverflowError` and
    `ValueError`.
  - `chr(-1)` raised what `except IndexError` catches: `ValueError` (`OverflowError` past a C
    int).
  - `"a\tb".expandtabs()` compiled, then raised `AttributeError` when it ran; it is
    implemented. A method the runtime lacks, called on a literal (`"x".encode()`), is now a
    compile error (`J103`) at start-up.
- **Browser operators coerced the wrong types instead of raising `TypeError`** (found by the
  re-run): `"a" + 1`, `[1] + 0` (`"1,20"`), `"a" - 1` (`NaN`), `"a" * "a"`, `-"a"`, `"a" ** 2`,
  a slice bound that isn't an int, `**` of a list, `5["valueOf"]`, and item assignment to a
  number or a set. A bare JavaScript `+` was also emitted when only one side was a string
  literal, so `"n=" + 1` concatenated. The number and string fast paths are unchanged.

### Known limits

- A dict changed inside a loop over it doesn't raise `RuntimeError` in the browser: the loop
  runs over the keys it began with (guide §18). A guard made every dict loop 1.7 to 4.5 times
  slower and would still miss `.items()`, `.keys()` and `.values()`, which return lists.
- The compiler differential still finds browser code that fails differently from CPython when
  it is wrong twice over or passes a builtin the wrong type of argument: `x[i] = y` evaluates
  `x` and `i` before `y`, and a few methods and `math` functions accept types CPython refuses
  (`"ab".removesuffix(1.5)`, `math.floor([1])`). Code that runs in CPython is not affected.
- An `__in` list that would bind more than `db.query.MAX_BOUND_IN` (10,000) values inlines its
  floats as text, as 0.7.0 did, so the extreme magnitudes above can still miss in such a list.
- PostgreSQL was not available: the `__in` change is verified on SQLite only, and the 13
  PostgreSQL tests were skipped. The browser fixes were tested under Node, not in a real
  browser. The 0.7.0 limits still apply.

## 0.7.0 (2026-11-21)

**Hardening.** This release is the result of a stress test of 0.6.0. Fable planned seven
missions (HTTP and auth security, agent surfaces and denial of service, compiler and browser
parity, the ORM and migrations under upgrade and concurrency, the CLI and patterns, and two
runs by a smaller model working only from the docs), agents ran them in parallel, and five
more agents fixed what they confirmed, one batch each. It fixes the 83 defects the test
confirmed (75 new and 8 already known; 6 high, 28 medium and 49 low severity), plus one
finding that came back without a verdict (`str.rsplit()` in the browser). The defences 0.6.0
was designed with held: session signing, CSRF tokens, admin authorisation, SQL injection,
path traversal, SSR escaping and MCP authentication. The six high-severity fixes are an
unauthenticated form-parsing denial of service on every app, XSS through `redirect()` from a
`@server` function, a quadratic regex in the error recorder, a subscriber leak on
`HEAD /_jongo/live`, a login-throttle reset through a look-alike account, and a crash on any
`data:` URL. Still zero dependencies.

Read "Changed" before upgrading: there are new refusals, a new default header, and browser
code that raises where it used to return a wrong value.

### Security

- **An anonymous request could make any app parse 16 MB of form before the CSRF check.**
  `POST /_jongo/rpc/<anything>` parsed a multipart or urlencoded body to look for
  `csrf_token` when no `X-CSRF-Token` header was sent: about 1 CPU-second per MB of tiny
  parts, and 650 MB of memory for 14 MB of empty fields. Server calls now take the token from
  the header only, the cookie's signature is checked before any body is read, and forms are
  capped before parsing: `Request.max_form_fields` and `Request.max_form_parts` (1000 each)
  refuse larger ones with a 413 (`J212`). Multipart bodies are split with plain byte
  operations instead of the email parser, so 8 MB of newlines in one field takes 2 ms instead
  of 1.5 s, and a part naming a charset with a quadratic decoder (`punycode`, `idna`) is read
  as UTF-8.
- **A `@server` function could send the browser to a `javascript:` URL.** A server-call
  redirect must now be relative or `http(s)`; anything else is a 500 (`J408`) naming the
  function and never reaches the browser. In the browser, `navigate()` (which also follows
  server-call redirects) accepts only `http:` and `https:` URLs and reports anything else
  (the overlay in dev, the console in production). `redirect()`'s docstring says to pass a
  target taken from the request through `is_local_url()`, which is now exported as
  `jongo.is_local_url`.
- **One request could cost seconds of CPU in the error recorder.** `monitor.redact()` ran its
  credential pattern over the whole error message before cutting it to 300 characters, which
  is quadratic on a long message of many `://` and no `@` (4 s for a 97 KB request). The
  message is cut first, and a password the cut runs through is masked to the end.
- **`HEAD /_jongo/live` (and the MCP `jongo_get` tool) left a subscriber registered forever**,
  each holding a 100-message queue that every `broadcast()` filled. A stream now subscribes
  only once the server starts sending it, and `HEAD` gets the headers alone. A process holds
  at most `live.MAX_SUBSCRIBERS` (10,000) live connections; past that a new one gets a 503.
- **A look-alike account could reset someone else's login throttle.** Wrong passwords were
  counted under the lower-cased username while the account lookup is exact, so a correct
  password for `Ann` cleared the count for `ann`, and an attacker who signed up as `Ann` could
  keep guessing until the per-address limit (50) stopped them. The count is now kept per exact
  username, and a correct password clears only its own. Pattern 16's "username taken" check
  is now case-insensitive as well.
- **A revoked sign-in kept receiving on an open live stream.** A stream opened by a signed-in
  user re-checks the sign-in before delivering a message and on keepalives (at most once per
  `live.SESSION_CHECK_SECONDS`, 5 s) and ends once it is revoked, expired or the password
  changed. `auth.logout()`, `auth.revoke_session()` and `auth.revoke_sessions()` end the
  affected streams at once; `live.hub.drop_session()` and `live.hub.drop_user()` do the same
  for code that removes sign-ins some other way.
- **`jongo dev` answered any `Host`, so a web page could reach it by DNS rebinding** (the
  debug page with source, `/_jongo/status`, the OpenAPI document, `@server` calls). Under
  `jongo dev` the app answers `localhost`, `*.localhost` and IP addresses only; any other name
  is a 400 (`J214`) unless listed in the new `Jongo(allowed_hosts=[...])` (`".example.com"`
  or `"*.example.com"` also allows subdomains). In production the check applies when
  `allowed_hosts` is set.
- **The CSRF `Origin` check ignored the scheme**, so an `http://` page (which an on-path
  attacker can inject into) could post to the `https` site. Scheme and host must both match;
  an `https` `Origin` on an app that sees `http` (TLS ended at a proxy without
  `trust_proxy=True`) is still accepted. `login()` and `logout()` now replace the CSRF token
  (new `request.rotate_csrf()`), so a token minted or planted before sign-in stops working.
- **Pages could be framed by any site** (clickjacking). Every response carries
  `X-Frame-Options: SAMEORIGIN` and, unless it sets its own `Content-Security-Policy`,
  `frame-ancestors 'self'`. `Jongo(frame_options="DENY")` forbids framing and `None` turns it
  off. The admin keeps its `DENY`.
- **A sign-up loop never hit a `rate_limit=`.** A signed-in caller was counted by user alone,
  so each new account started a fresh window. Every request now counts against the client
  address, and against the user as well when someone is signed in; it is refused if either is
  over, and then neither is charged. `ratelimit.ADDRESS_FACTOR` (default `1`) loosens the
  address side for many users behind one address; `None` restores the 0.6 behaviour.
- **Flooding the rate limiter with fresh keys could lift a throttled login.** A full limiter
  (50,000 keys) dropped the oldest 10% of keys whatever their state, and the login throttle
  shared it with every `rate_limit=`, so about 50,000 cheap requests bought 10 more password
  guesses. Keys with nothing left in their window now go first, then the least recent keys
  still under their limit; a key over its limit is never dropped, and if every key is, a new
  caller gets a 429. `authenticate()` has a limiter of its own (`auth.limiter`).
- **`User.create(password=...)` stored the raw password**, as did assigning `user.password`
  and calling `save()`. Saving a `User` whose password isn't a stored hash now raises
  `ValidationError` ("use User.create_user(...) or user.set_password(...)"). `login()` writes
  only `last_login`, so a row an older version stored that way can still sign in.
- **`@server` arguments typed `date`, `datetime`, `time`, `UUID`, `Decimal`, `bytes` or an
  `Enum` arrived unchecked**: the function got whatever JSON was sent, although the OpenAPI
  and MCP schemas promised a format. They are converted now (ISO 8601 text, UUID text,
  finite `Decimal` from text or a number, base64 text, the member's value), and anything else
  is a 400 (`J403`). A parameter annotated with a type Jongo can't check is refused when the
  function is defined (`J405`; see Changed).
- **Login timing revealed accounts with no password.** A user with a blank or unusable
  password answered a wrong guess at once, while an unknown username cost one PBKDF2 hash.
  Every path now does one hash.
- **`Schema.from_model()` included `is_admin` by default**, so a sign-up form built from
  `auth.User` accepted `{"is_admin": true}`. `is_admin`, `is_staff`, `is_superuser` and
  `password` are left out unless `only=` names them, with a warning outside production.
- **`style={...}` values could add CSS declarations in server-rendered HTML.** A value
  containing `;`, `{`, `}`, `<` or a backslash, or with unbalanced quotes or parentheses, is
  dropped on the server and in the browser alike (logged at INFO, so `jongo dev` shows it).
- **The official sign-in pattern was an open redirect.** `examples/patterns/02_auth.py`
  redirected to whatever `?next=` said (`https://evil.example`, `//evil.example`,
  `/\evil.example`). It now keeps `next` only when `is_local_url()` accepts it, and passes the
  request to `authenticate()` so wrong passwords are also counted per address. If you copied
  pattern 02, make the same change.

### Fixed

HTTP, live streams and MCP:

- A non-ASCII byte in the `jongo_csrf`/`jongo_session` cookie or `X-CSRF-Token` made every
  request a 500; such a token is treated as absent. A multipart part with an unknown charset
  was a 500; it is read as UTF-8.
- Deeply nested JSON was a 500 (`RecursionError`) on `/_jongo/rpc/` and `/_jongo/mcp`; it is a
  400 (`J402`, or `J422` for MCP). One such line no longer ends `jongo mcp`.
- `/static/` with a NUL byte or an over-long name was a 500; it is a 404.
- `redirect()` to a URL with non-ASCII characters sent a truncated response on the built-in
  server. `redirect()` percent-encodes what a `Location` header can't carry (`/€` becomes
  `/%E2%82%AC`); any other header a server can't send is a 500 that names it, `set_cookie()`
  refuses a value it can't encode, and the test client fails on such headers too.
- A `bool` or `list` page/route query parameter could never be set (always `J211`). `bool`
  accepts `true/1/on/yes` and `false/0/off/no`; `list[int]` and friends collect a repeated key.
- The over-cap live channel refusal had no code. It is `J210`, says how many channels were
  asked for and names `live.MAX_CHANNELS` (20); `/_jongo/live` refusals are JSON. In the
  browser, a refused stream no longer retries forever: it is probed once, a 4xx stops the
  retries and reports the server's message, and in dev the runtime warns before connecting
  with more than 20 channels.
- `app.root` fell back to the current directory when `Jongo(name)` wasn't an imported module's
  name, putting the database, the secret key and `static/` in the wrong place. It now uses the
  directory of the file that created the app, and the current directory only as a last
  resort, with a warning.

Compiler and browser runtime:

- Any `data:` URL in an `href` or `src` threw while mounting or hydrating, so one stored
  `data:` avatar broke the page for every viewer. Browser and server now give the same
  verdict for every URL.
- A module whose name starts with a digit (`14_shared_validation.py`, loaded under its file
  name) compiled to invalid JavaScript; unit names get a `_` prefix when they need one.
  `jongo check`, `jongo doctor`, `jongo build` and server start also parse the finished bundle
  with `node --check` when node is installed, and report a failure as `J100` naming the line
  and the unit.
- `(7).numerator` broke the whole bundle; it is a `J104` compile error. Unsupported `math`,
  `random`, `time` and `json` members (`math.gcd`, `random.sample`, `time.sleep`) are `J103`
  compile errors instead of failing in the browser; `json.JSONDecodeError` exists there now.
- A keyed list reorder blurred the focused input; the runtime uses `moveBefore` where the
  browser has it and otherwise restores focus and the selection.
- Server-rendered and browser text differed: the browser wrote attribute values and number
  children with JavaScript's `String()` (`1,2`, `[object Object]`), and `inf`/`nan` props
  arrived as `null`. It now writes what `str()` writes (`[1, 2]`, `(1, 2)`, `{'k': 1}`,
  `inf`), and non-finite floats in props and bundle constants arrive as floats.
- `forms.Float` accepted NaN (it compares false with every bound); it is refused. `forms.Int`
  raised a 500 on more than 4300 digits; leading zeros are dropped and more than 16
  significant digits is the type message, on both sides. `J115` now also refuses patterns the
  browser's Unicode-mode `RegExp` can't read: `x{,3}`, an unescaped `{` or `}`, `\a`, `\U` and
  octal escapes.
- Browser code now matches CPython in:
  - arithmetic: `/`, `//`, `%`, `**` and `divmod` by zero (or `False`) raise
    `ZeroDivisionError`; float `//`, `%` and `divmod` follow CPython (`0.1 % 1` is `0.1`);
    `pow(a, b, m)` is exact and takes a negative exponent; `~x` is no longer 32-bit;
    `round(x, n)` is exact from `1e21`; `sum()` of floats is compensated, as on 3.12+;
  - comparisons: `<`, `<=`, `>`, `>=` compare lists and tuples item by item and sets by
    inclusion, and raise `TypeError` for `None` or mixed types (also in `sorted`, `min`,
    `max`); a chained comparison evaluates each operand once;
  - containers: sets compare tuples by value (`(0, 0) in seen`); `True` works as an index and a
    dict key; `*=`, `|=`, `-=`, `&=` and `^=` mutate in place; tuple slices,
    `partition()`/`rpartition()` and `*args` are tuples; a `"__proto__"` key is kept;
  - methods read without a call: `max(d, key=d.get)`, `sorted(xs, key=str.lower)` and
    `map(str.upper, xs)` work, and a `key` that isn't callable is a `TypeError`;
  - strings: `find`, `rfind`, `index`, `rindex`, `count`, `startswith`, `endswith` and
    `list.index` take `start`/`end` (a `find` loop no longer hangs the tab); offsets and
    padding count code points; `strip()`/`split()` use `str.isspace()`'s whitespace;
    `rsplit()` splits from the right; `splitlines()`, `replace("", x)`, `isdigit()`,
    `isnumeric()`, `title()`, `capitalize()`, `casefold()` and `center()` follow Python;
  - text: `float()` follows Python's grammar (`float("")` is a `ValueError`); `f"{2.5:.0f}"`
    and `"%.2f" % 0.125` round half-to-even; `%`-formatting supports every conversion and
    flag; `repr()` picks quotes and escapes as CPython does; floats switch to exponent form
    below `1e-4` and from `1e16`; `bool(float("nan"))` is `True`;
  - `json.dumps` writes Python's text and takes `sort_keys`, `indent`, `separators`,
    `ensure_ascii`, `allow_nan` and `default`; a `json.loads` error is a
    `json.JSONDecodeError`, which `except ValueError` catches;
  - scope and errors: `nonlocal` on a loop variable no longer throws; `except AttributeError`
    catches `None.attr`; a carriage return in f-string text stays a carriage return.

Auth, server functions and OpenAPI:

- The OpenAPI request body of a `@server` function required `kwargs`, while the browser posts
  `{"args": [...]}`. It now describes `args` and `kwargs`, neither required by itself, with an
  `anyOf` of each valid way to pass the required parameters. The MCP `inputSchema` is
  unchanged.
- Two processes signing in at once after an upgrade could fail with `J303` (both ran
  `CREATE TABLE jongo_sessions`); the check and the create are now one write transaction.
- A `float` argument of `10**400` was a 500; it is a 400 (`J403`).

ORM and migrations (verified on SQLite; see Known limits):

- `DateTime` lookups given an ISO string (`"2024-03-10T12:00:00Z"`, `"...+05:00"`) compared
  raw text and returned the wrong rows. Strings are parsed and compared as the instant they
  name; text that doesn't parse is still compared as text, so `__startswith="2024-03"` keeps
  working. `Date` lookups accept full datetime strings.
- A NUL character in a `contains`/`startswith`/`endswith` term (or the `i…` forms and
  `iexact`) matched unrelated rows on SQLite; such a term now matches nothing, as on
  PostgreSQL.
- Integers beyond 64 bits gave a bare `OverflowError` (a 500 with no code). They fail
  validation with `J304` for an `Int`, a `ForeignKey` or an explicit `id`, and lookups answer
  as Python would (`get(id=2**63)` raises `DoesNotExist`). `filter(n__in=["abc"])` on a number
  column no longer matches NULL rows.
- `db.JSON` stored `NaN` and `Infinity`, which aren't JSON; saving one is a `J304`.
- A SQLite table rebuild reset the `AUTOINCREMENT` counter, so the ids of deleted rows were
  handed out again. The counter is carried over.
- Changing a `Text` field to `Int`, `Float` or `Bool` on SQLite went through silently when
  rows held values that don't convert, and one such row made every read of a `Float` field
  fail. `migrate()` refuses with `J303`, naming the column, the row count and the first ids;
  `--allow-destructive` applies it and keeps those values.
- After a field was removed, every insert failed with "__all__: This field is required."; the
  error now names the leftover `NOT NULL` column and how to drop it.
- A `ForeignKey` to an undefined model was reported as `J303` with an unhelpful hint; it is
  `J310` with the hint to define or import the model, in `migrate` and `jongo check`.
- `help_text=` and `on_delete="NO ACTION"` were documented but rejected. `help_text=` is an
  alias for `help=`, and `NO ACTION` is a valid `on_delete`.

CLI, examples and docs:

- Pattern 12 failed its own documented `pytest` command (1 passed, 4 errors); its app is now at
  module level with a fresh in-memory database per test, and the suite runs it. Pattern 06
  showed the oldest 50 chat messages instead of the newest.
- `--json` printed nothing on stdout when `routes`, `context`, `sessions` or `explain` failed;
  it prints `{"ok": false, "error": {...}}` and exits 1.
- Ordinary CLI mistakes printed tracebacks: a `--port` outside 0-65535 (now exit 2), a
  `--host` the machine can't listen on (`J505`, exit 3, and `jongo dev`'s reloader stops), an
  unwritable `-o` path, `jongo build` or `jongo new` target (new `J515`). A bad `$PORT` only
  affects `jongo run`.
- `jongo sessions --revoke 99999` printed "revoked 0 session(s)" and exited 0; it exits 1.
  Listing sessions on a database where nobody has signed in no longer fails with `J302`.
- `ratelimit.parse(("5", "60"))` (or NaN, infinity, a bool) raised a bare `TypeError`; it is
  `J513`. `doctor(app, port=99999)` raised `OverflowError`; it reports a failed check. The MCP
  `jongo_get` and `jongo_describe` tools say what to pass instead of giving JSON-RPC advice.
- The README's testing snippet failed against the README's own app; guide §9 named
  `help_text` as the field option; the patterns README said thirteen patterns (there are
  sixteen). All fixed, with tests.

### Changed (behaviour of existing apps)

- **New refusals.** Forms over 1000 fields or 1000 parts (413, `J212`). Under `jongo dev`, a
  `Host` that isn't localhost or an IP address (400, `J214`): add a tunnel's name to
  `allowed_hosts`. An `http://` `Origin` on an `https` request (403, `J207`). A server-call
  redirect to a non-http(s) scheme (500, `J408`). More than `live.MAX_SUBSCRIBERS` live
  connections (503). Saving a `User` with an unhashed password (`ValidationError`). A
  `@server` parameter annotated with a type Jongo can't check, such as a plain class,
  `Callable` or `type[X]`, even inside a container or union (`J405` when the function is
  defined; annotate with `Any` to check the value yourself). Malformed `date`/`UUID`/
  `Decimal`/`Enum` arguments (`J403`) and query parameters (`J211`). Integers outside
  -2^63 to 2^63-1 in `Int`, `ForeignKey` and `id` (`J304`). `NaN`/`Infinity` in `db.JSON`
  (`J304`). A SQLite type change that would leave values unconverted, unless
  `--allow-destructive` (`J303`; `jongo dev` stops at start with it).
- **New default headers**: `X-Frame-Options: SAMEORIGIN`, and `Content-Security-Policy:
  frame-ancestors 'self'` when the response has no CSP. An app meant to be embedded on other
  sites passes `Jongo(frame_options=None)`.
- **Rate limits count the address and the user.** Signed-in users behind one address now
  share that address's count for each `rate_limit=`; raise `ratelimit.ADDRESS_FACTOR` if
  that is too tight. The login throttle is keyed on the exact username (`"ANN "` and `"ann"`
  count separately), and `ratelimit.limiter.reset()` no longer clears it (use
  `auth.limiter.reset()`).
- **Browser code raises where it used to return a wrong value**: `ZeroDivisionError`,
  `TypeError` for unorderable comparisons, a non-callable `key` or an unhashable set member,
  `ValueError` from `float("")`, `OverflowError` from `int(float("inf"))`. Floats from `1e16`
  and below `1e-4` print in exponent form, and since a JavaScript number can't tell an int
  from a float, whole numbers from `1e16` up print that way too. `navigate()` refuses
  `mailto:` and `tel:` as well; use a link for those.
- `Schema.from_model()` without `only=` leaves out the privileged fields; name them in
  `only=` to include them. `portable_pattern()` refuses more patterns (`J115`).
- In a union, an option the value already is exactly is chosen before converting:
  `int | str` keeps `"007"`, `int | float` keeps `2.0` a float. `TypedDict`, `NewType`,
  `Annotated[...]` and the abstract containers are checked at the `@server` boundary; a
  `TypedDict` argument arrives with only its declared keys.
- `DateTime` and `Date` lookups compare ISO strings as instants (results change for apps that
  filtered with strings). A NUL in a pattern lookup matches nothing. `ne` against an
  out-of-range integer matches every row, NULL included.
- `redirect()` percent-encodes characters a `Location` can't carry (non-ASCII, spaces,
  backslashes, control characters). The CSRF token in a form field is no longer read on
  `/_jongo/` endpoints (server calls always sent the header). `/_jongo/live` refusals are JSON,
  and `HEAD` on it returns headers only.
- With node on PATH, `jongo check` and every start-up check run `node --check` on the bundle
  (about 50-100 ms).
- `jongo new` also writes `tests/conftest.py` (every test gets a fresh in-memory database, so
  tests no longer write to `db.sqlite3`) and `tests/test_app.py`. `jongo sessions --revoke`
  exits 1 when nothing was revoked. `jongo dev --host` with a non-local address prints a
  `J214` note. `J505` is now "Port in use, or the address can't be listened on".
- `TestClient.stream()` runs the response to its first chunk, as a server does, and the test
  client raises `ValueError` on a response header a real server couldn't send.
- New API: `Jongo(allowed_hosts=, frame_options=)`, `Request.max_form_fields`,
  `Request.max_form_parts`, `request.rotate_csrf()`, `Response.wsgi_headers()`,
  `jongo.is_local_url`, `live.MAX_SUBSCRIBERS`, `live.SESSION_CHECK_SECONDS`,
  `live.hub.drop_session()`, `live.hub.drop_user()`, `auth.limiter`,
  `ratelimit.ADDRESS_FACTOR`, `ratelimit.client_keys()`, `forms.PRIVILEGED_FIELDS`,
  `vdom.css_declaration_ok()`, `vdom.to_json_data(browser=True)`, `Field(help_text=)`,
  `on_delete="NO ACTION"`, `Operation.lossy`, and error codes `J214` (Host not allowed) and
  `J515` (can't write the file or directory).

### Known limits

- PostgreSQL was not available, so the ORM and migration fixes are verified on SQLite only
  and the 13 PostgreSQL tests were skipped. With `--allow-destructive`, SQLite applies a lossy
  type change and keeps the unconvertible values; PostgreSQL still refuses it.
- The browser fixes were tested under Node with DOM stand-ins, not in a real browser
  (`navigate()` refusal, the focus restore after a keyed reorder, the refused-stream report).
- Browser code still differs from CPython in a few places, listed in guide §18: integer-like
  dict keys iterate in ascending order (a Map-backed dict is a larger change), generator
  expressions are eager so `any()`/`all()` don't stop early, an integral float prints like an
  int (`str(2.0)` is `"2"`, `str(-0.0)` is `"0"`), `json.loads` rejects the `NaN`/`Infinity`
  tokens Python accepts, `casefold()` doesn't apply the Cherokee and Greek-extended folds, and
  `sum()` can differ when huge values cancel around an int. Server-call results still send
  `inf`/`nan` as `null`.
- A naive datetime, as an object or a string, compared with aware stored values compares the
  stored text and can miss an equal instant. Filter aware columns with aware values.
- Usernames are unique case-sensitively; the throttle no longer depends on it, but an app that
  wants `Ann` and `ann` to be one name must check with `username__iexact` (as pattern 16 does).
  Making it the default needs a data migration.
- `QuerySet.update(password=...)` and bulk inserts bypass the stored-hash check.
- The CSRF token is not bound to the session and has no expiry; `allowed_hosts` is opt-in in
  production. Both need their own design so that open tabs and proxied deployments keep
  working.
- A caller who can cheaply create keys that are over their limit (for example
  `ratelimit.check(key=email)` at `"1/hour"`) can fill the shared limiter, and new callers get
  a 429 until those keys age out. The login throttle has its own limiter and isn't affected.
- SQLite connections of finished request threads are freed by the cyclic garbage collector,
  not when the request ends (bounded, not a leak).
- `jongo_status` says `mode: production` while `jongo_doctor` says `production: false` under
  `jongo mcp`; they answer different questions, which guide §21 and §26 now explain.
- `docs/jongo-guide.html` and `.pdf` predate this release; `docs/guide.md` is current.

### Tests

- 543 → 977 tests (`tests/test_fixes_070_http.py`, `test_fixes_070_compiler.py`,
  `test_parity_070.py`, `test_fixes_070_auth.py`, `test_fixes_070_db.py`,
  `test_fixes_070_cli.py`, and new cases in `test_patterns.py`). The suite passes forwards and
  in reversed file order on Python 3.12, and on 3.10, 3.11, 3.13 and 3.14 (the
  `jsonschema`-backed table tests skip when that package is absent). The PostgreSQL tests
  were not run.
- The stress test's 74 repro scripts were re-run against the release: 61 pass. Of the rest,
  10 can't pass as written (two need Playwright and Chrome; the others count a deliberate
  refusal, such as the coded `J104` for `(7).numerator`, or a now-supported case as the bug,
  or run an app's tests without the new database fixture), and 3 still show a difference
  that is a documented limit above (`str(-0.0)`, an integral float's text, and
  naive-versus-aware datetimes).
- Tests that asserted 0.6 behaviour were updated: the framing default
  (`test_security_041`), the case-insensitive throttle key, per-user-only limits and the
  login limiter (`test_security_060`, `test_mcp`), and the OpenAPI body (`test_schema`).
- The `sum()` parity sample compares against CPython 3.12's results on 3.10 and 3.11, which
  add floats naively.

## 0.6.0 (2026-11-20)

**Contracts.** The types an app already declares now do four more jobs: they validate a form
in the browser and on the server from one definition, they describe the app as an OpenAPI
document, they turn it into tools a coding agent can call, and the three security items left
open by the 0.4.1 audit are closed. Still zero dependencies.

Read "Changed" before upgrading: two security defaults are stricter.

### Added

- **Form schemas: one set of validation rules for the browser and the server**
  (`jongo/forms.py`, guide §23). `forms.Schema("Signup", email=forms.Email(),
  seats=forms.Int(min=1), ...)` with `Text`, `Email`, `Int`, `Float`, `Bool` and `Choice`
  rules (`required`, `min_length`/`max_length`, `min`/`max`, `pattern`, `same_as`, `choices`,
  `default`, custom `messages`), or `forms.Schema.from_model(Model)`.
  - In a component, `forms.validate(schema, values)` and `forms.check(rule, value)` are the
    same Python, compiled to JavaScript, with the schema inlined as data.
  - On the server, a `@server` parameter annotated with a schema is validated before the
    function runs and arrives as the cleaned dict (typed, stripped, defaults applied, and
    only the declared fields); a failure is a 400 with per-field `errors`. `schema.clean()`
    does the same anywhere.
  - `forms.Field(schema=..., name=..., error=...)` renders the labelled control;
    `forms.attrs()` gives the HTML attributes for your own input.
  - Parity is built in, not hoped for: messages are rendered once in Python; patterns are
    rewritten to syntax `re` and `RegExp` share (`\d` becomes `[0-9]`), and a pattern that
    can't be is refused at definition (`J115`); whole numbers beyond 2^53 are refused on both
    sides. `tests/test_forms.py` runs 12 fields against 77 awkward values (non-ASCII digits,
    astral characters, `"1e400"`, bools that are ints) in CPython and in the compiled bundle
    under Node, and requires identical answers.
- **`jongo openapi` and `jongo.openapi(app)`: an OpenAPI 3.1 document from the type hints**
  (`jongo/schema.py`, guide §25). Every `@server` function (request body, typed result, the
  error envelope per status), every page and route (path and query parameters), and a schema
  for each model a function returns, in the shape `to_dict()` produces. Also served at
  `/_jongo/openapi.json` (open in dev; an admin or the status token in production).
  `jongo.json_schema(hint)` and `jongo.schema.function_schema / model_schema / form_schema`
  expose the pieces. `describe()` now carries `input_schema` and `rate_limit` for each server
  function, the app's form `schemas`, and `counts.schemas` in `check()`.
- **An MCP server, so a coding agent can inspect and call the app** (`jongo/mcp.py`, guide
  §26). `jongo mcp` speaks the Model Context Protocol over stdio for the app in the current
  directory (`claude mcp add myapp -- jongo mcp`); `app.mcp(tools=[...])` mounts the same at
  `POST /_jongo/mcp`. Tools: `jongo_context`, `jongo_describe`, `jongo_check`, `jongo_doctor`,
  `jongo_explain`, `jongo_status`, `jongo_openapi`, `jongo_get` (render a page in-process),
  and one per exposed `@server` function, with its input schema generated from the hints. A
  tool call passes through the checks a browser call does (argument validation, login and
  admin guards, rate limits) and a failure is a tool error carrying the Jongo code and hint.
  The HTTP endpoint exists only when mounted, answers this machine only under `jongo dev`,
  requires `Authorization: Bearer $JONGO_MCP_TOKEN` (16+ characters) in production, refuses
  cross-site browser `Origin`s and (in dev) any `Host` that isn't this machine, which covers
  DNS rebinding, and takes identity from `act_as=` only, never from a cookie.
  `jongo mcp --list | --read-only | --user NAME`. `jongo doctor` checks a mounted endpoint.
- **Rate limits** (`jongo/ratelimit.py`, guide §24). `rate_limit="5/minute"` on `@server`,
  `@app.page` and `@app.route`: per signed-in user, else per client address; over the limit
  the handler doesn't run and the response is 429 with `Retry-After` (`J213`).
  `ratelimit.check(request, "3/hour", "scope", key=...)` for a custom key. A sliding window,
  held in process memory (exact for one process; per worker with several).
  `request.client_ip` reads `X-Forwarded-For` only under `trust_proxy=True`.
- **Login throttling, on by default.** `authenticate(username, password, request=None)`
  stops checking passwords after 10 wrong ones for a username in 5 minutes (and 50 per client
  address when given the request) and raises `ratelimit.TooManyRequests` (429, `J213`).
  Attempts are counted before the password is checked, so a burst of parallel guesses can't
  outrun the limit; a throttled attempt does no password hashing; a correct password isn't
  counted. Tune with `auth.LOGIN_ATTEMPTS`, `LOGIN_ATTEMPTS_PER_ADDRESS`, `LOGIN_WINDOW`
  (`None` turns one off). The admin login shows the message.
- **Server-side session revocation.** Every `login()` is a row in `jongo_sessions`
  (`auth.LoginSession`, storing a SHA-256 of the session id, the address and the user agent).
  `auth.sessions(user)`, `auth.revoke_session(id, user=...)`,
  `auth.revoke_sessions(user, keep=request)`, `request.login_session`, and
  `jongo sessions [--user] [--revoke ID] [--revoke-user NAME] [--json]`. `logout()` deletes the
  row, so a copied cookie stops working.
- **Module-qualified names compile.** `forms.validate(...)`, or `helpers.fmt(x)` after
  `import helpers`, in a component used to be `J102` ("module isn't available in browser
  code"); the compiler now resolves `module.name` the way it resolves an imported name.
- Error codes `J115` (form schema can't be built), `J213` (too many requests), `J412`
  (channel guard gave no answer), `J420`–`J422` (MCP: refused, unknown tool, malformed
  message), `J513` (rate limit can't be read), `J514` (MCP endpoint mounted but unusable).
- Three patterns: `14_shared_validation.py`, `15_agent_tools.py`, `16_sessions_and_limits.py`.
  The context pack, the scaffold's `AGENTS.md` and the framework rules describe all of the above.

### Changed (behaviour of existing apps)

- **A channel guard must return a truthy value to allow a subscription.** Returning `None`
  (a guard with no `return`, or a branch that falls off the end) used to allow it; now it
  refuses with `J412`. `jongo check` reports a guard that never returns a value. Guards that
  `return True`/`False` are unaffected.
- **Signed-in users sign in once more after upgrading.** Cookies issued by 0.5 carry no
  server-side session id, so they are treated as signed out. The `jongo_sessions` table is
  created by `jongo migrate` / `jongo dev`, or on the first sign-in if neither has run.
  Code that built a session by hand from `{"_uid", "_pw"}` should use
  `auth.session_data_for(user)` (`TestClient.login()` already does). A signed-in request makes
  one more query than before.
- **`authenticate()` can raise.** Past the limit it raises a 429 `HTTPError` subclass instead
  of returning `None`. Left uncaught in a page, route or server function it becomes the 429
  response, which is usually what you want.
- `jongo check --json` and `describe()` have new keys (`schemas`, `input_schema`,
  `rate_limit`, `app.mcp`); nothing was removed.

### Tests

- 386 → 543 tests (`tests/test_forms.py`, `test_schema.py`, `test_mcp.py`,
  `test_security_060.py`). The suite passes forwards, reversed and shuffled, on Python 3.10
  to 3.14, and on PostgreSQL 16 (552 tests there).
- Checked outside the suite: the MCP server against the official MCP Python SDK client
  (2.3.0) over both transports, the generated documents against `openapi-spec-validator`
  (OpenAPI 3.1), and the three new patterns in headless Chrome.
- `test_agentic.test_describe_lists_what_the_app_declares` depended on which database the
  previous test had configured; it now configures its own.

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
