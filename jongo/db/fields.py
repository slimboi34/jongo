"""Model field types, value conversion and validation."""

from __future__ import annotations

import copy
import json
import math
from datetime import date, datetime, time, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import Model


class _Nothing:
    """Sentinel for "no default given"."""

    def __repr__(self) -> str:
        return "NOTHING"

    def __bool__(self) -> bool:
        return False


NOTHING: Any = _Nothing()

ON_DELETE_ACTIONS = ("CASCADE", "SET NULL", "RESTRICT")
TRUE_STRINGS = {"1", "true", "t", "yes", "y", "on"}
FALSE_STRINGS = {"0", "false", "f", "no", "n", "off", ""}


class ValidationError(Exception):
    """Invalid data. ``errors`` maps field names (or ``"__all__"``) to messages."""

    def __init__(self, message_or_dict: str | dict[str, Any]):
        if isinstance(message_or_dict, dict):
            self.errors = {str(key): _join_messages(value) for key, value in message_or_dict.items()}
        else:
            self.errors = {"__all__": str(message_or_dict)}
        super().__init__(str(self))

    @property
    def message(self) -> str:
        """All messages joined into one sentence-friendly string."""
        return " ".join(self.errors.values())

    def __str__(self) -> str:
        return "; ".join(
            message if key == "__all__" else f"{key}: {message}" for key, message in self.errors.items()
        )


def _join_messages(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value)
    return str(value)


class Field:
    """Base class for model fields. Subclasses set ``sql_type`` and ``kind``."""

    sql_type = "TEXT"
    kind = "text"
    max_length: int | None = None

    def __init__(
        self,
        *,
        default: Any = NOTHING,
        null: bool = False,
        unique: bool = False,
        index: bool = False,
        choices: list | tuple | None = None,
        blank: bool = False,
        label: str | None = None,
        help: str | None = None,
        editable: bool = True,
    ):
        self.default = default
        self.null = null
        self.unique = unique
        self.index = index
        self.choices = _normalise_choices(choices)
        self.blank = blank
        self.help = help
        self.editable = editable
        self._label = label
        self.name: str | None = None
        self.model: type[Model] | None = None

    def bind(self, model: type[Model], name: str) -> None:
        """Attach the field to ``model`` under attribute ``name``."""
        self.model = model
        self.name = name

    @property
    def column(self) -> str | None:
        """Database column name (and the instance attribute holding the raw value)."""
        return self.name

    attname = column

    @property
    def label(self) -> str:
        if self._label is not None:
            return self._label
        return (self.name or "").replace("_", " ").capitalize()

    @label.setter
    def label(self, value: str | None) -> None:
        self._label = value

    @property
    def required(self) -> bool:
        """Whether a form must supply a non-empty value."""
        return not (self.null or self.blank or self.has_default())

    def has_default(self) -> bool:
        return self.default is not NOTHING

    def get_default(self) -> Any:
        if not self.has_default():
            return None
        if callable(self.default):
            return self.default()
        return copy.deepcopy(self.default)  # a non-callable list/dict default must not be shared across rows

    def choice_label(self, value: Any) -> str:
        """Human label for ``value`` according to ``choices`` (or ``str(value)``)."""
        for choice, label in self.choices or ():
            if choice == value:
                return label
        return "" if value is None else str(value)

    # -- conversion -------------------------------------------------------

    def to_db(self, value: Any) -> Any:
        """Python value -> value stored in SQLite."""
        return value

    def to_python(self, value: Any) -> Any:
        """Value read from SQLite -> Python value."""
        return value

    def parse(self, value: Any) -> Any:
        """Coerce raw input (e.g. an HTML form string) into a Python value."""
        return value

    def coerce(self, value: Any) -> Any:
        """Normalise a value assigned in Python code; used by ``Model.full_clean``."""
        return self.parse(value)

    def clean(self, value: Any) -> Any:
        """Parse and validate user input, returning the Python value."""
        value = self.parse(value)
        self.validate(value)
        return value

    def validate(self, value: Any) -> None:
        """Check null/blank/max_length/choices constraints."""
        if value is None:
            if not self.null:
                self.fail("This field is required.")
            return
        if value == "" and not self.blank:
            self.fail("This field is required.")
        if self.max_length is not None and isinstance(value, str) and len(value) > self.max_length:
            self.fail(f"Ensure this value has at most {self.max_length} characters (it has {len(value)}).")
        if self.choices is not None and not (value == "" and self.blank):
            if value not in [choice for choice, _ in self.choices]:
                self.fail(f"Select a valid choice; {value!r} is not one of the available choices.")

    def fail(self, message: str) -> None:
        raise ValidationError({self.name or "__all__": message})

    def __repr__(self) -> str:
        owner = f"{self.model.__name__}." if self.model is not None else ""
        return f"<{type(self).__name__}: {owner}{self.name}>"


