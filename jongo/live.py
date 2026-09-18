"""Real-time push: send data from a server function straight to connected components.

A channel has to be declared before anything can subscribe to it — the same rule as
``@server``, for the same reason. The declaring function authorises each subscription:

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
"""

from __future__ import annotations

import json
import queue
import re
import threading
import typing
from dataclasses import dataclass, field

from . import log as _jlog
from .errors import Forbidden, JongoError
from .vdom import to_json_data
from .routing import CONVERTERS, _ANNOTATION_CONVERTERS, _PARAM

#: How many undelivered messages one browser connection will hold before shedding the oldest.
BACKLOG = 100
#: How many channels a single connection may subscribe to.
MAX_CHANNELS = 20
MAX_CHANNEL_LENGTH = 200
#: Seconds between keepalive comments, so proxies don't close an idle stream.
PING_SECONDS = 15

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


class Hub:
    """Fan-out from ``broadcast()`` to the connections subscribed to each channel."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._channels: dict[str, set[Subscriber]] = {}

    def subscribe(self, channels: typing.Iterable[str]) -> Subscriber:
        subscriber = Subscriber(frozenset(channels))
        with self._lock:
            for channel in subscriber.channels:
                self._channels.setdefault(channel, set()).add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
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
            return len({s for listeners in self._channels.values() for s in listeners})

    def clear(self) -> None:
        """Drop every subscription (used between tests)."""
        with self._lock:
            self._channels.clear()


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
            raise Forbidden("Invalid channel name.")
        for pattern in declared:
            params = pattern.match(channel)
            if params is None:
                continue
            if pattern.guard(request, **params) is False:
                raise Forbidden(f"Not allowed to subscribe to {channel!r}.")
            allowed.append(channel)
            break
        else:
            raise Forbidden(f"No channel matches {channel!r}.")
    return allowed


def event_stream(subscriber: Subscriber, *, ping_seconds: float = PING_SECONDS,
                 stop: threading.Event | None = None) -> typing.Iterator[str]:
    """Yield SSE frames for one connection until the client goes away."""
    yield ": subscribed\n\n"
    while stop is None or not stop.is_set():
        try:
            payload = subscriber.queue.get(timeout=ping_seconds)
        except queue.Empty:
            yield ": ping\n\n"  # keeps proxies from closing an idle stream
            continue
        yield f"data: {payload}\n\n"


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
        raise Forbidden(f"A connection may subscribe to at most {MAX_CHANNELS} channels.")
    return list(seen)
