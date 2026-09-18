"""The Jongo side of the benchmark: JSON, a server-rendered page, and an ORM read."""

import os

from jongo import Jongo, db
from jongo.html import div, h1, li, p, span, ul

app = Jongo(__name__, title="Bench", database=os.environ.get("BENCH_DB", "/tmp/bench_jongo.sqlite3"),
            secret_key="bench")


class Item(db.Model):
    name = db.Text(max_length=100)
    price = db.Float(default=0.0)


db.migrate()
if Item.count() == 0:
    with db.transaction():
        for i in range(100):
            Item.create(name=f"Item {i}", price=i * 1.5)


@app.route("/json")
def json_endpoint(request):
    return {"message": "hello"}


@app.page("/page")
def page():
    """A server-rendered page with no database work."""
    return div(h1("Hello"), p("A server-rendered page."),
               ul([li(f"row {i}") for i in range(20)]))


@app.page("/rows")
def rows():
    """Read 100 rows and render them."""
    items = Item.all()[:100]
    return div(
        h1("Items"),
        ul([li(span(item.name), span(f"{item.price:.2f}"), key=item.pk) for item in items]),
    )
