"""Benchmark Jongo against Django and FastAPI on the same three endpoints.

    python benchmarks/bench.py              # all frameworks, all endpoints
    python benchmarks/bench.py --only jongo
    python benchmarks/bench.py --requests 4000 --concurrency 32

Method
------
Jongo and Django are both WSGI and run under the *same* gunicorn configuration, so that
comparison is like for like. FastAPI is ASGI and runs under uvicorn, which is a different
server model — it is included because people ask, and labelled as such.

Each run warms up, then issues `--requests` requests across `--concurrency` connections
and reports throughput and latency percentiles. Numbers from one laptop are not a
league table; run it yourself on hardware you care about.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
PORT = 8901
ENDPOINTS = [
    ("json", "/json", "a plain JSON response"),
    ("page", "/page", "a server-rendered page"),
    ("rows", "/rows", "100 rows read and rendered"),
]

SERVERS = {
    "jongo": {
        "kind": "WSGI (gunicorn)",
        "command": [sys.executable, "-m", "gunicorn", "apps.jongo_app:app",
                    "--bind", f"127.0.0.1:{PORT}", "--workers", "1", "--threads", "8",
                    "--log-level", "error"],
    },
    "django": {
        "kind": "WSGI (gunicorn)",
        "command": [sys.executable, "-m", "gunicorn", "apps.django_app:application",
                    "--bind", f"127.0.0.1:{PORT}", "--workers", "1", "--threads", "8",
                    "--log-level", "error"],
    },
    "fastapi": {
        "kind": "ASGI (uvicorn)",
        "command": [sys.executable, "-m", "uvicorn", "apps.fastapi_app:app",
                    "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "error"],
    },
}


def wait_for_server(path="/json", timeout=30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=2)
            conn.request("GET", path)
            conn.getresponse().read()
            conn.close()
            return True
        except Exception:
            time.sleep(0.2)
    return False


def hammer(path: str, requests: int, concurrency: int) -> dict:
    """Issue `requests` GETs over `concurrency` keep-alive connections."""
    per_worker = max(1, requests // concurrency)
    latencies: list[float] = []
    failures = 0

    def worker() -> tuple[list[float], int]:
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
        mine: list[float] = []
        bad = 0
        for _ in range(per_worker):
            start = time.perf_counter()
            try:
                conn.request("GET", path)
                response = conn.getresponse()
                response.read()
                if response.status != 200:
                    bad += 1
            except Exception:
                bad += 1
                conn.close()
                conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
                continue
            mine.append((time.perf_counter() - start) * 1000)
        conn.close()
        return mine, bad

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for mine, bad in pool.map(lambda _: worker(), range(concurrency)):
            latencies.extend(mine)
            failures += bad
    elapsed = time.perf_counter() - started

    if not latencies:
        return {"rps": 0.0, "p50": 0.0, "p99": 0.0, "n": 0, "failures": failures}
    ordered = sorted(latencies)
    return {
        "rps": len(latencies) / elapsed,
        "p50": statistics.median(ordered),
        "p99": ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))],
        "n": len(latencies),
        "failures": failures,
    }


def run_server(name: str):
    spec = SERVERS[name]
    env = {**os.environ, "PYTHONPATH": str(ROOT), "JONGO_SECRET_KEY": "bench"}
    return subprocess.Popen(spec["command"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            preexec_fn=os.setsid)


def measure(name: str, requests: int, concurrency: int) -> dict | None:
    process = run_server(name)
    try:
        if not wait_for_server():
            print(f"  {name}: server did not start — skipped")
            return None
        results = {}
        for key, path, _ in ENDPOINTS:
            hammer(path, min(200, requests // 4), max(2, concurrency // 4))  # warm up
            results[key] = hammer(path, requests, concurrency)
            row = results[key]
            print(f"  {name:8s} {key:5s}  {row['rps']:8.0f} req/s   "
                  f"p50 {row['p50']:6.2f} ms   p99 {row['p99']:6.2f} ms"
                  + (f"   failures {row['failures']}" if row["failures"] else ""))
        return results
    finally:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            process.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            process.kill()
        time.sleep(0.5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=2000)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--only", choices=sorted(SERVERS), action="append")
    parser.add_argument("--json", type=Path, help="also write the raw numbers here")
    args = parser.parse_args()

    names = args.only or list(SERVERS)
    print(f"{args.requests} requests, concurrency {args.concurrency}, "
          f"python {sys.version.split()[0]}\n")

    results = {}
    for name in names:
        print(f"{name} — {SERVERS[name]['kind']}")
        measured = measure(name, args.requests, args.concurrency)
        if measured:
            results[name] = measured
        print()

    if results:
        print("throughput, requests/second (higher is better)\n")
        header = f"{'':10s}" + "".join(f"{key:>12s}" for key, _, _ in ENDPOINTS)
        print(header)
        for name, rows in results.items():
            print(f"{name:10s}" + "".join(f"{rows[key]['rps']:12.0f}" for key, _, _ in ENDPOINTS))

    if args.json:
        args.json.write_text(json.dumps(results, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
