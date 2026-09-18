"""Search as you type, filtered on the server.

Run it:  jongo dev examples/patterns/03_search.py --port 8772

What to notice
--------------
* The query runs on the server — the browser never sees rows the user didn't ask for.
* Keystrokes are debounced in the component, so typing "hello" is one query, not five.
* Every response carries the query it answered, so a slow reply for "he" can't
  overwrite the results for "hello". Out-of-order responses are the classic
  search-as-you-type bug.
"""

from jongo import Jongo, component, db, js, ref, server, state
from jongo.html import div, h1, input_, li, mark, p, ul

app = Jongo(__name__, title="Search")


class City(db.Model):
    name = db.Text(max_length=100, index=True)
    country = db.Text(max_length=100)


@server
def search_cities(request, query: str = "") -> dict:
    """Return matches for `query`, echoing the query back so stale replies can be dropped."""
    query = query.strip()
    if not query:
        return {"query": query, "results": []}
    rows = City.filter(name__icontains=query)[:10]
    return {"query": query, "results": [{"id": c.pk, "name": c.name, "country": c.country}
                                        for c in rows]}


@component
def CitySearch():
    query = state("")
    results = state([])
    pending = state(False)
    timer = ref(None)

    async def run(text):
        pending.set(True)
        reply = await search_cities(text)
        pending.set(False)
        if reply["query"] == query.value:          # ignore a reply the user has typed past
            results.set(reply["results"])

    def on_type(event):
        text = event.target.value
        query.set(text)
        if timer.current:
            js.clearTimeout(timer.current)          # `js` is the browser's global scope
        timer.current = js.setTimeout(lambda: run(text), 150)

    return div(
        input_(value=query.value, placeholder="Search cities", on_input=on_type),
        p("Searching…") if pending.value else None,
        ul([li(f"{row['name']} — {row['country']}", key=row["id"]) for row in results.value]),
        p("No matches.") if query.value and not results.value and not pending.value else None,
    )


@app.page("/")
def home():
    return div(h1("Cities"), CitySearch())
