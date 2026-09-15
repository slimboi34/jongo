"""Lazy, chainable querysets, ``Q`` objects and SQL compilation."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any

from . import connection
from .fields import DateTime, Field, ForeignKey, ValidationError, utcnow

if TYPE_CHECKING:
    from .models import Model

LOOKUPS = {
    "exact", "iexact", "contains", "icontains", "startswith", "istartswith",
    "endswith", "iendswith", "gt", "gte", "lt", "lte", "in", "isnull", "ne",
}
COMPARISONS = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
MAX_GET_RESULTS = 21
REPR_ITEMS = 20


class DoesNotExist(Exception):
    """A ``get()`` query matched no rows. Each model has its own subclass."""


class MultipleObjectsReturned(Exception):
    """A ``get()`` query matched more than one row. Each model has its own subclass."""


def quote(identifier: str) -> str:
    """Quote an SQL identifier."""
    return '"' + identifier.replace('"', '""') + '"'


def escape_like(value: str) -> str:
    """Escape ``%``, ``_`` and ``\\`` for ``LIKE ... ESCAPE '\\'``."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def escape_glob(value: str) -> str:
    """Escape GLOB wildcards so the value matches literally."""
    return "".join(f"[{char}]" if char in "*?[" else char for char in value)


class Q:
    """A filter condition that can be combined with ``|``, ``&`` and ``~``."""

    AND = "AND"
    OR = "OR"

    def __init__(self, *conditions: Q, **lookups: Any):
        for condition in conditions:
            if not isinstance(condition, Q):
                raise TypeError(f"Positional filter arguments must be Q objects, not {type(condition).__name__}.")
        # One-shot iterators (e.g. generators passed to __in) are materialised so they can be compiled twice.
        items = [(key, list(value) if isinstance(value, Iterator) else value) for key, value in lookups.items()]
        self.children: list[Q | tuple[str, Any]] = [*conditions, *items]
        self.connector = Q.AND
        self.negated = False

    @classmethod
    def _make(cls, children: list, connector: str, negated: bool = False) -> Q:
        q = cls()
        q.children, q.connector, q.negated = list(children), connector, negated
        return q

    def _copy(self) -> Q:
        return Q._make(self.children, self.connector, self.negated)

    def _combine(self, other: Q, connector: str) -> Q:
        if not isinstance(other, Q):
            return NotImplemented
        if not other:
            return self._copy()
        if not self:
            return other._copy()
        return Q._make([self, other], connector)

    def __or__(self, other: Q) -> Q:
        return self._combine(other, Q.OR)

    def __and__(self, other: Q) -> Q:
        return self._combine(other, Q.AND)

    def __invert__(self) -> Q:
        return Q._make(self.children, self.connector, not self.negated)

    def __bool__(self) -> bool:
        return bool(self.children)

    def __repr__(self) -> str:
        inner = f" {self.connector} ".join(
            repr(child) if isinstance(child, Q) else f"{child[0]}={child[1]!r}" for child in self.children
        )
        return f"<Q: {'NOT ' if self.negated else ''}({inner})>"


def resolve_field(model: type[Model], name: str, context: str) -> Field:
    """Find a field by name, attname (``owner_id``), ``id`` or ``pk``."""
    try:
        return model._meta.field(name)
    except KeyError:
        choices = ", ".join(["pk", *(field.name for field in model._meta.all_fields)])
        raise ValueError(f'Cannot resolve "{name}" in "{context}" on {model.__name__}. Choices: {choices}.') from None


def compile_q(model: type[Model], q: Q) -> tuple[str, list] | None:
    """Compile a ``Q`` tree into ``(sql, params)``; ``None`` if it has no conditions."""
    parts: list[str] = []
    params: list = []
    for child in q.children:
        compiled = compile_q(model, child) if isinstance(child, Q) else compile_lookup(model, *child)
        if compiled is not None:
            parts.append(compiled[0])
            params.extend(compiled[1])
    if not parts:
        return None
    sql = parts[0] if len(parts) == 1 else f" {q.connector} ".join(f"({part})" for part in parts)
    if q.negated:
        # COALESCE makes NULL comparisons count as "no match" so they survive the NOT.
        sql = f"NOT COALESCE(({sql}), 0)"
    return sql, params


