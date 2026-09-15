"""Exception types shared across Jongo."""
from __future__ import annotations


class JongoError(Exception):
    """Base class for framework errors."""


class CompileError(JongoError):
    """Python code that can't be turned into browser JavaScript."""

    def __init__(self, message, *, filename=None, lineno=None, source_line=None, hint=None):
        super().__init__(message)
        self.message = message
        self.filename = filename
        self.lineno = lineno
        self.source_line = source_line
        self.hint = hint

    def __str__(self):
        parts = [self.message]
        if self.filename:
            where = f"{self.filename}:{self.lineno}" if self.lineno else self.filename
            parts.append(f"  at {where}")
        if self.source_line:
            parts.append(f"    {self.source_line.strip()}")
        if self.hint:
            parts.append(f"  hint: {self.hint}")
        return "\n".join(parts)


class HTTPError(JongoError):
    """Raise from a page, route or server function to send an error response."""

    def __init__(self, status: int = 500, message: str | None = None):
        from http import HTTPStatus

        self.status = status
        try:
            default = HTTPStatus(status).phrase
        except ValueError:
            default = "Error"
        self.message = message or default
        super().__init__(f"{status} {self.message}")


class NotFound(HTTPError):
    def __init__(self, message: str | None = None):
        super().__init__(404, message)


class Forbidden(HTTPError):
    def __init__(self, message: str | None = None):
        super().__init__(403, message)
