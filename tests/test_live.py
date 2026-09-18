"""Real-time channels: declaration, authorisation, fan-out and the browser hook."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from decimal import Decimal

import pytest

from jongo import Jongo, broadcast, component, live, state
from jongo.errors import Forbidden, JongoError
from jongo.html import div, li, ul
from jongo.live import (BACKLOG, MAX_CHANNELS, Channel, Hub, authorize, event_stream, hub,
                        parse_channels)
from jongo.testing import TestClient


@pytest.fixture(autouse=True)
def empty_hub():
    hub.clear()
    yield
    hub.clear()


# -- channel patterns -----------------------------------------------------------------


class TestChannelPatterns:
    def test_a_literal_channel_matches_itself(self):
        channel = Channel("prices", lambda request: True)
        assert channel.match("prices") == {}
        assert channel.match("prices:1") is None
        assert channel.match("price") is None

    def test_parameters_are_converted(self):
        channel = Channel("room:<int:id>", lambda request, id: True)
        assert channel.match("room:12") == {"id": 12}
        assert channel.match("room:abc") is None
        assert channel.match("room:12:extra") is None

    def test_parameter_type_comes_from_the_annotation(self):
        def guard(request, id: int):
            return True

        assert Channel("room:<id>", guard).match("room:7") == {"id": 7}

    def test_a_channel_pattern_must_be_one_short_line(self):
        with pytest.raises(ValueError):
            Channel("a\nb", lambda request: True)
        with pytest.raises(ValueError):
            Channel("x" * 500, lambda request: True)


class TestParsing:
    def test_channels_are_split_and_deduplicated(self):
        assert parse_channels(" a, b ,a, ") == ["a", "b"]
        assert parse_channels("") == []
        assert parse_channels(None) == []

    def test_too_many_channels_is_refused(self):
        with pytest.raises(Forbidden):
            parse_channels(",".join(f"c{i}" for i in range(MAX_CHANNELS + 1)))


class TestAuthorization:
    def test_an_undeclared_channel_is_refused(self):
        with pytest.raises(Forbidden, match="No channel matches"):
            authorize(["secrets"], [], request=None)

    def test_a_guard_returning_false_refuses(self):
        declared = [Channel("room:<int:id>", lambda request, id: id == 1)]
        assert authorize(["room:1"], declared, request=None) == ["room:1"]
        with pytest.raises(Forbidden, match="Not allowed"):
            authorize(["room:2"], declared, request=None)

    def test_a_guard_may_raise(self):
        def guard(request, id):
            raise Forbidden("nope")

        with pytest.raises(Forbidden):
            authorize(["room:1"], [Channel("room:<id>", guard)], request=None)

    def test_the_request_is_passed_to_the_guard(self):
        seen = []
        declared = [Channel("feed", lambda request: seen.append(request) or True)]
        authorize(["feed"], declared, request="REQ")
        assert seen == ["REQ"]


# -- fan-out --------------------------------------------------------------------------


class TestHub:
    def test_publish_reaches_every_subscriber_on_the_channel(self):
        hub = Hub()
        one, two = hub.subscribe(["a"]), hub.subscribe(["a", "b"])
        assert hub.publish("a", {"x": 1}) == 2
        for subscriber in (one, two):
            assert json.loads(subscriber.queue.get_nowait()) == {"channel": "a", "data": {"x": 1}}

    def test_publish_to_an_empty_channel_is_not_an_error(self):
        assert Hub().publish("nobody", 1) == 0

    def test_other_channels_are_untouched(self):
        hub = Hub()
        subscriber = hub.subscribe(["a"])
        hub.publish("b", 1)
        assert subscriber.queue.empty()

    def test_unsubscribing_stops_delivery(self):
        hub = Hub()
        subscriber = hub.subscribe(["a"])
        hub.unsubscribe(subscriber)
        assert hub.publish("a", 1) == 0
        assert hub.subscriber_count() == 0

    def test_a_slow_subscriber_sheds_its_oldest_messages(self):
        """A stuck browser must not grow the server's memory without bound."""
        hub = Hub()
        subscriber = hub.subscribe(["a"])
        for i in range(BACKLOG + 10):
            hub.publish("a", i)
        assert subscriber.queue.qsize() == BACKLOG
        assert subscriber.dropped == 10
        assert json.loads(subscriber.queue.get_nowait())["data"] == 10  # oldest ten gone

    def test_concurrent_publishes_are_all_delivered(self):
        hub = Hub()
        subscriber = hub.subscribe(["a"])
        threads = [threading.Thread(target=lambda n=n: hub.publish("a", n)) for n in range(50)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert subscriber.queue.qsize() == 50


class TestBroadcast:
    def test_broadcast_uses_the_process_hub(self):
        subscriber = hub.subscribe(["news"])
        assert broadcast("news", {"headline": "hi"}) == 1
        assert json.loads(subscriber.queue.get_nowait())["data"] == {"headline": "hi"}

    def test_broadcast_with_no_listeners_returns_zero(self):
        assert broadcast("quiet") == 0

    def test_an_invalid_channel_name_is_rejected(self):
        with pytest.raises(ValueError):
            broadcast("bad\nname", 1)

    def test_non_serialisable_data_is_a_clear_error(self):
        hub.subscribe(["news"])
        with pytest.raises(JongoError, match="broadcast"):
            broadcast("news", {"when": object()})

    def test_dates_and_decimals_convert_like_rpc_results(self):
        subscriber = hub.subscribe(["news"])
        broadcast("news", {"at": datetime(2026, 9, 18, 12, 0), "price": Decimal("1.50")})
        assert json.loads(subscriber.queue.get_nowait())["data"] == \
            {"at": "2026-09-18T12:00:00", "price": 1.5}


class TestEventStream:
    def test_frames_are_sse_formatted(self):
        hub = Hub()
        subscriber = hub.subscribe(["a"])
        hub.publish("a", {"x": 1})
        stop = threading.Event()
        frames = event_stream(subscriber, ping_seconds=0.01, stop=stop)
        assert next(frames) == ": subscribed\n\n"
        frame = next(frames)
        assert frame.startswith("data: ") and frame.endswith("\n\n")
        assert json.loads(frame[6:].strip())["data"] == {"x": 1}

    def test_an_idle_stream_sends_keepalives(self):
        subscriber = Hub().subscribe(["a"])
        frames = event_stream(subscriber, ping_seconds=0.01, stop=threading.Event())
        next(frames)
        assert next(frames) == ": ping\n\n"


# -- the route ------------------------------------------------------------------------


@pytest.fixture
def app():
    app = Jongo("live_test", secret_key="test-secret")

    @app.channel("prices")
    def prices(request):
        return True

    @app.channel("room:<int:id>")
    def room(request, id):
        return id < 10

    @app.page("/")
    def home():
        return div("home")

    return app


class TestRoute:
    def test_subscribing_opens_a_stream(self, app):
        client = TestClient(app)
        response, chunks = client.stream("/_jongo/live?channels=prices")
        try:
            assert response.status == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert hub.subscriber_count("prices") == 1
            assert broadcast("prices", {"btc": 1}) == 1
            frames = b"".join(next(chunks) for _ in range(2))  # the greeting, then the message
            assert b"btc" in frames
        finally:
            chunks.close()

    def test_closing_the_connection_unsubscribes(self, app):
        client = TestClient(app)
        _, chunks = client.stream("/_jongo/live?channels=prices")
        next(chunks, None)
        assert hub.subscriber_count("prices") == 1
        chunks.close()
        assert hub.subscriber_count("prices") == 0

    def test_an_undeclared_channel_is_forbidden(self, app):
        response = TestClient(app).get("/_jongo/live?channels=secrets")
        assert response.status == 403

    def test_a_refused_subscription_is_forbidden(self, app):
        assert TestClient(app).get("/_jongo/live?channels=room:99").status == 403
        response, chunks = TestClient(app).stream("/_jongo/live?channels=room:3")
        chunks.close()
        assert response.status == 200

    def test_the_stream_is_not_the_dev_reload_channel(self, app):
        """Without channels the route stays the dev-only reload stream."""
        assert TestClient(app).get("/_jongo/live").status == 404

    def test_several_channels_on_one_connection(self, app):
        client = TestClient(app)
        response, chunks = client.stream("/_jongo/live?channels=prices,room:1")
        try:
            assert response.status == 200
            assert hub.subscriber_count("prices") == 1
            assert hub.subscriber_count("room:1") == 1
            assert broadcast("room:1", "hello") == 1
        finally:
            chunks.close()


# -- the component hook ---------------------------------------------------------------


class TestHook:
    def test_live_compiles_to_the_runtime_hook(self, app):
        @component
        def Chat(room):
            messages = state([])
            live(f"room:{room}", lambda message: messages.set([*messages.value, message]))
            return ul([li(m) for m in messages.value])

        @app.page("/chat")
        def chat():
            return Chat(room=1)

        bundle = app.bundle()[0] if callable(getattr(app, "bundle", None)) else None
        source = bundle if isinstance(bundle, str) else TestClient(app).get("/_jongo/app.js").text
        assert "$live(" in source

    def test_live_renders_on_the_server_without_subscribing(self, app):
        @component
        def Ticker():
            price = state(0)
            live("prices", lambda value: price.set(value))
            return div(f"price {price.value}")

        @app.page("/ticker")
        def ticker():
            return Ticker()

        response = TestClient(app).get("/ticker")
        assert response.status == 200
        assert "price 0" in response.text
        assert hub.subscriber_count() == 0