def compile_lookup(model: type[Model], key: str, value: Any) -> tuple[str, list]:
    """Compile one ``field__lookup=value`` condition, following ForeignKeys."""
    parts = key.split("__")
    lookup = parts.pop() if len(parts) > 1 and parts[-1] in LOOKUPS else "exact"
    field = resolve_field(model, parts[0], key)
    path = parts[1:]
    if not path:
        return _condition(field, lookup, value)
    if not isinstance(field, ForeignKey) or parts[0] != field.name:
        raise ValueError(f'Cannot follow "{parts[0]}" in "{key}" on {model.__name__}: it is not a ForeignKey.')
    if path in (["id"], ["pk"]):
        return _condition(field, lookup, value)
    related = field.related_model
    inner_sql, params = compile_lookup(related, "__".join([*path, lookup]), value)
    sql = f'{quote(field.column)} IN (SELECT "id" FROM {quote(related._meta.table)} WHERE {inner_sql})'
    return sql, params


def _condition(field: Field, lookup: str, value: Any) -> tuple[str, list]:
    column = quote(field.column)
    if lookup == "exact":
        if value is None:
            return f"{column} IS NULL", []
        return f"{column} = ?", [field.to_db(value)]
    if lookup == "ne":
        return f"{column} IS NOT ?", [None if value is None else field.to_db(value)]
    if lookup == "isnull":
        return (f"{column} IS NULL" if value else f"{column} IS NOT NULL"), []
    if lookup in COMPARISONS:
        return f"{column} {COMPARISONS[lookup]} ?", [field.to_db(value)]
    if lookup == "in":
        return _in_condition(field, column, value)
    if value is None and lookup == "iexact":
        return f"{column} IS NULL", []
    text = value if isinstance(value, str) else str(field.to_db(value))
    if lookup == "iexact":
        return f"{column} LIKE ? ESCAPE '\\'", [escape_like(text)]
    if lookup.startswith("i"):
        pattern = {"icontains": "%{}%", "istartswith": "{}%", "iendswith": "%{}"}[lookup]
        return f"{column} LIKE ? ESCAPE '\\'", [pattern.format(escape_like(text))]
    pattern = {"contains": "*{}*", "startswith": "{}*", "endswith": "*{}"}[lookup]
    return f"{column} GLOB ?", [pattern.format(escape_glob(text))]


def _in_condition(field: Field, column: str, value: Any) -> tuple[str, list]:
    if isinstance(value, QuerySet):
        sql, params = value._id_subquery()
        return f"{column} IN ({sql})", params
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise TypeError(f'The "in" lookup expects a list, tuple, set or QuerySet, not {type(value).__name__}.')
    values = [field.to_db(item) for item in value]
    present = [item for item in values if item is not None]
    conditions = []
    if present:
        conditions.append(f"{column} IN ({', '.join('?' * len(present))})")
    if len(present) != len(values):
        conditions.append(f"{column} IS NULL")
    if not conditions:
        return "0 = 1", []
    if len(conditions) == 1:
        return conditions[0], present
    return f"({' OR '.join(conditions)})", present


