# Jongo patterns

Thirteen complete, runnable apps — one per problem people actually hit. Each file is
self-contained: copy it, run it, change it. Every one is exercised by the test suite
(`tests/test_patterns.py`), so none of them can quietly rot.

```bash
jongo dev examples/patterns/01_crud.py --port 8770
```

| | Pattern | What it shows |
|---|---|---|
| 01 | [CRUD](01_crud.py) | List, create, edit, delete; server validation rendered per field |
| 02 | [Auth](02_auth.py) | Sign in/out, `login_required`, a form that works without JavaScript |
| 03 | [Search as you type](03_search.py) | Debounced input, server-side filtering, dropping stale replies |
| 04 | [Pagination](04_pagination.py) | Page number in the URL, `LIMIT`/`OFFSET` via slicing |
| 05 | [Optimistic UI](05_optimistic_ui.py) | Paint the change now, roll back if the server refuses |
| 06 | [Live chat](06_live_chat.py) | `@app.channel` + `broadcast()` + `live()`, no polling |
| 07 | [Live dashboard](07_live_dashboard.py) | Per-team channels, a background thread pushing metrics |
| 08 | [Forms and modals](08_forms_and_modals.py) | A modal as conditional UI; errors as data, not exceptions |
| 09 | [Master–detail](09_master_detail.py) | Real URLs per item; where to read the database (and where not to) |
| 10 | [Uploads](10_uploads.py) | Multipart POST to a route, generated filenames, type and size checks |
| 11 | [Background work](11_background_work.py) | Return immediately, push progress on a per-job channel |
| 12 | [Testing](12_testing.py) | `TestClient`: pages, `rpc()`, `navigate()`, `stream()` for channels |
| 13 | [Deployment](13_deployment.py) | PostgreSQL, secrets, migrate on start, health check, worker shape |

## The three rules these examples keep repeating

1. **Read the database in a page or a `@server` function, never in a component.**
   A component also runs in the browser, where there is no database. Load the rows in the
   page and pass them as props. Jongo catches the mistake when it compiles, not in production.

2. **Validate on the server and return errors as data.**
   `{"errors": {"email": "already taken"}}` renders next to the field. The rules live in one
   place, where they can see the database.

3. **A channel must be declared before anything can subscribe.**
   `@app.channel(...)` decides who may listen, exactly as `@server` decides what may be called.
