"""Rate limits: a cap on how often one caller may hit a server function, a route or the login.

    @server(rate_limit="5/minute")
    def send_invite(request, email: str) -> dict: ...

    @app.route("/webhook", methods=("POST",), csrf=False, rate_limit="30/minute")
    def webhook(request): ...

The caller is the signed-in user when there is one and the client's address otherwise. A
caller over the limit gets ``429 Too Many Requests`` with a ``Retry-After`` header and the
error code ``J213``; the function does not run.

``jongo.auth.authenticate()`` uses the same machinery to throttle password guessing, with
no configuration (see :data:`jongo.auth.LOGIN_ATTEMPTS`).

Counters live in the memory of the process that served the request. ``jongo run`` is one
process, so its limits are exact; behind several worker processes each worker counts on its
own, which makes the effective limit ``workers x limit``. Restarting the server clears them.
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque

from .errors import HTTPError, JongoError

__all__ = ["RateLimit", "Limiter", "TooManyRequests", "parse", "limiter", "check", "client_key"]

_UNITS = {"second": 1, "sec": 1, "s": 1, "minute": 60, "min": 60, "m": 60,
          "hour": 3600, "h": 3600, "day": 86400, "d": 86400}
_SPEC = re.compile(r"^\s*(\d+)\s*(?:/|per)\s*(\d+(?:\.\d+)?)?\s*([a-z]+?)s?\s*$", re.I)

#: The most distinct callers one limiter remembers; the oldest are forgotten beyond this.
MAX_KEYS = 50_000


class TooManyRequests(HTTPError):
    """Raised when a caller is over a rate limit: a 429 with ``retry_after`` seconds."""

    code = "J213"

    def __init__(self, message: str | None = None, *, retry_after: int = 1, hint: str | None = None):
        super().__init__(429, message or "Too many requests. Try again shortly.", code="J213", hint=hint)
        #: Whole seconds until the caller may try again (also sent as the Retry-After header).
        self.retry_after = max(1, int(retry_after))


class RateLimit:
    """``count`` calls per ``seconds``, e.g. ``RateLimit(5, 60)``.

    Usually written as a string and parsed by :func:`parse`: ``"5/minute"``,
    ``"100/hour"``, ``"10 per 30 seconds"``, ``"3/s"``.
    """

    __slots__ = ("count", "seconds")

    def __init__(self, count: int, seconds: float):
        numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (count, seconds))
        if not numbers or not 1 <= count < float("inf") or not 0 < seconds < float("inf"):
            raise JongoError(f"a rate limit needs a positive count and period, got {count!r} calls per "
                             f"{seconds!r} seconds", code="J513",
                             hint='write it as "5/minute", or as a (count, seconds) pair of numbers: (5, 60)')
        self.count = int(count)
        self.seconds = float(seconds)

    def __str__(self) -> str:
        for name, size in (("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1)):
            if self.seconds % size == 0:
                n = int(self.seconds // size)
                return f"{self.count}/{name}" if n == 1 else f"{self.count} per {n} {name}s"
        return f"{self.count} per {self.seconds:g} seconds"

    def __repr__(self) -> str:
        return f"RateLimit({self.count}, {self.seconds:g})"

    def __eq__(self, other) -> bool:
        return isinstance(other, RateLimit) and (self.count, self.seconds) == (other.count, other.seconds)

    def __hash__(self) -> int:
        return hash((self.count, self.seconds))


def parse(spec) -> RateLimit | None:
    """Turn ``"5/minute"`` (or a ``(count, seconds)`` pair, or a RateLimit) into a RateLimit.

    Parameters:
        spec: ``"<count>/<unit>"`` or ``"<count> per <n> <unit>s"`` with unit second, minute,
            hour or day (``s``, ``m``, ``h``, ``d`` for short); a ``(count, seconds)`` tuple; an
            existing :class:`RateLimit`; or ``None``/``False`` for "no limit".

    Returns:
        A :class:`RateLimit`, or ``None`` when ``spec`` means no limit.

    Raises:
        JongoError: the text isn't a rate (the message shows the accepted forms).

    Example::

        parse("5/minute")          # RateLimit(5, 60)
        parse("10 per 30 seconds") # RateLimit(10, 30)
        parse((100, 3600))         # RateLimit(100, 3600)
    """
    if spec is None or spec is False:
        return None
    if isinstance(spec, RateLimit):
        return spec
    if isinstance(spec, (tuple, list)) and len(spec) == 2:
        return RateLimit(spec[0], spec[1])
    if isinstance(spec, str):
        found = _SPEC.match(spec)
        if found:
            unit = found.group(3).lower()
            size = _UNITS.get(unit) or _UNITS.get(unit + "s")
            if size and int(found.group(1)) >= 1:
                return RateLimit(int(found.group(1)), size * float(found.group(2) or 1))
    raise JongoError(f"can't read the rate limit {spec!r}", code="J513",
                     hint='write it as "5/minute", "100/hour" or "10 per 30 seconds"')


class Limiter:
    """A sliding-window counter per key, held in memory and safe to share between threads.

    Each key keeps the times of its recent hits; a hit is allowed while fewer than
    ``limit.count`` of them fall inside the last ``limit.seconds``. Unlike a fixed window,
    a burst can't straddle a boundary and get through twice.

    Example::

        limits = Limiter()
        allowed, retry_after = limits.hit("invite:42", parse("5/minute"))
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, deque] = {}

    def hit(self, key: str, limit: RateLimit) -> tuple[bool, int]:
        """Record one attempt for ``key`` and say whether it is within ``limit``.

        Parameters:
            key: who and what is being counted, e.g. ``"login:ann"``.
            limit: the cap to apply.

        Returns:
            ``(allowed, retry_after)``: ``allowed`` is False when the caller is over the
            limit (the attempt is then not recorded), and ``retry_after`` is the whole
            seconds until the next attempt would be allowed (0 when allowed).
        """
        now = self._clock()
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                if len(self._hits) >= MAX_KEYS:
                    self._evict(now)
                hits = self._hits[key] = deque()
            cutoff = now - limit.seconds
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= limit.count:
                return False, max(1, int(hits[0] + limit.seconds - now + 0.999))
            hits.append(now)
            return True, 0

    def peek(self, key: str, limit: RateLimit) -> tuple[bool, int]:
        """Like :meth:`hit` but records nothing: would an attempt be allowed right now?"""
        now = self._clock()
        with self._lock:
            hits = self._hits.get(key)
            if not hits:
                return True, 0
            cutoff = now - limit.seconds
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= limit.count:
                return False, max(1, int(hits[0] + limit.seconds - now + 0.999))
            return True, 0

    def refund(self, key: str) -> None:
        """Take back ``key``'s most recent hit: for an attempt that turned out not to count.

        ``authenticate()`` records every attempt *before* checking the password (so a
        burst of parallel guesses can't all slip past the limit), then refunds the ones
        that were correct.
        """
        with self._lock:
            hits = self._hits.get(key)
            if hits:
                hits.pop()

    def reset(self, key: str | None = None) -> None:
        """Forget ``key``'s hits, or every key's when called with no argument."""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)

    def _evict(self, now: float) -> None:
        # Called with the lock held, only when the table is full: drop the keys whose most
        # recent hit is oldest. A flood of distinct keys can't grow memory without bound.
        ordered = sorted(self._hits.items(), key=lambda kv: kv[1][-1] if kv[1] else 0.0)
        for key, _ in ordered[: max(1, len(ordered) // 10)]:
            del self._hits[key]

    def __len__(self) -> int:
        with self._lock:
            return len(self._hits)


#: The process-wide limiter used by ``rate_limit=`` and by ``authenticate()``.
limiter = Limiter()


def client_key(request) -> str:
    """Who to count a request against: ``user:<id>`` when signed in, else ``ip:<address>``.

    Behind a proxy the address is only meaningful with ``Jongo(trust_proxy=True)``, which
    makes ``request.client_ip`` read the proxy's ``X-Forwarded-For`` entry.
    """
    try:
        user = request.user
    except Exception:
        user = None
    uid = getattr(user, "id", None)
    if uid is not None:
        return f"user:{uid}"
    return f"ip:{getattr(request, 'client_ip', None) or getattr(request, 'remote_addr', '') or 'unknown'}"


def check(request, limit, scope: str, *, key: str | None = None) -> None:
    """Count this request against ``limit`` and raise :class:`TooManyRequests` if it is over.

    This is what ``rate_limit=`` on ``@server``, ``@app.page`` and ``@app.route`` calls; use
    it directly for a limit keyed by something else (an email address, an API key).

    Parameters:
        request: the current request (used to find the caller unless ``key`` is given).
        limit: a rate as accepted by :func:`parse`; ``None`` means no limit.
        scope: what is being limited, so two functions with the same rate don't share a
            counter, e.g. ``"reset-password"``.
        key: count against this instead of the user/address.

    Raises:
        TooManyRequests: 429, code ``J213``, with ``retry_after`` seconds.

    Example::

        @server
        def reset_password(request, email: str) -> dict:
            ratelimit.check(request, "3/hour", "reset-password", key=email.lower())
            ...
    """
    parsed = parse(limit)
    if parsed is None:
        return
    allowed, retry_after = limiter.hit(f"{scope}|{key if key is not None else client_key(request)}", parsed)
    if not allowed:
        raise TooManyRequests(f"Too many requests (limit {parsed}). Try again in {retry_after}s.",
                              retry_after=retry_after)