class QuerySet:
    """A lazy database query for ``model``. Every refining method returns a new QuerySet."""

    def __init__(self, model: type[Model]):
        self.model = model
        self._where: list[Q] = []
        self._ordering: list[str] | None = None
        self._limit: int | None = None
        self._offset = 0
        self._defaults: dict[str, Any] = {}
        self._cache: list[Model] | None = None

    def _clone(self, **changes: Any) -> QuerySet:
        clone = QuerySet(self.model)
        clone._where = list(self._where)
        clone._ordering = self._ordering
        clone._limit, clone._offset = self._limit, self._offset
        clone._defaults = dict(self._defaults)
        for name, value in changes.items():
            setattr(clone, f"_{name}", value)
        return clone

    # -- refining ---------------------------------------------------------

    def all(self) -> QuerySet:
        return self._clone()

    def filter(self, *conditions: Q, **lookups: Any) -> QuerySet:
        return self._refine("filter", Q(*conditions, **lookups))

    def exclude(self, *conditions: Q, **lookups: Any) -> QuerySet:
        return self._refine("exclude", ~Q(*conditions, **lookups))

    def _refine(self, action: str, q: Q) -> QuerySet:
        self._require_unsliced(action)
        if not q:
            return self._clone()
        compile_q(self.model, q)  # fail fast on unknown fields
        return self._clone(where=[*self._where, q])

    def order_by(self, *fields: str) -> QuerySet:
        """Order by field names; prefix ``-`` for descending, ``"?"`` for random."""
        self._require_unsliced("order_by")
        clone = self._clone(ordering=list(fields))
        clone._order_sql()
        return clone

    def search(self, term: str | None, fields: Iterable[str] | str) -> QuerySet:
        """Rows where any of ``fields`` contains ``term`` (case-insensitive)."""
        term = (term or "").strip()
        if not term:
            return self._clone()
        names = [fields] if isinstance(fields, str) else list(fields)
        condition = Q()
        for name in names:
            condition |= Q(**{f"{name}__icontains": term})
        return self.filter(condition)

    # -- fetching single objects -------------------------------------------

    def get(self, *conditions: Q, **lookups: Any) -> Model:
        qs = self.filter(*conditions, **lookups) if conditions or lookups else self
        results = qs._slice(0, MAX_GET_RESULTS)._fetch_all()
        name = self.model.__name__
        if not results:
            raise self.model.DoesNotExist(f"{name} matching query does not exist.")
        if len(results) > 1:
            count = "more than 20" if len(results) == MAX_GET_RESULTS else len(results)
            raise self.model.MultipleObjectsReturned(f"get() returned more than one {name} -- it returned {count}!")
        return results[0]

    def create(self, **values: Any) -> Model:
        obj = self.model(**{**self._defaults, **values})
        obj.save()
        return obj

    def get_or_create(self, defaults: dict[str, Any] | None = None, **lookups: Any) -> tuple[Model, bool]:
        """Fetch the object matching ``lookups`` or create it; returns ``(obj, created)``."""
        try:
            return self.get(**lookups), False
        except self.model.DoesNotExist:
            pass
        values = {key: value for key, value in lookups.items() if "__" not in key}
        values.update(defaults or {})
        try:
            with connection.transaction():
                return self.create(**values), True
        except (sqlite3.IntegrityError, ValidationError) as error:
            try:
                return self.get(**lookups), False
            except self.model.DoesNotExist:
                raise error from None

    def first(self) -> Model | None:
        qs = self if self._effective_ordering() or self._is_sliced() else self._clone(ordering=["id"])
        results = qs._slice(0, 1)._fetch_all()
        return results[0] if results else None

    def last(self) -> Model | None:
        if self._is_sliced():
            results = self._fetch_all()
            return results[-1] if results else None
        reversed_ordering = [
            item if item == "?" else (item[1:] if item.startswith("-") else f"-{item}")
            for item in (self._effective_ordering() or ["id"])
        ]
        results = self._clone(ordering=reversed_ordering)._slice(0, 1)._fetch_all()
        return results[0] if results else None

    # -- aggregates and bulk operations ------------------------------------

    def count(self) -> int:
        if self._cache is not None:
            return len(self._cache)
        if self._is_sliced():
            inner, params = self._compile('"id"')
            sql = f"SELECT COUNT(*) FROM ({inner})"
        else:
            where, params = self._where_sql()
            sql = f"SELECT COUNT(*) FROM {quote(self.model._meta.table)}{where}"
        return connection.fetch(sql, params)[0][0]

    def exists(self) -> bool:
        if self._cache is not None:
            return bool(self._cache)
        qs = self if self._is_sliced() else self._clone(ordering=[])
        sql, params = qs._slice(0, 1)._compile("1")
        return bool(connection.fetch(sql, params))

    def update(self, **values: Any) -> int:
        """Update matching rows in one statement; returns the number of rows changed."""
        meta = self.model._meta
        assignments: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for key, value in values.items():
            field = resolve_field(self.model, key, "update()")
            if field is meta.pk:
                raise ValueError("update() cannot change the id column.")
            try:
                value = field.coerce(value)
                field.validate(value)
            except ValidationError as exc:
                errors[field.name] = exc.message
                continue
            assignments[field.column] = field.to_db(value)
        if errors:
            raise ValidationError(errors)
        now = utcnow()
        for field in meta.fields:
            if isinstance(field, DateTime) and field.auto_now and field.column not in assignments:
                assignments[field.column] = field.to_db(now)
        if not assignments:
            return 0
        sets = ", ".join(f"{quote(column)} = ?" for column in assignments)
        where, params = self._write_where_sql()
        cursor = connection.execute(
            f"UPDATE {quote(meta.table)} SET {sets}{where}", [*assignments.values(), *params]
        )
        self._cache = None
        return cursor.rowcount

    def delete(self) -> int:
        """Delete matching rows; returns the number of rows deleted (cascades not counted)."""
        where, params = self._write_where_sql()
        cursor = connection.execute(f"DELETE FROM {quote(self.model._meta.table)}{where}", params)
        self._cache = None
        return cursor.rowcount

    def values(self, *fields: str) -> list[dict[str, Any]]:
        """Rows as dicts. Without arguments: ``id`` plus every column (FKs as ``<name>_id``)."""
        names, selected = self._selected_fields(fields)
        rows = self._fetch_columns(selected)
        return [
            {name: field.to_python(value) for name, field, value in zip(names, selected, row)} for row in rows
        ]

    def values_list(self, *fields: str, flat: bool = False) -> list:
        """Rows as tuples, or a flat list of values when ``flat=True`` and one field is given."""
        if flat and len(fields) != 1:
            raise TypeError("values_list(flat=True) requires exactly one field name.")
        _, selected = self._selected_fields(fields)
        rows = [tuple(field.to_python(value) for field, value in zip(selected, row))
                for row in self._fetch_columns(selected)]
        return [row[0] for row in rows] if flat else rows

    def _selected_fields(self, names: tuple[str, ...]) -> tuple[list[str], list[Field]]:
        meta = self.model._meta
        if not names:
            return [field.attname for field in meta.all_fields], list(meta.all_fields)
        return list(names), [resolve_field(self.model, name, "values()") for name in names]

    def _fetch_columns(self, fields: list[Field]) -> list[sqlite3.Row]:
        sql, params = self._compile(", ".join(quote(field.column) for field in fields))
        return connection.fetch(sql, params)

    # -- evaluation ---------------------------------------------------------

    def _fetch_all(self) -> list[Model]:
        if self._cache is None:
            rows = self._fetch_columns(self.model._meta.all_fields)
            self._cache = [self.model._from_row(row) for row in rows]
        return self._cache

    def __iter__(self) -> Iterator[Model]:
        return iter(self._fetch_all())

    def __len__(self) -> int:
        return len(self._fetch_all())

    def __bool__(self) -> bool:
        return bool(self._fetch_all())

    def __getitem__(self, key: int | slice) -> Model | QuerySet:
        if isinstance(key, slice):
            if key.step not in (None, 1):
                raise ValueError("QuerySet slicing does not support a step.")
            if (key.start or 0) < 0 or (key.stop is not None and key.stop < 0):
                raise ValueError("Negative indexing is not supported on QuerySets.")
            clone = self._slice(key.start or 0, key.stop)
            if self._cache is not None:
                clone._cache = self._cache[key]
            return clone
        if isinstance(key, int) and not isinstance(key, bool):
            if key < 0:
                raise ValueError("Negative indexing is not supported on QuerySets.")
            if self._cache is not None:
                return self._cache[key]
            results = self._slice(key, key + 1)._fetch_all()
            if not results:
                raise IndexError("QuerySet index out of range.")
            return results[0]
        raise TypeError(f"QuerySet indices must be integers or slices, not {type(key).__name__}.")

    def __repr__(self) -> str:
        items = list(self[: REPR_ITEMS + 1])
        shown = [repr(item) for item in items[:REPR_ITEMS]]
        if len(items) > REPR_ITEMS:
            shown.append("...(remaining elements truncated)...")
        return f"<QuerySet [{', '.join(shown)}]>"

    # -- SQL compilation ----------------------------------------------------

    def _is_sliced(self) -> bool:
        return self._limit is not None or self._offset > 0

    def _require_unsliced(self, action: str) -> None:
        if self._is_sliced():
            raise TypeError(f"Cannot {action}() a QuerySet after it has been sliced.")

    def _slice(self, start: int, stop: int | None) -> QuerySet:
        if self._limit is None:
            limit = None if stop is None else max(stop - start, 0)
        else:
            available = max(self._limit - start, 0)
            limit = available if stop is None else min(max(stop - start, 0), available)
        return self._clone(limit=limit, offset=self._offset + start)

    def _effective_ordering(self) -> list[str]:
        return list(self.model._meta.ordering if self._ordering is None else self._ordering)

    def _where_sql(self) -> tuple[str, list]:
        parts, params = [], []
        for q in self._where:
            compiled = compile_q(self.model, q)
            if compiled is not None:
                parts.append(f"({compiled[0]})")
                params.extend(compiled[1])
        return (f" WHERE {' AND '.join(parts)}" if parts else ""), params

    def _order_sql(self) -> str:
        terms = []
        for item in self._effective_ordering():
            if item == "?":
                terms.append("RANDOM()")
                continue
            name = item[1:] if item.startswith("-") else item
            field = resolve_field(self.model, name, "order_by()")
            terms.append(f"{quote(field.column)} {'DESC' if item.startswith('-') else 'ASC'}")
        return f" ORDER BY {', '.join(terms)}" if terms else ""

    def _compile(self, columns: str) -> tuple[str, list]:
        where, params = self._where_sql()
        sql = f"SELECT {columns} FROM {quote(self.model._meta.table)}{where}{self._order_sql()}"
        if self._is_sliced():
            sql += " LIMIT ? OFFSET ?"
            params = [*params, -1 if self._limit is None else self._limit, self._offset]
        return sql, params

    def _id_subquery(self) -> tuple[str, list]:
        qs = self if self._is_sliced() else self._clone(ordering=[])
        return qs._compile('"id"')

    def _write_where_sql(self) -> tuple[str, list]:
        if not self._is_sliced():
            return self._where_sql()
        sql, params = self._compile('"id"')
        return f' WHERE "id" IN ({sql})', params
