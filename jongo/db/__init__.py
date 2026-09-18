"""Jongo's ORM: models, querysets, transactions and auto-migrations.

Runs on SQLite (the zero-config default) or PostgreSQL — the same models, unchanged.

    from jongo import db

    class Todo(db.Model):
        title = db.Text(max_length=200)
        done = db.Bool(default=False)

    db.configure("sqlite:///app.sqlite3")   # or "postgres://user@host/app"
    db.migrate()
    Todo.create(title="Write docs")
"""

from __future__ import annotations

from .fields import JSON, Bool, Date, DateTime, Field, Float, ForeignKey, Int, Text, ValidationError
from .query import DoesNotExist, MultipleObjectsReturned, Q, QuerySet, integrity_error
from .models import Model, get_model, models_registry
from .migrate import MigrationError, Operation, migrate, plan_migrations

# Imported last on purpose: the `query` function must shadow the `query` submodule name.
from .connection import (  # noqa: E402
    backend,
    close_connections,
    configure,
    database_path,
    execute,
    get_connection,
    query,
    transaction,
)

__all__ = [
    "Model",
    "Field",
    "Text",
    "Int",
    "Float",
    "Bool",
    "DateTime",
    "Date",
    "JSON",
    "ForeignKey",
    "Q",
    "QuerySet",
    "ValidationError",
    "DoesNotExist",
    "MultipleObjectsReturned",
    "MigrationError",
    "Operation",
    "configure",
    "database_path",
    "get_connection",
    "transaction",
    "execute",
    "query",
    "plan_migrations",
    "migrate",
    "models_registry",
    "get_model",
    "close_connections",
    "backend",
    "integrity_error",
]
