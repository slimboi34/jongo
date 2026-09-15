"""The ``Model`` base class, its metaclass, the model registry and relation descriptors."""

from __future__ import annotations

import copy
import re
from datetime import date, datetime
from typing import Any

from . import connection
from .fields import DateTime, Field, ForeignKey, Int, ValidationError, utcnow
from .query import DoesNotExist, MultipleObjectsReturned, Q, QuerySet, quote

models_registry: dict[str, type[Model]] = {}
_relations: list[ForeignKey] = []

META_OPTIONS = {"table", "ordering", "abstract", "verbose_name", "verbose_name_plural"}
RESERVED_NAMES = {"id", "pk", "objects", "save", "delete", "refresh", "full_clean", "clean", "to_dict"}


def snake_case(name: str) -> str:
    """``BlogPost`` -> ``blog_post``; ``HTTPRequest`` -> ``http_request``."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name).lower()


def get_model(name: str) -> type[Model]:
    """Look up a registered model by class name (case-insensitive) or table name."""
    if name in models_registry:
        return models_registry[name]
    lowered = name.lower()
    for model_name, model in models_registry.items():
        if model_name.lower() == lowered or model._meta.table.lower() == lowered:
            return model
    raise LookupError(f'No model named "{name}" is registered.')


class FieldDoesNotExist(KeyError):
    def __str__(self) -> str:
        return str(self.args[0])


class Options:
    """Model metadata, available as ``Model._meta``."""

    def __init__(self, model: type[Model], meta: dict[str, Any], fields: list[Field], parent: Options | None):
        self.model = model
        self.name = model.__name__
        self.abstract = bool(meta.get("abstract", False))
        self.table: str = meta.get("table") or snake_case(self.name)
        ordering = meta.get("ordering", parent.ordering if parent else [])
        self.ordering: list[str] = [ordering] if isinstance(ordering, str) else list(ordering or [])
        self.verbose_name: str = meta.get("verbose_name") or snake_case(self.name).replace("_", " ")
        self.verbose_name_plural: str = meta.get("verbose_name_plural") or f"{self.verbose_name}s"
        self.pk = Int(editable=False, null=True, label="ID")
        self.pk.bind(model, "id")
        self.fields = fields
        self.all_fields = [self.pk, *fields]
        self._lookup: dict[str, Field] = {"pk": self.pk}
        for field in self.all_fields:
            self._lookup[field.name] = field
            self._lookup.setdefault(field.attname, field)

    def field(self, name: str) -> Field:
        """Return a field by name, attname (``owner_id``), ``id`` or ``pk``."""
        try:
            return self._lookup[name]
        except KeyError:
            raise FieldDoesNotExist(f'{self.name} has no field named "{name}".') from None

    def __repr__(self) -> str:
        return f"<Options for {self.name}>"


def _meta_options(model_name: str, meta: type | None) -> dict[str, Any]:
    if meta is None:
        return {}
    options = {key: value for key, value in vars(meta).items() if not key.startswith("__")}
    unknown = set(options) - META_OPTIONS
    if unknown:
        raise TypeError(
            f"{model_name}.Meta has unknown option(s): {', '.join(sorted(unknown))}. "
            f"Valid options: {', '.join(sorted(META_OPTIONS))}."
        )
    return options


def _class_attribute(cls: type, name: str) -> Any:
    for klass in cls.__mro__:
        if name in vars(klass):
            return vars(klass)[name]
    return None


class ModelMeta(type):
    """Collects fields, builds ``_meta``, registers models and wires up relations."""

    def __new__(mcls, name: str, bases: tuple[type, ...], namespace: dict[str, Any], **kwargs: Any):
        if not any(isinstance(base, ModelMeta) for base in bases):
            return super().__new__(mcls, name, bases, namespace, **kwargs)

        meta = _meta_options(name, namespace.pop("Meta", None))
        declared = {key: value for key, value in namespace.items() if isinstance(value, Field)}
        for key in declared:
            del namespace[key]
        cls = super().__new__(mcls, name, bases, namespace, **kwargs)

        fields: dict[str, Field] = {}
        parent_meta = None
        for base in bases:
            base_meta = getattr(base, "_meta", None)
            if not isinstance(base, ModelMeta) or base_meta is None:
                continue
            if not base_meta.abstract:
                raise TypeError(
                    f"{name} cannot subclass the concrete model {base.__name__}. "
                    f"Mark {base.__name__} abstract (class Meta: abstract = True) to share fields."
                )
            parent_meta = parent_meta or base_meta
            for field in base_meta.fields:
                fields.setdefault(field.name, copy.copy(field))
        fields.update(declared)

        for field_name, field in fields.items():
            _check_field_name(name, field_name, field)
            field.bind(cls, field_name)
        cls._meta = Options(cls, meta, list(fields.values()), parent_meta)
        if cls._meta.abstract:
            return cls

        _check_model(cls)
        qualname = cls.__qualname__
        cls.DoesNotExist = type(
            "DoesNotExist", (DoesNotExist,), {"__module__": cls.__module__, "__qualname__": f"{qualname}.DoesNotExist"}
        )
        cls.MultipleObjectsReturned = type(
            "MultipleObjectsReturned",
            (MultipleObjectsReturned,),
            {"__module__": cls.__module__, "__qualname__": f"{qualname}.MultipleObjectsReturned"},
        )
        for field in cls._meta.fields:
            if isinstance(field, ForeignKey):
                setattr(cls, field.name, ForwardRelation(field))
                setattr(cls, field.attname, RelationId(field))
        _register(cls)
        return cls


def _check_field_name(model_name: str, name: str, field: Field) -> None:
    if name.startswith("_") or "__" in name or name in RESERVED_NAMES:
        raise TypeError(f'{model_name}.{name}: "{name}" is reserved and cannot be used as a field name.')
    if isinstance(field, ForeignKey) and _class_attribute(Model, name) is not None:
        raise TypeError(f'{model_name}.{name}: a ForeignKey cannot be named "{name}" (it clashes with Model.{name}).')


def _check_model(cls: type[Model]) -> None:
    meta = cls._meta
    names = {field.name for field in meta.fields}
    for field in meta.fields:
        if field.attname != field.name and field.attname in names:
            raise TypeError(f'{meta.name}.{field.name}: its column "{field.attname}" clashes with another field.')
    for item in meta.ordering:
        name = item[1:] if item.startswith("-") else item
        if item != "?" and name not in meta._lookup:
            raise TypeError(f'{meta.name}.Meta.ordering refers to unknown field "{name}".')


def _register(model: type[Model]) -> None:
    """Register ``model`` (replacing older models with the same name or table)."""
    global _relations
    table = model._meta.table.lower()
    for name, existing in list(models_registry.items()):
        if existing._meta.table.lower() == table:
            del models_registry[name]
    models_registry[model.__name__] = model

    _relations = [fk for fk in _relations if models_registry.get(fk.model.__name__) is fk.model]
    _relations.extend(field for field in model._meta.fields if isinstance(field, ForeignKey))
    for fk in _relations:
        if fk.model is model or fk.target_name == model.__name__ or fk.to is model:
            try:
                target = fk.related_model
            except LookupError:
                continue
            _install_reverse(target, fk)


def _install_reverse(target: type[Model], fk: ForeignKey) -> None:
    name = fk.reverse_name
    existing = _class_attribute(target, name)
    if name in target._meta._lookup or (existing is not None and not isinstance(existing, ReverseRelation)):
        raise TypeError(
            f'Reverse accessor "{target.__name__}.{name}" for {fk.model.__name__}.{fk.name} clashes with an '
            f"existing attribute; pass related_name= to the ForeignKey."
        )
    setattr(target, name, ReverseRelation(fk))


class ForwardRelation:
    """``todo.owner``: lazily loads and caches the related instance."""

    def __init__(self, field: ForeignKey):
        self.field = field

    def __get__(self, instance: Model | None, owner: type | None = None) -> Any:
        if instance is None:
            return self
        field = self.field
        related_id = instance.__dict__.get(field.attname)
        cache = instance.__dict__.setdefault("_related", {})
        cached = cache.get(field.name)
        if cached is not None and (related_id is None or cached.pk == related_id):
            return cached
        if related_id is None:
            return None
        obj = field.related_model.objects.get(id=related_id)
        cache[field.name] = obj
        return obj

    def __set__(self, instance: Model, value: Any) -> None:
        field = self.field
        cache = instance.__dict__.setdefault("_related", {})
        if isinstance(value, Model):
            related = field.related_model
            if not isinstance(value, related):
                raise TypeError(
                    f"{field.model.__name__}.{field.name} must be a {related.__name__}, not {type(value).__name__}."
                )
            instance.__dict__[field.attname] = value.pk
            cache[field.name] = value
        else:
            instance.__dict__[field.attname] = value
            cache.pop(field.name, None)


class RelationId:
    """``todo.owner_id``: the raw id; changing it forgets the cached related object."""

    def __init__(self, field: ForeignKey):
        self.field = field

    def __get__(self, instance: Model | None, owner: type | None = None) -> Any:
        if instance is None:
            return self
        return instance.__dict__.get(self.field.attname)

    def __set__(self, instance: Model, value: Any) -> None:
        if instance.__dict__.get(self.field.attname) != value:
            instance.__dict__.setdefault("_related", {}).pop(self.field.name, None)
        instance.__dict__[self.field.attname] = value


class ReverseRelation:
    """``user.todos``: a QuerySet of the objects pointing at this instance."""

    def __init__(self, field: ForeignKey):
        self.field = field

    def __get__(self, instance: Model | None, owner: type | None = None) -> Any:
        if instance is None:
            return self
        if instance.pk is None:
            raise ValueError(f"Save this {type(instance).__name__} before using .{self.field.reverse_name}.")
        qs = self.field.model.objects.filter(**{self.field.attname: instance.pk})
        qs._defaults = {self.field.attname: instance.pk}
        return qs

    def __set__(self, instance: Model, value: Any) -> None:
        raise AttributeError(
            f"Cannot assign to reverse relation {type(instance).__name__}.{self.field.reverse_name}; "
            f"set {self.field.model.__name__}.{self.field.name} on each object instead."
        )


class _Objects:
    """``Todo.objects``: a fresh QuerySet for the model."""

    def __get__(self, instance: Model | None, owner: type[Model]) -> QuerySet:
        if instance is not None:
            raise AttributeError(f"objects is only available on the model class, e.g. {owner.__name__}.objects.")
        return owner._queryset()


class Model(metaclass=ModelMeta):
    """Base class for database models."""

    _meta: Options = None  # type: ignore[assignment]
    DoesNotExist = DoesNotExist
    MultipleObjectsReturned = MultipleObjectsReturned
    objects = _Objects()

    def __init__(self, **values: Any):
        meta = type(self)._meta
        if meta is None or meta.abstract:
            raise TypeError(f"{type(self).__name__} is abstract and cannot be instantiated.")
        self.__dict__["_related"] = {}
        pk = values.pop("id", None)
        self.pk = values.pop("pk", pk)
        for field in meta.fields:
            if field.attname != field.name and field.attname in values:
                raw_id = values.pop(field.attname)
                if field.name not in values:
                    setattr(self, field.attname, raw_id)
                    continue
            if field.name in values:
                setattr(self, field.name, values.pop(field.name))
            else:
                setattr(self, field.name, field.get_default())
        if values:
            raise TypeError(f"{type(self).__name__}() got unexpected field(s): {', '.join(sorted(values))}.")

    @classmethod
    def _from_row(cls, row: Any) -> Model:
        obj = cls.__new__(cls)
        data = obj.__dict__
        data["_related"] = {}
        for field, value in zip(cls._meta.all_fields, row):
            data[field.attname] = field.to_python(value)
        return obj

    @property
    def pk(self) -> int | None:
        return self.__dict__.get("id")

    @pk.setter
    def pk(self, value: int | None) -> None:
        self.__dict__["id"] = value

    # -- persistence ----------------------------------------------------------

    def save(self) -> Model:
        """Validate, then INSERT (no id or id not in the table) or UPDATE."""
        meta = self._meta
        self._sync_relations()
        now = utcnow()
        for field in meta.fields:
            if isinstance(field, DateTime):
                if field.auto_now or (field.auto_now_add and self.__dict__.get(field.attname) is None):
                    self.__dict__[field.attname] = now
        self.full_clean()

        table = quote(meta.table)
        columns = [quote(field.column) for field in meta.fields]
        values = [field.to_db(self.__dict__[field.attname]) for field in meta.fields]
        with connection.locked() as conn:
            if self.pk is not None:
                if columns:
                    sets = ", ".join(f"{column} = ?" for column in columns)
                    updated = conn.execute(f'UPDATE {table} SET {sets} WHERE "id" = ?', [*values, self.pk]).rowcount
                else:
                    updated = conn.execute(f'SELECT 1 FROM {table} WHERE "id" = ?', [self.pk]).fetchone() is not None
                if updated:
                    return self
                columns, values = ['"id"', *columns], [self.pk, *values]
            if columns:
                placeholders = ", ".join("?" * len(columns))
                sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
            else:
                sql = f"INSERT INTO {table} DEFAULT VALUES"
            cursor = conn.execute(sql, values)
            if self.pk is None:
                self.pk = cursor.lastrowid
        return self

    def _sync_relations(self) -> None:
        for name, related in self.__dict__.get("_related", {}).items():
            field = self._meta.field(name)
            if self.__dict__.get(field.attname) is None:
                if related.pk is None:
                    raise ValueError(
                        f"Cannot save {type(self).__name__}: the related {type(related).__name__} "
                        f"assigned to .{name} has not been saved."
                    )
                self.__dict__[field.attname] = related.pk

    def delete(self) -> int:
        """Delete this row; the instance's id becomes None. Returns rows deleted."""
        if self.pk is None:
            raise ValueError(f"{type(self).__name__} cannot be deleted because it has no id.")
        cursor = connection.execute(f'DELETE FROM {quote(self._meta.table)} WHERE "id" = ?', [self.pk])
        self.pk = None
        return cursor.rowcount

    def refresh(self) -> Model:
        """Reload every field from the database."""
        if self.pk is None:
            raise ValueError(f"{type(self).__name__} cannot be refreshed because it has no id.")
        fresh = type(self).objects.get(id=self.pk)
        for field in self._meta.all_fields:
            self.__dict__[field.attname] = fresh.__dict__[field.attname]
        self.__dict__["_related"] = {}
        return self

    # -- validation -------------------------------------------------------------

    def clean(self) -> None:
        """Hook for model-wide validation; raise ValidationError to reject the object."""

    def full_clean(self) -> None:
        """Coerce and validate every field, check uniqueness, run ``clean()``."""
        meta = self._meta
        errors: dict[str, str] = {}
        for field in meta.fields:
            try:
                value = field.coerce(self.__dict__.get(field.attname))
                field.validate(value)
            except ValidationError as exc:
                errors[field.name] = exc.message
            else:
                self.__dict__[field.attname] = value

        for field in meta.fields:
            value = self.__dict__.get(field.attname)
            if not field.unique or field.name in errors or value is None:
                continue
            duplicates = type(self).objects.filter(**{field.attname: value})
            if self.pk is not None:
                duplicates = duplicates.exclude(id=self.pk)
            if duplicates.exists():
                errors[field.name] = f"{_capfirst(meta.verbose_name)} with this {field.label.lower()} already exists."

        try:
            self.clean()
        except ValidationError as exc:
            for key, message in exc.errors.items():
                errors[key] = f"{errors[key]} {message}" if key in errors else message
        if errors:
            raise ValidationError(errors)

    # -- representation ----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict: ``id`` plus every field (ForeignKeys as ``<name>_id``)."""
        data: dict[str, Any] = {"id": self.pk}
        for field in self._meta.fields:
            value = self.__dict__.get(field.attname)
            if isinstance(value, (datetime, date)):
                value = value.isoformat()
            data[field.attname] = value
        return data

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Model):
            return NotImplemented
        if type(self) is not type(other) or self.pk is None:
            return self is other
        return self.pk == other.pk

    def __hash__(self) -> int:
        if self.pk is None:
            raise TypeError(f"Unsaved {type(self).__name__} instances are unhashable.")
        return hash((type(self), self.pk))

    def __str__(self) -> str:
        name = type(self).__name__
        return f"{name} #{self.pk}" if self.pk is not None else f"{name} (unsaved)"

    def __repr__(self) -> str:
        return f"<{type(self).__name__}: {self}>"

    # -- queryset shortcuts ---------------------------------------------------------

    @classmethod
    def _queryset(cls) -> QuerySet:
        if cls._meta is None or cls._meta.abstract:
            raise TypeError(f"{cls.__name__} is abstract and has no table.")
        return QuerySet(cls)

    @classmethod
    def all(cls) -> QuerySet:
        return cls._queryset()

    @classmethod
    def filter(cls, *conditions: Q, **lookups: Any) -> QuerySet:
        return cls._queryset().filter(*conditions, **lookups)

    @classmethod
    def exclude(cls, *conditions: Q, **lookups: Any) -> QuerySet:
        return cls._queryset().exclude(*conditions, **lookups)

    @classmethod
    def order_by(cls, *fields: str) -> QuerySet:
        return cls._queryset().order_by(*fields)

    @classmethod
    def search(cls, term: str | None, fields: Any) -> QuerySet:
        return cls._queryset().search(term, fields)

    @classmethod
    def get(cls, *conditions: Q, **lookups: Any) -> Model:
        return cls._queryset().get(*conditions, **lookups)

    @classmethod
    def create(cls, **values: Any) -> Model:
        return cls._queryset().create(**values)

    @classmethod
    def get_or_create(cls, defaults: dict[str, Any] | None = None, **lookups: Any) -> tuple[Model, bool]:
        return cls._queryset().get_or_create(defaults=defaults, **lookups)

    @classmethod
    def count(cls) -> int:
        return cls._queryset().count()

    @classmethod
    def first(cls) -> Model | None:
        return cls._queryset().first()

    @classmethod
    def last(cls) -> Model | None:
        return cls._queryset().last()


def _capfirst(text: str) -> str:
    return text[:1].upper() + text[1:]
