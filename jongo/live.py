"""Real-time push: send data from a server function straight to connected components.

A channel has to be declared before anything can subscribe to it — the same rule as
``@server``, for the same reason. The declaring function authorises each subscription, and
it has to say yes: only a truthy return value allows it. ``False`` refuses (``J410``), and
so does returning nothing at all (``J412``), so a guard with a forgotten ``return`` fails
closed:

    @app.channel("room:<int:id>")
    def room(request, id):
        return id in request.session.get("rooms", [])

    @server
    def post(request, room: int, text: str) -> dict:
        message = Message.create(room_id=room, text=text).to_dict()
        broadcast(f"room:{room}", message)      # every subscriber gets it
        return message

    @component
    def Chat(room):
        messages = state([])
        live(f"room:{room}", lambda message: messages.set([*messages.value, message]))
        return ul([li(m["text"], key=m["id"]) for m in messages.value])

Delivery is at-most-once and in-order per subscriber: messages are pushed over one SSE
connection per browser tab, and a subscriber that falls too far behind drops its oldest
messages rather than growing without bound. It is a live feed, not a queue — a client
that reconnects sees what happens next, not what it missed.

A stream opened by a signed-in user ends when that sign-in stops being valid (revoked,
expired, or the password changed): the stream re-checks it at least every
``SESSION_CHECK_SECONDS`` while it delivers messages, and on every keepalive. To end one
browser's streams at once, call ``hub.drop_session(login_session)`` (or
``hub.drop_user(user)``) after revoking.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
import typing
from dataclasses import dataclass, field

from . import log as _jlog
from .errors import Forbidden, HTTPError, JongoError
from .vdom import to_json_data
from .routing import CONVERTERS, _ANNOTATION_CONVERTERS, _PARAM

#: How many undelivered messages one browser connection will hold before shedding the oldest.
BACKLOG = 100
#: How many channels a single connection may subscribe to.
MAX_CHANNELS = 20
MAX_CHANNEL_LENGTH = 200
#: Seconds between keepalive comments, so proxies don't close an idle stream.
PING_SECONDS = 15
#: How many live connections one process holds at most; past it, new ones get a 503.
MAX_SUBSCRIBERS = 10_000
#: How long a refused (full) connection's browser waits before trying again, in milliseconds.
FULL_RETRY_MS = 5_000
#: The longest a signed-in user's stream goes without re-checking that the sign-in still holds.
SESSION_CHECK_SECONDS = 5.0

_END = object()  # put on a subscriber's queue to end its stream

_CHANNEL_NAME = re.compile(r"^[^\s\x00-\x1f]{1,%d}$" % MAX_CHANNEL_LENGTH)


class Channel:
    """A declared channel pattern, e.g. ``"room:<int:id>"``, and its authorisation guard."""

    def __init__(self, pattern: str, guard: typing.Callable):
        if not pattern or not _CHANNEL_NAME.match(pattern):
            raise ValueError(f"Channel patterns must be one line of at most {MAX_CHANNEL_LENGTH} characters.")
        self.pattern = pattern
        self.guard = guard
        self.name = getattr(guard, "__name__", pattern)
        self.params: dict[str, typing.Callable] = {}
        try:
            hints = typing.get_type_hints(guard)
        except Exception:
            hints = {}
        regex, last = ["^"], 0
        for match in _PARAM.finditer(pattern):
            converter, param = match.group(1), match.group(2)
            if converter is None:
                converter = _ANNOTATION_CONVERTERS.get(hints.get(param), "str")
            if converter not in CONVERTERS:
                raise ValueError(f"unknown converter {converter!r} in channel {pattern!r}")
            pattern_regex, convert = CONVERTERS[converter]
            regex.append(re.escape(pattern[last:match.start()]))
            regex.append(f"(?P<{param}>{pattern_regex})")
            self.params[param] = convert
            last = match.end()
        regex.append(re.escape(pattern[last:]))
        regex.append("$")
        self.regex = re.compile("".join(regex))

    def match(self, channel: str) -> dict | None:
        found = self.regex.match(channel)
        if not found:
            return None
        try:
            return {name: self.params[name](value) for name, value in found.groupdict().items()}
        except ValueError:
            return None

    def __repr__(self) -> str:
        return f"<Channel {self.pattern} -> {self.name}>"


@dataclass(eq=False)  # identity, so a subscriber can live in a set
class Subscriber:
    """One browser connection's slice of the hub."""

    channels: frozenset[str]
    queue: "queue.Queue[str]" = field(default_factory=lambda: queue.Queue(maxsize=BACKLOG))
    dropped: int = 0
    #: The sign-in (LoginSession id) and user id the connection was opened with, if any.
    login_session: typing.Any = None
    user: typing.Any = None
    #: Set when the hub ended the stream (drop_session/drop_user); it closes on its next step.
    ended: bool = False

    def end(self) -> None:
        self.ended = True
        self.offer(_END)  # wake the stream if it is waiting for a message

    def offer(self, payload: str) -> None:
        """Queue a message, shedding the oldest if this subscriber is not keeping up."""
        while True:
            try:
                self.queue.put_nowait(payload)
                return
            except queue.Full:
                try:
                    self.queue.get_nowait()
                    self.dropped += 1
                except queue.Empty:  # another thread drained it first
                    pass


