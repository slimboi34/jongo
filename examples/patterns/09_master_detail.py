"""Master–detail with real URLs, so every item is linkable.

Run it:  jongo dev examples/patterns/09_master_detail.py --port 8778

What to notice
--------------
* The detail view is its own page at its own URL. Clicking a link navigates without a
  full page load (Jongo intercepts same-origin links) but the URL, the back button and
  a hard refresh all still work.
* `navigate()` does the same thing from code — after saving, after a redirect, whenever.
* The detail page loads its own row on the server. No client-side store to keep in sync.
"""

from jongo import Jongo, component, db, navigate
from jongo.html import a, article, aside, div, h1, h2, li, main, p, ul

app = Jongo(__name__, title="Docs")


class Article(db.Model):
    slug = db.Text(max_length=80, unique=True)
    title = db.Text(max_length=200)
    body = db.Text(blank=True, default="")

    class Meta:
        ordering = ["title"]


@app.page("/")
def index():
    return Layout(links=_links(), current=None, body=p("Pick an article."))


@app.page("/a/<slug>")
def detail(slug: str):
    article_row = Article.get_or_none(slug=slug)
    if article_row is None:
        return Layout(links=_links(), current=None, body=p("No such article."))
    return Layout(
        links=_links(),
        current=slug,
        body=article(h2(article_row.title), p(article_row.body)),
    )


def _links():
    """Read the database in the page function, not in the component.

    A component also runs in the browser, where there is no database. Jongo catches
    this at compile time rather than letting it fail at runtime.
    """
    return [{"slug": row.slug, "title": row.title} for row in Article.all()]


@component
def Layout(links, current, body):
    rows = links
    return div(
        h1("Docs"),
        div(
            aside(ul([
                li(a(row["title"], href=f"/a/{row['slug']}",
                     style={"font_weight": 700 if row["slug"] == current else 400}),
                   key=row["slug"])
                for row in rows
            ])),
            main(body),
            style={"display": "grid", "grid_template_columns": "200px 1fr", "gap": "24px"},
        ),
    )
