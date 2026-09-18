"""Pagination: a page at a time, with the page number in the URL.

Run it:  jongo dev examples/patterns/04_pagination.py --port 8773

What to notice
--------------
* The page number lives in the URL, so a link to page 3 is a link to page 3 —
  shareable, bookmarkable and correct in the back button.
* Slicing a QuerySet issues a LIMIT/OFFSET; the rows never all load into memory.
* `count()` is one query, not a fetch of everything.
"""

from jongo import Jongo, db
from jongo.html import a, div, h1, li, nav, p, span, ul

app = Jongo(__name__, title="Posts")
PER_PAGE = 10


class Post(db.Model):
    title = db.Text(max_length=200)

    class Meta:
        ordering = ["-id"]


@app.page("/")
@app.page("/page/<int:number>", name="page")
def listing(request, number: int = 1):
    total = Post.count()
    pages = max(1, -(-total // PER_PAGE))        # ceiling division
    number = min(max(number, 1), pages)
    start = (number - 1) * PER_PAGE
    rows = Post.all()[start:start + PER_PAGE]    # LIMIT/OFFSET, not a full fetch

    return div(
        h1("Posts"),
        ul([li(post.title, key=post.pk) for post in rows]),
        p(f"{total} posts, page {number} of {pages}"),
        Pager(number=number, pages=pages),
    )


def Pager(number, pages):
    """A plain function returning UI — no state, so it needs no @component."""
    return nav(
        a("← Newer", href=f"/page/{number - 1}") if number > 1 else span("← Newer"),
        " ",
        a("Older →", href=f"/page/{number + 1}") if number < pages else span("Older →"),
    )