class HubFull(HTTPError):
    """The process holds ``MAX_SUBSCRIBERS`` live connections already (a 503)."""

    def __init__(self):
        super().__init__(503, f"This server holds the most live connections it allows ({MAX_SUBSCRIBERS}).",
                         code="J410",
                         hint="try again shortly; jongo.live.MAX_SUBSCRIBERS caps open live() streams per process")
        self.retry_after = max(1, FULL_RETRY_MS // 1000)


class Hub:
    """Fan-out from ``broadcast()`` to the connections subscribed to each channel."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._channels: dict[str, set[Subscriber]] = {}
        self._subscribers: set[Subscriber] = set()

    def check_capacity(self) -> None:
        """Raise :class:`HubFull` (a 503) if no more connections may subscribe."""
        with self._lock:
            full = len(self._subscribers) >= MAX_SUBSCRIBERS
        if full:
            raise HubFull()

    def subscribe(self, channels: typing.Iterable[str], *, login_session=None, user=None) -> Subscriber:
        """Register a connection on ``channels``; raises :class:`HubFull` past ``MAX_SUBSCRIBERS``."""
        subscriber = Subscriber(frozenset(channels), login_session=login_session, user=user)
        with self._lock:
            if len(self._subscribers) >= MAX_SUBSCRIBERS:
                raise HubFull()
            self._subscribers.add(subscriber)
            for channel in subscriber.channels:
                self._channels.setdefault(channel, set()).add(subscriber)
        return subscriber

    def drop_session(self, session) -> int:
        """End the open streams of one sign-in now; returns how many ended.

        ``session`` is a ``jongo.auth.LoginSession`` or its id, as ``auth.revoke_session``
        takes. Without this a revoked session's streams still end, within
        ``SESSION_CHECK_SECONDS`` of their next message or keepalive.
        """
        pk = getattr(session, "pk", session)
        return self._drop(lambda s: s.login_session is not None and s.login_session == pk)

    def drop_user(self, user) -> int:
        """End every open stream a user (or user id) has, e.g. after ``auth.revoke_sessions``."""
        uid = getattr(user, "pk", user)
        return self._drop(lambda s: s.user is not None and s.user == uid)

    def _drop(self, match) -> int:
        with self._lock:
            doomed = [s for s in self._subscribers if match(s)]
        for subscriber in doomed:
            self.unsubscribe(subscriber)  # no more messages reach it...
            subscriber.end()              # ...and its stream closes on its next step
        return len(doomed)

    def unsubscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)
            for channel in subscriber.channels:
                listeners = self._channels.get(channel)
                if listeners is None:
                    continue
                listeners.discard(subscriber)
                if not listeners:
                    del self._channels[channel]

    def publish(self, channel: str, data: typing.Any) -> int:
        """Send ``data`` to everyone on ``channel``; returns how many connections got it."""
        with self._lock:
            listeners = list(self._channels.get(channel, ()))
        if not listeners:
            return 0
        payload = json.dumps({"channel": channel, "data": data}, separators=(",", ":"))
        for subscriber in listeners:
            subscriber.offer(payload)
        return len(listeners)

    def subscriber_count(self, channel: str | None = None) -> int:
        with self._lock:
            if channel is not None:
                return len(self._channels.get(channel, ()))
            return len(self._subscribers)

    def clear(self) -> None:
        """Drop every subscription (used between tests)."""
        with self._lock:
            self._channels.clear()
            self._subscribers.clear()


#: The process-wide hub. One process fans out to its own connections; see the docs on
#: running more than one worker.
hub = Hub()


def broadcast(channel: str, data: typing.Any = None) -> int:
    """Push ``data`` to every browser subscribed to ``channel``.

    Returns the number of connections it reached — 0 when nobody is listening, which is
    not an error. ``data`` must be JSON-serialisable.
    """
    if not isinstance(channel, str) or not _CHANNEL_NAME.match(channel):
        raise ValueError(f"Channel names must be one line of at most {MAX_CHANNEL_LENGTH} characters.")
    # The same conversion RPC results go through: datetimes, Decimals and VNodes are
    # handled, and anything else is a clear error rather than a broken frame on the wire.
    payload = to_json_data(data, "broadcast() data")
    delivered = hub.publish(channel, payload)
    if delivered:
        _jlog.get_logger("live").debug("broadcast", extra={"channel": channel, "subscribers": delivered})
    return delivered


def authorize(channels: typing.Iterable[str], declared: list[Channel], request) -> list[str]:
    """Return the channels this request may subscribe to, raising Forbidden for the rest.

    An undeclared channel is refused the same way an undeclared function is: it simply
    does not exist as far as the browser is concerned.
    """
    allowed: list[str] = []
    for channel in channels:
        if not _CHANNEL_NAME.match(channel):
            raise Forbidden("Invalid channel name.", code="J410")
        for pattern in declared:
            params = pattern.match(channel)
            if params is None:
                continue
            verdict = pattern.guard(request, **params)
            if verdict is None:
                # A guard that falls off the end (a forgotten `return`, a branch with no
                # result) used to count as "yes". A missing answer is not permission.
                raise Forbidden(
                    f"Not allowed to subscribe to {channel!r}: the guard {pattern.name}() returned None.",
                    code="J412",
                    hint=f"make {pattern.name}() return True to allow the subscription and False to refuse it",
                )
            if not verdict:
                raise Forbidden(f"Not allowed to subscribe to {channel!r}.", code="J410")
            allowed.append(channel)
            break
        else:
            raise Forbidden(f"No channel matches {channel!r}.", code="J410",
                            hint="declare it with @app.channel(pattern) on the server")
    return allowed


def event_stream(subscriber: Subscriber, *, ping_seconds: float = PING_SECONDS,
                 stop: threading.Event | None = None,
                 alive: typing.Callable[[], bool] | None = None) -> typing.Iterator[str]:
    """Yield SSE frames for one connection until the client goes away.

    ``alive``, when given, says whether the connection may keep receiving (for a signed-in
    user: whether the sign-in still holds). It is asked before a message is delivered and
    on a keepalive, at most once per ``SESSION_CHECK_SECONDS``; once it says no (or
    raises), the stream ends without delivering anything more. The stream also ends when
    the hub drops the subscriber.
    """
    yield ": subscribed\n\n"
    checked = time.monotonic()  # the request that opened the stream was just checked
    while (stop is None or not stop.is_set()) and not subscriber.ended:
        try:
            payload = subscriber.queue.get(timeout=ping_seconds)
        except queue.Empty:
            payload = None
        if payload is _END or subscriber.ended:
            break
        if alive is not None and time.monotonic() - checked >= SESSION_CHECK_SECONDS:
            if not _still_alive(alive):
                break
            checked = time.monotonic()
        if payload is None:
            yield ": ping\n\n"  # keeps proxies from closing an idle stream
            continue
        yield f"data: {payload}\n\n"
    if stop is None or not stop.is_set():  # ended by the hub, or the sign-in no longer holds
        yield ": signed out\n\n"


def _still_alive(alive) -> bool:
    try:
        return bool(alive())
    except Exception:  # can't tell (e.g. the database is down): fail closed; the browser reconnects
        _jlog.get_logger("live").warning("couldn't re-check a live stream's sign-in; ending it", exc_info=True)
        return False


def parse_channels(raw: str | None) -> list[str]:
    """Parse the ``channels`` query parameter: a comma-separated list, deduplicated."""
    if not raw:
        return []
    seen: dict[str, None] = {}
    for part in raw.split(","):
        channel = part.strip()
        if channel:
            seen[channel] = None
    if len(seen) > MAX_CHANNELS:
        raise Forbidden(
            f"A connection may subscribe to at most {MAX_CHANNELS} channels; this one asked for {len(seen)}.",
            code="J210",
            hint=f"every live() on a page shares the tab's one connection, capped at jongo.live.MAX_CHANNELS "
                 f"({MAX_CHANNELS}); subscribe to fewer, broader channels, or raise MAX_CHANNELS on the server",
        )
    return list(seen)
