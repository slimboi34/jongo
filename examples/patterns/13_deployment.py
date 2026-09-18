"""Production shape: PostgreSQL, a real secret, migrations on start, health check.

Run it locally:
    JONGO_DATABASE=postgres://localhost/app JONGO_SECRET_KEY=dev python -m gunicorn \
        examples.patterns.13_deployment:app --bind 0.0.0.0:8000 --workers 1 --threads 8

What to notice
--------------
* Config comes from the environment, with SQLite as the local default, so the same file
  runs on a laptop and in production.
* `db.migrate()` runs at import, so a fresh container has its schema before the first
  request arrives.
* One worker with threads keeps `broadcast()` reaching every connection: the hub is
  per process. Scale to several workers only with a shared bus in front.
* The health check touches the database, so a load balancer notices a broken database
  rather than just a live process.
"""

import os

from jongo import Jongo, db
from jongo.html import div, h1, p

DATABASE = os.environ.get("JONGO_DATABASE", "app.sqlite3")

app = Jongo(
    __name__,
    title="Production app",
    database=DATABASE,
    trust_proxy=bool(os.environ.get("BEHIND_PROXY")),   # honour X-Forwarded-Proto
)


class Widget(db.Model):
    name = db.Text(max_length=100)


db.migrate()          # schema is in place before the first request


@app.route("/healthz")
def health(request):
    """Ready only if the database answers."""
    Widget.count()
    return {"ok": True, "backend": db.backend()}


@app.page("/")
def home():
    return div(h1("It runs"), p(f"{Widget.count()} widgets, on {db.backend()}."))


# Procfile:
#   web: python -m gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 60
#
# Environment:
#   JONGO_SECRET_KEY   required in production; sessions and CSRF are signed with it
#   JONGO_DATABASE     postgres://user:pw@host/db   (pip install "jongo[postgres]")
#   BEHIND_PROXY       set when a proxy terminates TLS
