"""The Django side of the benchmark: the same three endpoints."""

import os
import sys
from pathlib import Path

import django
from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.urls import path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if not settings.configured:
    settings.configure(
        DEBUG=False,
        SECRET_KEY="bench",
        ALLOWED_HOSTS=["*"],
        ROOT_URLCONF=__name__,
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3",
                               "NAME": os.environ.get("BENCH_DJANGO_DB", "/tmp/bench_django.sqlite3")}},
        INSTALLED_APPS=["benchapp"],
        TEMPLATES=[],
        USE_TZ=True,
        MIDDLEWARE=[],
    )
    django.setup()

from benchapp.models import Item  # noqa: E402


def json_endpoint(request):
    return JsonResponse({"message": "hello"})


def page(request):
    rows = "".join(f"<li>row {i}</li>" for i in range(20))
    return HttpResponse(
        f"<!doctype html><html><body><div><h1>Hello</h1>"
        f"<p>A server-rendered page.</p><ul>{rows}</ul></div></body></html>"
    )


def rows(request):
    items = Item.objects.all()[:100]
    body = "".join(f"<li><span>{i.name}</span><span>{i.price:.2f}</span></li>" for i in items)
    return HttpResponse(
        f"<!doctype html><html><body><div><h1>Items</h1><ul>{body}</ul></div></body></html>"
    )


urlpatterns = [path("json", json_endpoint), path("page", page), path("rows", rows)]

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()


def setup_database():
    from django.db import connection

    if Item._meta.db_table not in connection.introspection.table_names():
        with connection.schema_editor() as editor:
            editor.create_model(Item)
    if not Item.objects.exists():
        Item.objects.bulk_create([Item(name=f"Item {i}", price=i * 1.5) for i in range(100)])


setup_database()
