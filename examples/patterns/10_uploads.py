"""File uploads, and serving what was uploaded.

Run it:  jongo dev examples/patterns/10_uploads.py --port 8779

What to notice
--------------
* An upload is a classic multipart form POST to a route — `request.files["avatar"]`.
  `@server` functions take JSON arguments, so file bytes go through a route instead.
* The stored filename is generated, never taken from the client: a name like
  `../../etc/passwd` must not decide where the bytes land.
* The size and content type are checked before anything is written.
"""

import secrets
from pathlib import Path

from jongo import Jongo, NotFound, Response, db, redirect
from jongo.html import a, button, div, form, h1, img, input_, label, li, p, ul

app = Jongo(__name__, title="Uploads")

UPLOADS = Path(__file__).parent / "uploaded"
MAX_BYTES = 2 * 1024 * 1024
ALLOWED = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif"}


class Upload(db.Model):
    original = db.Text(max_length=255)
    stored = db.Text(max_length=80, unique=True)
    size = db.Int(default=0)


@app.route("/upload", methods=("POST",))
def upload(request):
    uploaded = request.files.get("avatar")
    if uploaded is None:
        return redirect("/?error=missing")
    if uploaded.content_type not in ALLOWED:
        return redirect("/?error=type")
    if uploaded.size > MAX_BYTES:
        return redirect("/?error=size")

    stored = secrets.token_hex(16) + ALLOWED[uploaded.content_type]   # never the client's name
    UPLOADS.mkdir(exist_ok=True)
    (UPLOADS / stored).write_bytes(uploaded.data)
    Upload.create(original=uploaded.filename, stored=stored, size=uploaded.size)
    return redirect("/")


@app.route("/uploaded/<name>")
def serve(request, name: str):
    """Serve by stored name only, and confirm the row exists before reading the disk."""
    row = Upload.filter(stored=name).first()
    if row is None:
        raise NotFound()
    return Response((UPLOADS / row.stored).read_bytes(),
                    content_type="image/" + row.stored.rsplit(".", 1)[-1])


@app.page("/")
def home(request):
    return div(
        h1("Uploads"),
        form(
            input_(type="hidden", name="csrf_token", value=request.csrf_token),
            label("Picture", input_(type="file", name="avatar", accept="image/*")),
            button("Upload"),
            method="post", action="/upload", enctype="multipart/form-data",
        ),
        p(f"Error: {request.query['error']}") if "error" in request.query else None,
        ul([
            li(a(img(src=f"/uploaded/{row.stored}", width=80), href=f"/uploaded/{row.stored}"),
               f" {row.original} ({row.size} bytes)", key=row.pk)
            for row in Upload.all()
        ]),
    )
