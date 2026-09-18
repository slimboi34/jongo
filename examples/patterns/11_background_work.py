"""A long job: start it, return immediately, push progress as it runs.

Run it:  jongo dev examples/patterns/11_background_work.py --port 8780

What to notice
--------------
* The server function starts a thread and returns a job id straight away. A request
  that waits for slow work ties up a worker and eventually times out.
* Progress reaches the browser over a channel keyed by job id, so two people running
  two jobs never see each other's progress.
* The job's final state is stored, so a browser that reconnects mid-job can still find
  out how it ended — a live feed alone would have missed it.
"""

import threading
import time
import uuid

from jongo import Jongo, broadcast, component, db, live, server, state
from jongo.html import button, div, h1, p, progress, span

app = Jongo(__name__, title="Jobs")


class Job(db.Model):
    key = db.Text(max_length=40, unique=True)
    status = db.Text(max_length=20, default="running")
    percent = db.Int(default=0)


@app.channel("job:<key>")
def job_channel(request, key):
    """Anyone holding the job key may watch it; the key is unguessable."""
    return Job.filter(key=key).exists()


@server
def start_export(request) -> dict:
    job = Job.create(key=uuid.uuid4().hex)
    threading.Thread(target=_run, args=(job.key,), daemon=True).start()
    return {"key": job.key}


def _run(key):
    """The worker. In a real app this is a queue consumer, not a thread."""
    for percent in range(0, 101, 10):
        time.sleep(0.2)
        Job.filter(key=key).update(percent=percent)
        broadcast(f"job:{key}", {"percent": percent, "status": "running"})
    Job.filter(key=key).update(status="done", percent=100)
    broadcast(f"job:{key}", {"percent": 100, "status": "done"})


@component
def Export():
    key = state("")
    percent = state(0)
    status = state("idle")

    def progressed(update):
        percent.set(update["percent"])
        status.set(update["status"])

    live(f"job:{key.value}" if key.value else None, progressed)

    async def start(event):
        status.set("running")
        percent.set(0)
        reply = await start_export()
        key.set(reply["key"])

    return div(
        button("Start export", on_click=start, disabled=status.value == "running"),
        progress(value=percent.value, max=100) if key.value else None,
        p(f"{status.value} — {percent.value}%"),
    )


@app.page("/")
def home():
    return div(h1("Export"), Export())
