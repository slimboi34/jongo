"""Auto-migrations: diff model definitions against the live SQLite schema.

There are no migration files. ``plan_migrations()`` introspects the database and
returns the operations needed to make it match the models; ``migrate()`` applies them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Iterable

from . import connection
from .fields import DateTime, Field, ForeignKey, utcnow
from .models import Model, models_registry
from .query import quote

ZERO_VALUES = {"text": "''", "int": "0", "float": "0.0", "bool": "0", "datetime": "''", "date": "''", "json": "'null'"}


class MigrationError(Exception):
    """The models cannot be migrated as requested."""


@dataclass
class Column:
    name: str
    type: str
    notnull: bool = False
    pk: bool = False
    references: tuple[str, str | None, str] | None = None  # (table, column, on_delete)
    default: str | None = None  # SQL literal

    def sql(self) -> str:
        parts = [quote(self.name)]
        if self.type:
            parts.append(self.type)
        if self.pk:
            parts.append("PRIMARY KEY AUTOINCREMENT")
        elif self.notnull:
            parts.append("NOT NULL")
        if self.default is not None:
            parts.append(f"DEFAULT {self.default}")
        if self.references:
            table, column, on_delete = self.references
            target = f"{quote(table)} ({quote(column)})" if column else quote(table)
            parts.append(f"REFERENCES {target} ON DELETE {on_delete}")
        return " ".join(parts)


@dataclass
class Index:
    name: str
    columns: tuple[str | None, ...]
    unique: bool
    origin: str = "c"
    partial: bool = False
    sql: str | None = None

    def create_sql(self, table: str) -> str:
        unique = "UNIQUE " if self.unique else ""
        columns = ", ".join(quote(column or "") for column in self.columns)
        return f"CREATE {unique}INDEX {quote(self.name)} ON {quote(table)} ({columns})"

    def is_on(self, column: str) -> bool:
        return len(self.columns) == 1 and (self.columns[0] or "").lower() == column.lower()


@dataclass
class TableSchema:
    name: str
    columns: dict[str, Column]  # keyed by lower-cased column name, in table order
    indexes: list[Index]
    has_rows: bool


@dataclass
class Operation:
    """One schema change. ``sql`` holds the statements that implement it."""

    kind: str
    table: str
    sql: list[str]
    description: str
    destructive: bool = False
    rebuild: bool = False
    column: str | None = None
    reasons: list[str] = dataclass_field(default_factory=list)

    def describe(self) -> str:
        return self.description

    def __repr__(self) -> str:
        return f"<Operation {self.kind}: {self.description}>"


# -- desired schema ----------------------------------------------------------------


def model_columns(model: type[Model]) -> list[Column]:
    columns = [Column("id", "INTEGER", pk=True)]
    for field in model._meta.fields:
        references = None
        if isinstance(field, ForeignKey):
            try:
                target = field.related_model
            except LookupError as exc:
                raise MigrationError(str(exc)) from None
            references = (target._meta.table, "id", field.on_delete)
        columns.append(Column(field.column, field.sql_type, notnull=not field.null, references=references))
    return columns


def model_indexes(model: type[Model]) -> list[Index]:
    table = model._meta.table
    indexes = []
    for field in model._meta.fields:
        if field.unique:
            indexes.append(Index(f"ux_{table}_{field.column}", (field.column,), True))
        elif field.index:
            indexes.append(Index(f"ix_{table}_{field.column}", (field.column,), False))
    return indexes


def _is_managed(table: str, index: Index) -> bool:
    if index.origin != "c" or len(index.columns) != 1 or index.columns[0] is None:
        return False
    column = index.columns[0]
    return index.name.lower() in (f"ix_{table}_{column}".lower(), f"ux_{table}_{column}".lower())


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def fill_literal(field: Field) -> str | None:
    """SQL literal used to fill existing rows when a column becomes NOT NULL."""
    if field.has_default():
        value = field.to_db(field.get_default())
        if value is not None:
            return sql_literal(value)
    if isinstance(field, DateTime) and (field.auto_now or field.auto_now_add):
        return sql_literal(field.to_db(utcnow()))
    return ZERO_VALUES.get(field.kind)


# -- live schema ---------------------------------------------------------------------


def live_tables(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'")
    return {row[0].lower(): row[0] for row in rows}


def introspect(conn: sqlite3.Connection, table: str) -> TableSchema:
    references: dict[str, tuple[str, str | None, str]] = {}
    groups: dict[int, list[sqlite3.Row]] = {}
    for row in conn.execute(f"PRAGMA foreign_key_list({quote(table)})"):
        groups.setdefault(row["id"], []).append(row)
    for rows in groups.values():
        if len(rows) == 1:
            row = rows[0]
            references[row["from"].lower()] = (row["table"], row["to"], row["on_delete"].upper())

    columns = {}
    for row in conn.execute(f"PRAGMA table_info({quote(table)})"):
        name = row["name"]
        columns[name.lower()] = Column(
            name, (row["type"] or "").upper(), bool(row["notnull"]), bool(row["pk"]),
            references.get(name.lower()), row["dflt_value"],
        )

    indexes = []
    for row in conn.execute(f"PRAGMA index_list({quote(table)})").fetchall():
        info = conn.execute(f"PRAGMA index_info({quote(row['name'])})").fetchall()
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (row["name"],)).fetchone()
        indexes.append(Index(
            row["name"], tuple(item["name"] for item in info), bool(row["unique"]),
            row["origin"], bool(row["partial"]), sql[0] if sql else None,
        ))
    has_rows = conn.execute(f"SELECT 1 FROM {quote(table)} LIMIT 1").fetchone() is not None
    return TableSchema(table, columns, indexes, has_rows)


def _affinity(declared: str) -> str:
    declared = declared.upper()
    if "INT" in declared:
        return "INTEGER"
    if any(token in declared for token in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if not declared or "BLOB" in declared:
        return "BLOB"
    if any(token in declared for token in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _same_reference(old: tuple | None, new: tuple | None) -> bool:
    if old is None or new is None:
        return old is new
    return old[0].lower() == new[0].lower() and old[1] in (None, "id") and old[2].upper() == new[2].upper()


# -- planning --------------------------------------------------------------------------


def plan_migrations(models: Iterable[type[Model]] | None = None) -> list[Operation]:
    """Return the operations needed to bring the database in line with ``models``."""
    targets = _select_models(models)
    operations: list[Operation] = []
    with connection.locked() as conn:
        tables = live_tables(conn)
        for model in targets:
            live_name = tables.get(model._meta.table.lower())
            if live_name is None:
                operations.append(_create_table(model))
            else:
                operations.extend(_TablePlanner(model, introspect(conn, live_name), tables).plan())
    return operations


def _select_models(models: Iterable[type[Model]] | None) -> list[type[Model]]:
    candidates = list(models_registry.values()) if models is None else list(models)
    selected: dict[str, type[Model]] = {}
    for model in candidates:
        if not (isinstance(model, type) and issubclass(model, Model)) or model._meta is None:
            raise TypeError(f"Expected a Model class, got {model!r}.")
        if model._meta.abstract:
            continue
        key = model._meta.table.lower()
        if key in selected and selected[key] is not model:
            raise MigrationError(
                f'{selected[key].__name__} and {model.__name__} both use table "{model._meta.table}".'
            )
        selected[key] = model
    return list(selected.values())


def _create_table(model: type[Model]) -> Operation:
    table = model._meta.table
    columns = ", ".join(column.sql() for column in model_columns(model))
    sql = [f"CREATE TABLE {quote(table)} ({columns})"]
    sql.extend(index.create_sql(table) for index in model_indexes(model))
    return Operation("create_table", table, sql, f'Create table "{table}"')


class _TablePlanner:
    """Plans the changes for one model whose table already exists."""

    def __init__(self, model: type[Model], live: TableSchema, tables: dict[str, str]):
        self.model = model
        self.live = live
        self.tables = tables
        self.table = model._meta.table
        self.want = model_columns(model)
        self.wanted_names = {column.name.lower() for column in self.want}
        self.fields = {field.column.lower(): field for field in model._meta.fields}
        self.extras = [column for key, column in live.columns.items() if key not in self.wanted_names]
        self.added = [column for column in self.want if column.name.lower() not in live.columns]

    def plan(self) -> list[Operation]:
        reasons = self._rebuild_reasons()
        if reasons:
            operations = [self._rebuild(reasons)]
        else:
            operations = [self._add_column(column) for column in self.added]
            operations.extend(self._index_operations())
        operations.extend(self._drop_operations())
        return operations

    def _label(self, column: str) -> str:
        return f'"{self.table}"."{column}"'

    def _rebuild_reasons(self) -> list[str]:
        reasons = []
        for new in self.want:
            old = self.live.columns.get(new.name.lower())
            if old is None:
                continue
            label = self._label(new.name)
            if new.pk and not old.pk:
                reasons.append(f"make {label} the primary key")
            elif _affinity(old.type) != _affinity(new.type):
                reasons.append(f"change the type of {label} from {old.type or 'untyped'} to {new.type}")
            if not new.pk and old.notnull != new.notnull:
                reasons.append(f"make {label} {'NOT NULL' if new.notnull else 'nullable'}")
            if not _same_reference(old.references, new.references):
                reasons.append(f"change the foreign key on {label}")
        for column in self.added:
            if column.pk:
                reasons.append(f"add primary key {self._label('id')}")
            elif column.notnull and column.references:
                if self.live.has_rows and fill_literal(self.fields[column.name.lower()]) is None:
                    raise MigrationError(
                        f"Cannot add the required foreign key {self._label(column.name)} to a table that already "
                        f"has rows. Declare it with null=True (or give it a default) and fill it in afterwards."
                    )
                reasons.append(f"add column {self._label(column.name)}")
        for index in self.live.indexes:
            if index.origin == "u" and len(index.columns) == 1:
                field = self.fields.get((index.columns[0] or "").lower())
                if field is not None and not field.unique:
                    reasons.append(f"drop the UNIQUE constraint on {self._label(field.column)}")
        return reasons

    def _add_column(self, column: Column) -> Operation:
        field = self.fields[column.name.lower()]
        definition = Column(column.name, column.type, column.notnull, references=column.references)
        if column.notnull:
            definition.default = fill_literal(field)
        sql = f"ALTER TABLE {quote(self.table)} ADD COLUMN {definition.sql()}"
        return Operation("add_column", self.table, [sql], f"Add column {self._label(column.name)}", column=column.name)

    def _index_operations(self) -> list[Operation]:
        desired = {index.columns[0].lower(): index for index in model_indexes(self.model)}
        operations, remaining = [], []
        for index in self.live.indexes:
            column = (index.columns[0] or "").lower() if len(index.columns) == 1 else None
            wanted = desired.get(column) if column else None
            stale = wanted is None or wanted.unique != index.unique
            if column in self.fields and _is_managed(self.table, index) and stale:
                operations.append(Operation(
                    "drop_index", self.table, [f"DROP INDEX {quote(index.name)}"], f'Drop index "{index.name}"'
                ))
            else:
                remaining.append(index)
        for column, index in desired.items():
            if any(live.is_on(column) and live.unique == index.unique and not live.partial for live in remaining):
                continue
            kind = "unique index" if index.unique else "index"
            operations.append(Operation(
                "create_index", self.table, [index.create_sql(self.table)],
                f'Create {kind} "{index.name}" on {self._label(index.columns[0])}',
            ))
        return operations

    def _rebuild(self, reasons: list[str]) -> Operation:
        new_columns = [*self.want, *(self._retained(column) for column in self.extras)]
        targets, sources = [], []
        for column in new_columns:
            old = self.live.columns.get(column.name.lower())
            field = self.fields.get(column.name.lower())
            literal = fill_literal(field) if field is not None and column.notnull else None
            if old is None:
                if literal is not None:
                    targets.append(column.name)
                    sources.append(literal)
                continue
            source = quote(old.name)
            if literal is not None and not old.notnull:
                source = f"COALESCE({source}, {literal})"
            targets.append(column.name)
            sources.append(source)
        sql = self._rebuild_sql(new_columns, targets, sources)
        description = f'Rebuild table "{self.table}" to {"; ".join(reasons)}'
        return Operation("rebuild_table", self.table, sql, description, rebuild=True, reasons=reasons)

    @staticmethod
    def _retained(column: Column) -> Column:
        return Column(column.name, column.type, column.notnull, False, column.references, column.default)

    def _rebuild_sql(self, new_columns: list[Column], targets: list[str], sources: list[str]) -> list[str]:
        temp = f"new_{self.table}"
        suffix = 1
        while temp.lower() in self.tables:
            temp, suffix = f"new_{self.table}_{suffix}", suffix + 1
        sql = [f"CREATE TABLE {quote(temp)} ({', '.join(column.sql() for column in new_columns)})"]
        if targets:
            sql.append(
                f"INSERT INTO {quote(temp)} ({', '.join(map(quote, targets))}) "
                f"SELECT {', '.join(sources)} FROM {quote(self.table)}"
            )
        sql.append(f"DROP TABLE {quote(self.table)}")
        sql.append(f"ALTER TABLE {quote(temp)} RENAME TO {quote(self.table)}")
        sql.extend(self._indexes_after_rebuild({column.name.lower() for column in new_columns}))
        return sql

    def _indexes_after_rebuild(self, column_names: set[str]) -> list[str]:
        desired = model_indexes(self.model)
        desired_names = {index.name.lower() for index in desired}
        sql = [index.create_sql(self.table) for index in desired]
        for index in self.live.indexes:
            if index.origin != "c" or not index.sql or index.name.lower() in desired_names:
                continue
            named = [column for column in index.columns if column is not None]
            if not all(column.lower() in column_names for column in named):
                continue
            if _is_managed(self.table, index) and (index.columns[0] or "").lower() in self.fields:
                continue
            sql.append(index.sql)
        return sql

    def _drop_operations(self) -> list[Operation]:
        operations = []
        remaining = list(self.extras)
        for column in self.extras:
            remaining.remove(column)
            label = self._label(column.name)
            if self._can_alter_drop(column):
                sql = [f"DROP INDEX {quote(index.name)}" for index in self.live.indexes
                       if index.is_on(column.name) and _is_managed(self.table, index)]
                sql.append(f"ALTER TABLE {quote(self.table)} DROP COLUMN {quote(column.name)}")
                operations.append(Operation(
                    "drop_column", self.table, sql, f"Drop column {label}", destructive=True, column=column.name
                ))
                continue
            # Planned against the schema as it will be once earlier operations have run.
            new_columns = [*self.want, *(self._retained(extra) for extra in remaining)]
            names = [c.name for c in new_columns]
            sql = self._rebuild_sql(new_columns, names, [quote(name) for name in names])
            operations.append(Operation(
                "drop_column", self.table, sql, f"Drop column {label}",
                destructive=True, rebuild=True, column=column.name,
            ))
        return operations

    def _can_alter_drop(self, column: Column) -> bool:
        if sqlite3.sqlite_version_info < (3, 35, 0) or column.pk or column.references:
            return False
        for index in self.live.indexes:
            touches = any((name or "").lower() == column.name.lower() for name in index.columns)
            if touches and not (index.is_on(column.name) and _is_managed(self.table, index)):
                return False
        return True


# -- applying ------------------------------------------------------------------------------


def migrate(
    models: Iterable[type[Model]] | None = None,
    *,
    allow_destructive: bool = False,
    dry_run: bool = False,
) -> list[tuple[Operation, bool]]:
    """Apply planned operations; returns ``(operation, applied)`` pairs.

    Destructive operations (dropping columns) are skipped unless ``allow_destructive``.
    """
    operations = plan_migrations(models)
    selected = [op for op in operations if allow_destructive or not op.destructive]
    if dry_run or not selected:
        return [(op, False) for op in operations]

    rebuilt = list(dict.fromkeys(op.table for op in selected if op.rebuild))
    with connection.locked() as conn:
        if rebuilt:
            if conn.in_transaction:
                raise MigrationError("migrate() cannot rebuild tables inside an open transaction.")
            conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with connection.transaction():
                for op in selected:
                    _apply(conn, op)
                for table in rebuilt:
                    problems = conn.execute(f"PRAGMA foreign_key_check({quote(table)})").fetchall()
                    if problems:
                        raise MigrationError(
                            f'Rebuilding "{table}" would leave {len(problems)} row(s) with invalid foreign keys.'
                        )
        finally:
            if rebuilt:
                conn.execute("PRAGMA foreign_keys=ON")
    applied = {id(op) for op in selected}
    return [(op, id(op) in applied) for op in operations]


def _apply(conn: sqlite3.Connection, op: Operation) -> None:
    for statement in op.sql:
        try:
            conn.execute(statement)
        except sqlite3.DatabaseError as exc:
            raise MigrationError(f"{op.describe()} failed: {exc}") from exc
