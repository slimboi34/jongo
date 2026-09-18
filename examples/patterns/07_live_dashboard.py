"""A dashboard the server pushes to: per-user channels and a background ticker.

Run it:  jongo dev examples/patterns/07_live_dashboard.py --port 8776

What to notice
--------------
* The channel pattern carries a parameter (`metrics:<int:team>`), and the guard decides
  whether *this* request may listen to *that* team. Guessing a channel name gets you
  nothing.
* A background thread broadcasts on a timer — anything that can call a function can push:
  a cron job, a webhook handler, a queue worker.
* `broadcast()` returns how many connections it reached, which is useful for logging and
  for skipping work when nobody is watching.
"""

import random
import threading

from jongo import Jongo, broadcast, component, live, state
from jongo.html import div, h1, li, p, span, ul

app = Jongo(__name__, title="Dashboard")
TEAMS = {1: "Platform", 2: "Growth"}


@app.channel("metrics:<int:team>")
def metrics(request, team):
    """Only teams this session belongs to. Default here: team 1, for the demo."""
    allowed = request.session.get("teams", [1])
    return team in allowed


@component
def Metrics(team, name):
    latest = state({"requests": 0, "errors": 0})
    history = state([])

    def received(point):
        latest.set(point)
        history.set([*history.value, point][-20:])   # keep the last 20

    live(f"metrics:{team}", received)

    return div(
        h1(name),
        p(span("requests/s: "), span(str(latest.value["requests"]))),
        p(span("errors/s: "), span(str(latest.value["errors"]))),
        ul([li(f"{point['requests']} req, {point['errors']} err", key=index)
            for index, point in enumerate(history.value)]),
    )


@app.page("/")
def home(request):
    return Metrics(team=1, name=TEAMS[1])


def start_ticker(interval=2.0):
    """Push a reading every couple of seconds. Call this from your server start-up."""

    def tick():
        while True:
            for team in TEAMS:
                broadcast(f"metrics:{team}", {
                    "requests": random.randint(50, 500),
                    "errors": random.randint(0, 5),
                })
            threading.Event().wait(interval)

    thread = threading.Thread(target=tick, daemon=True, name="metrics-ticker")
    thread.start()
    return thread


_ticker_started = threading.Lock()


@app.before_request
def ensure_ticker(request):
    """Start the ticker on the first request, so importing this file starts nothing."""
    if _ticker_started.acquire(blocking=False):
        start_ticker()