def _normalise_choices(choices: Any) -> list[tuple[Any, str]] | None:
    if choices is None:
        return None
    pairs = []
    for choice in choices:
        if isinstance(choice, (list, tuple)) and len(choice) == 2:
            pairs.append((choice[0], str(choice[1])))
        else:
            pairs.append((choice, str(choice)))
    return pairs


def _is_blank_string(value: Any) -> bool:
    return isinstance(value, str) and not value.strip()


class Text(Field):
    sql_type = "TEXT"
    kind = "text"

    def __init__(self, max_length: int | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.max_length = max_length

    def get_default(self) -> Any:
        if not self.has_default() and not self.null:
            return ""
        return super().get_default()

    def to_db(self, value: Any) -> Any:
        return None if value is None else str(value)

    def parse(self, value: Any) -> Any:
        if value is None or value == "":
            return None if self.null else ""
        return value if isinstance(value, str) else str(value)


def _to_utc(dt: datetime) -> datetime:
    """A naive datetime is assumed to be UTC; an aware one is converted to UTC."""
    return dt.astimezone(timezone.utc) if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _to_int(value: Any) -> Any:
    """Best-effort int conversion for query values; unconvertible values pass through."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # Keep a non-integral float as-is so filter(v=5.9) doesn't match rows where v == 5.
        return int(value) if value.is_integer() else value
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


class Int(Field):
    sql_type = "INTEGER"
    kind = "int"

    def to_db(self, value: Any) -> Any:
        return _to_int(value)

    def parse(self, value: Any) -> Any:
        if value is None or _is_blank_string(value):
            return None
        if isinstance(value, int):
            return int(value)
        if isinstance(value, str):
            value = value.strip()
            try:
                return int(value)
            except ValueError:
                pass
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = math.nan
        if not math.isfinite(number) or not number.is_integer():
            self.fail("Enter a whole number.")
        return int(number)


class Float(Field):
    sql_type = "REAL"
    kind = "float"

    def to_db(self, value: Any) -> Any:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return value
        if not math.isfinite(number):  # SQLite stores NaN as NULL and can't round-trip inf reliably
            self.fail("Enter a finite number (not NaN or infinity).")
        return number

    def to_python(self, value: Any) -> Any:
        return None if value is None else float(value)

    def parse(self, value: Any) -> Any:
        if value is None or _is_blank_string(value):
            return None
        try:
            return float(value.strip() if isinstance(value, str) else value)
        except (TypeError, ValueError):
            self.fail("Enter a number.")


class Bool(Field):
    sql_type = "INTEGER"
    kind = "bool"

    @property
    def required(self) -> bool:
        return False

    def to_db(self, value: Any) -> Any:
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in TRUE_STRINGS:
                return 1
            if lowered in FALSE_STRINGS:
                return 0
            return value
        return None if value is None else int(bool(value))

    def to_python(self, value: Any) -> Any:
        return None if value is None else bool(value)

    def parse(self, value: Any) -> Any:
        if value is None or (value == "" and self.null):
            return None if self.null else False
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in TRUE_STRINGS:
                return True
            if lowered in FALSE_STRINGS:
                return False
        self.fail("Enter true or false.")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime_string(text: str) -> datetime:
    text = text.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


class DateTime(Field):
    sql_type = "TEXT"
    kind = "datetime"

    def __init__(self, auto_now: bool = False, auto_now_add: bool = False, **kwargs: Any):
        if auto_now or auto_now_add:
            kwargs["editable"] = False
        super().__init__(**kwargs)
        self.auto_now = auto_now
        self.auto_now_add = auto_now_add

    @property
    def required(self) -> bool:
        return super().required and not (self.auto_now or self.auto_now_add)

    def to_db(self, value: Any) -> Any:
        # Normalise to UTC before storing so the ISO text sorts and range-compares
        # chronologically (mixed offsets would otherwise sort lexicographically).
        if isinstance(value, datetime):
            return _to_utc(value).isoformat(timespec="microseconds")
        if isinstance(value, date):
            return datetime.combine(value, time(), tzinfo=timezone.utc).isoformat(timespec="microseconds")
        return value

    def to_python(self, value: Any) -> Any:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value
        try:
            return _parse_datetime_string(str(value))
        except ValueError:
            return value

    def parse(self, value: Any) -> Any:
        if value is None or _is_blank_string(value):
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime.combine(value, time())
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, timezone.utc)
        if isinstance(value, str):
            try:
                return _parse_datetime_string(value)
            except ValueError:
                pass
        self.fail("Enter a valid date and time.")


class Date(Field):
    sql_type = "TEXT"
    kind = "date"

    def to_db(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return value

    def to_python(self, value: Any) -> Any:
        if value is None or value == "":
            return None
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return value

    def parse(self, value: Any) -> Any:
        if value is None or _is_blank_string(value):
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            text = value.strip()
            try:
                if len(text) > 10:
                    return _parse_datetime_string(text).date()
                return date.fromisoformat(text)
            except ValueError:
                pass
        self.fail("Enter a valid date.")


class JSON(Field):
    sql_type = "TEXT"
    kind = "json"

    def to_db(self, value: Any) -> Any:
        return None if value is None else json.dumps(value)

    def to_python(self, value: Any) -> Any:
        if value is None:
            return None
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value

    def parse(self, value: Any) -> Any:
        if isinstance(value, str):
            if not value.strip():
                return None
            try:
                return json.loads(value)
            except ValueError:
                self.fail("Enter valid JSON.")
        return self.coerce(value)

    def coerce(self, value: Any) -> Any:
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            self.fail("Value is not JSON serialisable.")
        return value

    def validate(self, value: Any) -> None:
        if value is None and not self.null:
            self.fail("This field is required.")
        if value is not None and self.choices is not None:
            super().validate(value)


class ForeignKey(Field):
    """A reference to another model's ``id``; stored in the ``<name>_id`` column."""

    sql_type = "INTEGER"
    kind = "fk"

    def __init__(
        self,
        to: type[Model] | str,
        *,
        null: bool = False,
        on_delete: str = "CASCADE",
        related_name: str | None = None,
        index: bool = True,
        **kwargs: Any,
    ):
        super().__init__(null=null, index=index, **kwargs)
        action = on_delete.upper().replace("_", " ").strip()
        if action not in ON_DELETE_ACTIONS:
            raise ValueError(f"on_delete must be one of {', '.join(ON_DELETE_ACTIONS)}; got {on_delete!r}.")
        if action == "SET NULL" and not null:
            raise ValueError('on_delete="SET NULL" requires null=True.')
        self.to = to
        self.on_delete = action
        self.related_name = related_name

    @property
    def column(self) -> str | None:
        return None if self.name is None else f"{self.name}_id"

    attname = column

    @property
    def related_model(self) -> type[Model]:
        """The target model class, resolving string references lazily."""
        if not isinstance(self.to, str):
            return self.to
        if self.to == "self":
            return self.model
        from .models import get_model

        try:
            return get_model(self.to)
        except LookupError:
            owner = f"{self.model.__name__}.{self.name}" if self.model else "ForeignKey"
            raise LookupError(
                f'{owner} points to model "{self.to}", which is not defined. '
                f"Define (import) the {self.to} model before using this relation."
            ) from None

    @property
    def target_name(self) -> str:
        """Name of the target model without resolving it."""
        if self.to == "self":
            return self.model.__name__ if self.model else "self"
        return self.to if isinstance(self.to, str) else self.to.__name__

    @property
    def reverse_name(self) -> str:
        from .models import snake_case

        return self.related_name or f"{snake_case(self.model.__name__)}_set"

    def to_db(self, value: Any) -> Any:
        if hasattr(value, "_meta"):
            return value.pk
        return _to_int(value)

    def parse(self, value: Any) -> Any:
        if value is None or _is_blank_string(value):
            return None
        if hasattr(value, "_meta"):
            if value.pk is None:
                self.fail(f"Save the {value._meta.verbose_name} before assigning it.")
            return value.pk
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        self.fail("Select a valid choice.")

    def clean(self, value: Any) -> Any:
        value = super().clean(value)
        if value is not None and not self.related_model.filter(id=value).exists():
            self.fail(f"Select a valid choice; that {self.related_model._meta.verbose_name} does not exist.")
        return value

    @property
    def required(self) -> bool:
        return not (self.null or self.has_default())


FIELD_TYPES: dict[str, type[Field]] = {
    cls.kind: cls for cls in (Text, Int, Float, Bool, DateTime, Date, JSON, ForeignKey)
}

__all__ = [
    "NOTHING",
    "Field",
    "Text",
    "Int",
    "Float",
    "Bool",
    "DateTime",
    "Date",
    "JSON",
    "ForeignKey",
    "ValidationError",
]
