"""The FastAPI side: JSON and hand-written HTML (FastAPI has no ORM or template layer)."""

import os
import sqlite3

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

DB = os.environ.get("BENCH_FASTAPI_DB", "/tmp/bench_fastapi.sqlite3")
app = FastAPI()


def connect():
    conn = sqlite3.connect(DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


with connect() as setup:
    setup.execute("CREATE TABLE IF NOT EXISTS item (id INTEGER PRIMARY KEY, name TEXT, price REAL)")
    if setup.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 0:
        setup.executemany("INSERT INTO item (name, price) VALUES (?, ?)",
                          [(f"Item {i}", i * 1.5) for i in range(100)])


@app.get("/json")
def json_endpoint():
    return {"message": "hello"}


@app.get("/page", response_class=HTMLResponse)
def page():
    rows = "".join(f"<li>row {i}</li>" for i in range(20))
    return (f"<!doctype html><html><body><div><h1>Hello</h1>"
            f"<p>A server-rendered page.</p><ul>{rows}</ul></div></body></html>")


@app.get("/rows", response_class=HTMLResponse)
def rows():
    conn = connect()
    items = conn.execute("SELECT name, price FROM item LIMIT 100").fetchall()
    conn.close()
    body = "".join(f"<li><span>{r['name']}</span><span>{r['price']:.2f}</span></li>" for r in items)
    return (f"<!doctype html><html><body><div><h1>Items</h1><ul>{body}</ul></div></body></html>")
