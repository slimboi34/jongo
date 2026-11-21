"""Rate limits: a cap on how often one caller may hit a server function, a route or the login.

    @server(rate_limit="5/minute")
    def send_invite(request, email: str) -> dict: ...

    @app.route("/webhook", methods=("POST",), csrf=False, rate_limit="30/minute")
    def webhook(request): ...

Every request counts against the client's address and, when someone is signed in, against
their user as well; it is refused when either is over the limit. So signing in as a fresh
account (a sign-up loop) doesn't buy a fresh window, and sharing one account between
machines doesn't either. :data:`ADDRESS_FACTOR` loosens the address side for signed-in
users who share an address. A caller over the limit gets ``429 Too Many Requests`` with a
``Retry-After`` header and the error code ``J213``; the function does not run.

``jongo.auth.authenticate()`` uses the same machinery to throttle password guessing, with
no configuration (see :data:`jongo.auth.LOGIN_ATTEMPTS`), on a limiter of its own.

Counters live in the memory of the process that served the request. ``jongo run`` is one
process, so its limits are exact; behind several worker processes each worker counts on its
own, which makes the effective limit ``workers x limit``. Restarting the server clears them.
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections import deque

from .errors import HTTPError, JongoError

__all__ = ["RateLimit", "Limiter", "TooManyRequests", "parse", "limiter", "check", "client_key", "client_keys"]

_UNITS = {"second": 1, "sec": 1, "s": 1, "minute": 60, "min": 60, "m": 60,
          "hour": 3600, "h": 3600, "day": 86400, "d": 86400}
_SPEC = re.compile(r"^\s*(\d+)\s*(?:/|per)\s*(\d+(?:\.\d+)?)?\s*([a-z]+?)s?\s*$", re.I)

#: The most distinct callers one limiter remembers. When the table is full, callers with no
#: hits left inside their window are forgotten first, then the least recent callers that are
#: still under their limit. A caller who is *over* its limit is never forgotten, so flooding
#: the table with fresh keys can't lift somebody's block; if every caller is over its limit,
#: a new caller is refused (429) until one drops back under.
MAX_KEYS = 50_000

#: For a signed-in caller the request is also counted against the client address, which may
#: make ``limit x ADDRESS_FACTOR`` requests in all. ``1`` (the default) gives the address the
#: same limit as one user, so creating accounts in a loop gets nowhere. Raise it when many
#: signed-in users share one address (an office network, or a proxy without
#: ``Jongo(trust_proxy=True)``); ``None`` counts signed-in callers by user only, as 0.6 did.
#: Anonymous callers are always counted by address, at the limit itself.
ADDRESS_FACTOR: float | None = 1


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
        if count < 1 or seconds <= 0:
            raise JongoError(f"a rate limit needs a positive count and period, got {count} per {seconds}s",
                             code="J513")
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
        # The strictest limit each key has been counted with: (lowest count, longest window).
        self._limits: dict[str, tuple[int, float]] = {}
        self._next_sweep = float("-inf")  # a table full of blocked keys isn't swept again before this

    def hit(self, key: str, limit: RateLimit) -> tuple[bool, int]:
        """Record one attempt for ``key`` and say whether it is within ``limit``.

        Parameters:
            key: who and what is being counted, e.g. ``"login:ann"``.
            limit: the cap to apply.

        Returns:
            ``(allowed, retry_after)``: ``allowed`` is False when the caller is over the
            limit (the attempt is then not recorded), and ``retry_after`` is the whole
            seconds until the next attempt would be allowed (0 when allowed). A new key is
            also refused while the table holds :data:`MAX_KEYS` callers that are all over
            their limit.
        """
        now = self._clock()
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                if len(self._hits) >= MAX_KEYS:
                    wait = self._make_room(now)
                    if wait:
                        return False, wait
                hits = self._hits[key] = deque()
            count, window = self._limits.get(key, (limit.count, limit.seconds))
            self._limits[key] = (min(count, limit.count), max(window, limit.seconds))
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
                self._limits.clear()
                self._next_sweep = float("-inf")
            else:
                self._hits.pop(key, None)
                self._limits.pop(key, None)

    def _make_room(self, now: float) -> int:
        """Free a slot for a new key in a full table; 0 if there is one now, else seconds to wait.

        Called with the lock held. In order: keys with nothing left inside their window go,
        then the least recently seen keys that are under their limit (forgetting those only
        ever gives a caller a fresh window it was entitled to anyway). A key that is over its
        limit stays: dropping it (0.6 dropped the oldest 10% whatever their state) let anyone
        lift a throttled caller's block, the login throttle included, by flooding the table
        with fresh keys. If every key is over its limit the new key is refused: the limiter
        fails closed. A sweep frees a tenth of the table, so it runs rarely.
        """
        if now < self._next_sweep:
            return max(1, int(self._next_sweep - now + 0.999))
        spare, blocked_until = [], []
        for key, hits in list(self._hits.items()):
            count, window = self._limits.get(key, (1, 0.0))
            if not hits or hits[-1] + window <= now:
                del self._hits[key]
                self._limits.pop(key, None)
            elif len(hits) >= count and hits[-count] + window > now:
                blocked_until.append(hits[-count] + window)  # when it drops back under its limit
            else:
                spare.append((hits[-1], key))
        excess = len(self._hits) - (MAX_KEYS - max(1, MAX_KEYS // 10))
        if excess > 0 and spare:
            spare.sort()
            for _, key in spare[:excess]:
                del self._hits[key]
                self._limits.pop(key, None)
        if len(self._hits) < MAX_KEYS:
            self._next_sweep = float("-inf")
            return 0
        self._next_sweep = min(blocked_until) if blocked_until else now + 1
        return max(1, int(self._next_sweep - now + 0.999))

    def __len__(self) -> int:
        with self._lock:
            return len(self._hits)


#: The process-wide limiter used by ``rate_limit=`` and :func:`check`. (``authenticate()``
#: has its own, :data:`jongo.auth.limiter`, so these calls can't crowd its counters out.)
limiter = Limiter()


def _user_id(request):
    try:
        user = request.user
    except Exception:
        user = None
    return getattr(user, "id", None)


def _address(request) -> str:
    return getattr(request, "client_ip", None) or getattr(request, "remote_addr", "") or "unknown"


def client_key(request) -> str:
    """The caller's main identity: ``user:<id>`` when signed in, else ``ip:<address>``.

    :func:`check` counts a signed-in request against the address as well; see
    :func:`client_keys`. Behind a proxy the address is only meaningful with
    ``Jongo(trust_proxy=True)``, which makes ``request.client_ip`` read the proxy's
    ``X-Forwarded-For`` entry.
    """
    uid = _user_id(request)
    return f"user:{uid}" if uid is not None else f"ip:{_address(request)}"


def client_keys(request) -> list[str]:
    """Every identity a request counts against: ``ip:<address>``, plus ``user:<id>`` when signed in.

    Example::

        client_keys(request)   # ["ip:203.0.113.9", "user:42"]
    """
    keys = [f"ip:{_address(request)}"]
    uid = _user_id(request)
    if uid is not None:
        keys.append(f"user:{uid}")
    return keys


def _scaled(limit: RateLimit, factor: float) -> RateLimit:
    if factor == 1:
        return limit
    return RateLimit(max(1, math.ceil(limit.count * factor)), limit.seconds)


def check(request, limit, scope: str, *, key: str | None = None) -> None:
    """Count this request against ``limit`` and raise :class:`TooManyRequests` if it is over.

    This is what ``rate_limit=`` on ``@server``, ``@app.page`` and ``@app.route`` calls; use
    it directly for a limit keyed by something else (an email address, an API key).

    Without ``key`` the request counts against the client address, and also against the
    user when someone is signed in (the address then gets ``limit x`` :data:`ADDRESS_FACTOR`);
    it is refused if either is over, and then neither is charged for it.

    Parameters:
        request: the current request (used to find the caller unless ``key`` is given).
        limit: a rate as accepted by :func:`parse`; ``None`` means no limit.
        scope: what is being limited, so two functions with the same rate don't share a
            counter, e.g. ``"reset-password"``.
        key: count against this alone instead of the address and user.

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
    if key is not None:
        targets = [(str(key), parsed)]
    else:
        uid = _user_id(request)
        if uid is None:
            targets = [(f"ip:{_address(request)}", parsed)]
        else:
            # Count the user *and* the address: keyed by user alone, every new account would
            # start a fresh window, so a sign-up loop would never be refused.
            targets = [(f"user:{uid}", parsed)]
            if ADDRESS_FACTOR:
                targets.append((f"ip:{_address(request)}", _scaled(parsed, ADDRESS_FACTOR)))
    counted = []
    for who, rate in targets:
        allowed, retry_after = limiter.hit(f"{scope}|{who}", rate)
        if not allowed:
            for earlier in counted:  # a refused request isn't charged to the other identity either
                limiter.refund(earlier)
            raise TooManyRequests(f"Too many requests (limit {parsed}). Try again in {retry_after}s.",
                                  retry_after=retry_after)
        counted.append(f"{scope}|{who}")
