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
| Jongo (WSGI) | 2530 | 2315 | 791 |
| Django (WSGI) | 2591 | 2508 | 856 |
| FastAPI (ASGI) | 6970 | 6835 | 1597 |

**Read these numbers with the variance in mind.** Across repeated runs on this machine the
same configuration moved by ±30% — Jongo's `/json` came out at 2333, 1826, 1496 and 2530 on
four runs, and Django's `/page` at 2225, 1737, 1062 and 2508. Anything inside that band is
noise. Because whole runs drift together, the *ratio* between two frameworks in the same run
is worth more than either number, and that is what the analysis below uses. Run it on
hardware you care about before drawing a conclusion.

What was consistent across every run:

1. **FastAPI on uvicorn is 3-4x faster on the simple endpoints.** ASGI plus a minimal
   framework is hard to beat when there is nothing to render.
2. **Jongo and Django trade places on `/json` and `/page`** - the differences there are
   inside the noise band.
3. **`/rows` is the endpoint that separates them**, and it is where Jongo has moved most.

## Why `/rows` costs Jongo more

Jongo renders the page for two audiences: HTML for the first paint, and a JSON tree so the
browser can hydrate and take over. Django's endpoint builds a string and stops. That second
audience is the whole point - it is what makes the page interactive without a second
codebase - but it is not free, and `/rows` is where you see the bill.

It used to be a much bigger bill. Two rounds of work on the render path:

| | Jongo `/rows` over Django `/rows` | in-process per request |
|---|---|---|
| Before | 0.51x (about half Django's throughput) | 0.799 ms |
| After removing the re-parse and per-node tag validation | - | 0.715 ms |
| After rendering straight from the serialised tree | **0.92x** | **0.512 ms** |

The largest win was structural. The page path used to serialise the tree to JSON, rebuild a
second VNode tree from that JSON, and render *that* to HTML - the round trip existed so the
server's HTML and the browser's hydration data could not drift apart. Rendering directly
from the serialised data keeps that guarantee in a stronger form (there is now one object,
not a copy) and removes a whole pass and a whole tree from every request.

Profiling one request today (100 rows, ~500 nodes):

| | share of request |
|---|---|
| `render_to_string` (HTML) | ~42% |
| `serialize` (the hydration tree) | ~14% |
| the page function, including the ORM | ~32% |
| the ORM query itself | ~10% |

`build` - previously ~19% on its own - no longer appears. What remains is one walk to
produce the data and one to render it, which is the cost of the model rather than a defect.
For a page that is never interactive, Django's single pass is still cheaper and Jongo has
nothing to offer it.

## What is not measured here

Cold start, memory, concurrency beyond 16, PostgreSQL, real network latency, TLS, static
files, and the thing the whole framework exists for: how long it takes a person to build
and change a feature.
