# Benchmarks

Three endpoints, three frameworks, one command:

```bash
python benchmarks/bench.py                          # all of them
python benchmarks/bench.py --only jongo --requests 4000 --concurrency 32
```

The apps are in [apps/](apps/) — the same three endpoints in each framework:

| Endpoint | What it does |
|---|---|
| `/json` | return `{"message": "hello"}` |
| `/page` | render a small server-side page (a heading and 20 list items) |
| `/rows` | read 100 rows from SQLite and render them |

## Method, and what is and is not fair

- **Jongo and Django both run under the same gunicorn configuration** (1 worker, 8 threads),
  so that comparison is like for like.
- **FastAPI runs under uvicorn** because it is ASGI. That is a different server model, not
  just a different framework, and a large part of the gap below is the server.
- The Django and FastAPI pages build HTML by string concatenation — no template engine, no
  ORM in FastAPI's case. That makes them a *generous* baseline, not a hobbled one.
- Load is generated in-process by `bench.py` over keep-alive connections, after a warm-up.

## Results

One laptop (Apple Silicon, Python 3.12), 3000 requests at concurrency 16, requests/second:

| | `/json` | `/page` | `/rows` |
|---|---|---|---|
| Jongo (WSGI) | 1496 | 1710 | 485 |
| Django (WSGI) | 2049 | 1062 | 956 |
| FastAPI (ASGI) | 7118 | 5635 | 2091 |

**Read these numbers with the variance in mind.** Across repeated runs on this machine the
same configuration moved by ±30% — Jongo's `/json` came out at 2333, 1826 and 1496 on three
consecutive runs, and Django's `/page` at 2225, 1737 and 1062. Anything inside that band is
noise. Run it on hardware you care about before drawing a conclusion.

What *was* consistent across every run:

1. **FastAPI on uvicorn is 3–4× faster on the simple endpoints.** ASGI plus a minimal
   framework is hard to beat when there is nothing to render.
2. **Jongo and Django trade places on `/json` and `/page`** — the differences there are
   inside the noise band.
3. **Django is consistently ~1.5–2× faster on `/rows`** (956 vs 485 here; 958 vs 618 and
   642 vs 421 on other runs). This one is real and repeatable.

## Why `/rows` costs Jongo more

Profiling one request (100 rows, ~500 nodes) puts about three quarters of the time in
rendering and a twentieth in the database:

| | share of request |
|---|---|
| `render_to_string` (HTML) | ~38% |
| `build` + `serialize` (the hydration tree) | ~28% |
| the page function, including the ORM | ~23% |
| the ORM query itself | ~7% |

Jongo renders the page **twice over**: once to HTML for the first paint, and once as a JSON
tree so the browser can hydrate and take over. Django's endpoint emits a string and stops.
That extra ~28% is what buys you an interactive page without a second codebase — it is the
cost of the model, not a bug. The trade only pays off when the page is actually interactive;
for a purely static page, Django's output is cheaper and Jongo has nothing to offer.

The renderer got about 10% faster while these benchmarks were being written (0.799 → 0.715 ms
per `/rows` request in-process, best of 7 × 300): the HTML was being rendered from a
*re-parsed* copy of the serialised tree, and the tag-name validation regex was running once
per node instead of once per distinct tag. The remaining cost is the two passes themselves.

## What is not measured here

Cold start, memory, concurrency beyond 16, PostgreSQL, real network latency, TLS, static
files, and the thing the whole framework exists for: how long it takes a person to build
and change a feature.
