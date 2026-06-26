"""Unit tests for src/slack_daemon.py — Feature C reaction wiring."""

import asyncio

from slack_daemon import SlackDaemon


class FakeSlackClient:
    """Records reactions_add/remove and chat_postMessage calls."""

    def __init__(self) -> None:
        self.added: list[dict] = []
        self.removed: list[dict] = []
        self.posted: list[dict] = []

    async def reactions_add(self, **kwargs):
        self.added.append(kwargs)
        return {"ok": True}

    async def reactions_remove(self, **kwargs):
        self.removed.append(kwargs)
        return {"ok": True}

    async def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs)
        return {"ok": True, "ts": "posted.0"}


def make_daemon(monkeypatch) -> SlackDaemon:
    """Build a SlackDaemon without opening real Slack/socket connections."""
    import slack_daemon as sd

    # Neuter the slack-bolt app/handler constructors so __init__ does no I/O.
    class _FakeApp:
        def __init__(self, *a, **k):
            self.client = FakeSlackClient()

        def event(self, _name):  # decorator factory; registration is a no-op here
            def _wrap(fn):
                return fn
            return _wrap

    monkeypatch.setattr(sd, "AsyncApp", _FakeApp)
    monkeypatch.setattr(sd, "AsyncSocketModeHandler", lambda *a, **k: object())
    return SlackDaemon(bot_token="xoxb-test", app_token="xapp-test")


class TestTriggerMap:
    def test_trigger_to_thread_map_exists(self, monkeypatch):
        d = make_daemon(monkeypatch)
        assert d._trigger_to_thread == {}


class TestReactionLifecycle:
    def test_new_message_adds_then_removes_octagonal_sign(self, monkeypatch):
        d = make_daemon(monkeypatch)

        async def fake_handle_message(channel, message_ts, text):
            return "the reply"

        async def fake_post(channel, thread_ts, text):
            return None

        monkeypatch.setattr(d._claude, "handle_message", fake_handle_message)
        monkeypatch.setattr(d, "_post_response", fake_post)

        asyncio.run(d._handle_claude_new_message("C1", "100.1", "hi", "100.1"))

        client = d._app.client
        assert client.added == [
            {"channel": "C1", "name": "octagonal_sign", "timestamp": "100.1"}
        ]
        assert client.removed == [
            {"channel": "C1", "name": "octagonal_sign", "timestamp": "100.1"}
        ]

    def test_thread_reply_uses_reply_ts_as_trigger(self, monkeypatch):
        d = make_daemon(monkeypatch)

        async def fake_reply(channel, thread_ts, text):
            return "reply"

        async def fake_post(channel, thread_ts, text):
            return None

        monkeypatch.setattr(d._claude, "handle_thread_reply", fake_reply)
        monkeypatch.setattr(d, "_post_response", fake_post)

        # thread_ts (root) = "100.1", but the reply's own ts = "200.9"
        asyncio.run(d._handle_claude_thread_reply("C1", "100.1", "more", "200.9"))

        client = d._app.client
        assert client.added[0]["timestamp"] == "200.9"
        assert client.removed[0]["timestamp"] == "200.9"
        # reverse map links trigger_ts → thread_ts during the run
        # (cleared in finally; assert it was populated via the add side effect)

    def test_reaction_removed_even_on_error(self, monkeypatch):
        d = make_daemon(monkeypatch)

        async def boom(channel, message_ts, text):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(d._claude, "handle_message", boom)

        asyncio.run(d._handle_claude_new_message("C1", "100.1", "hi", "100.1"))

        assert d._app.client.removed[0]["timestamp"] == "100.1"


class TestReactionAddedHandler:
    def _event(self, reaction="octagonal_sign", user="U_human", ts="trig.1", channel="C1"):
        return {
            "reaction": reaction,
            "user": user,
            "item": {"type": "message", "channel": channel, "ts": ts},
        }

    def test_stops_run_and_posts_stopped_notice(self, monkeypatch):
        d = make_daemon(monkeypatch)
        d._bot_user_id = "U_bot"
        d._trigger_to_thread["trig.1"] = "thread.1"

        stopped = []

        async def fake_stop(thread_ts):
            stopped.append(thread_ts)
            return True

        monkeypatch.setattr(d._claude, "stop", fake_stop)

        asyncio.run(d._handle_reaction_added(self._event()))

        assert stopped == ["thread.1"]
        assert d._app.client.posted == [
            {"channel": "C1", "thread_ts": "thread.1", "text": "⏹️ Stopped."}
        ]

    def test_ignores_bot_own_reaction(self, monkeypatch):
        d = make_daemon(monkeypatch)
        d._bot_user_id = "U_bot"
        d._trigger_to_thread["trig.1"] = "thread.1"

        called = []
        monkeypatch.setattr(d._claude, "stop", lambda t: called.append(t))

        asyncio.run(d._handle_reaction_added(self._event(user="U_bot")))

        assert called == []
        assert d._app.client.posted == []

    def test_ignores_other_emoji(self, monkeypatch):
        d = make_daemon(monkeypatch)
        d._bot_user_id = "U_bot"
        d._trigger_to_thread["trig.1"] = "thread.1"

        called = []
        monkeypatch.setattr(d._claude, "stop", lambda t: called.append(t))

        asyncio.run(d._handle_reaction_added(self._event(reaction="thumbsup")))

        assert called == []
        assert d._app.client.posted == []

    def test_unknown_trigger_ts_is_noop(self, monkeypatch):
        d = make_daemon(monkeypatch)
        d._bot_user_id = "U_bot"
        # _trigger_to_thread empty → unknown ts

        called = []
        monkeypatch.setattr(d._claude, "stop", lambda t: called.append(t))

        asyncio.run(d._handle_reaction_added(self._event(ts="ghost.0")))

        assert called == []
        assert d._app.client.posted == []


class TestStoppedSuppression:
    def test_stopped_thread_skips_normal_reply(self, monkeypatch):
        d = make_daemon(monkeypatch)

        async def fake_handle_message(channel, message_ts, text):
            # Simulate the user having stopped this run mid-flight.
            d._claude._stopped.add(message_ts)
            return "partial reply that must NOT be posted"

        posted = []

        async def fake_post(channel, thread_ts, text):
            posted.append((thread_ts, text))

        monkeypatch.setattr(d._claude, "handle_message", fake_handle_message)
        monkeypatch.setattr(d, "_post_response", fake_post)

        asyncio.run(d._handle_claude_new_message("C1", "100.1", "hi", "100.1"))

        assert posted == []  # reply suppressed
        assert "100.1" not in d._claude._stopped  # flag cleared for next run

    def test_normal_run_still_posts(self, monkeypatch):
        d = make_daemon(monkeypatch)

        async def fake_handle_message(channel, message_ts, text):
            return "normal reply"

        posted = []

        async def fake_post(channel, thread_ts, text):
            posted.append((thread_ts, text))

        monkeypatch.setattr(d._claude, "handle_message", fake_handle_message)
        monkeypatch.setattr(d, "_post_response", fake_post)

        asyncio.run(d._handle_claude_new_message("C1", "100.1", "hi", "100.1"))

        assert posted == [("100.1", "normal reply")]

    def test_stopped_flag_cleared_even_when_run_raises(self, monkeypatch):
        # If the run is marked stopped and then raises before the suppression
        # check, the flag must still be cleared (in finally) so a LATER run on
        # the same thread_ts is not silently suppressed.
        d = make_daemon(monkeypatch)

        async def boom(channel, message_ts, text):
            d._claude._stopped.add(message_ts)
            raise RuntimeError("kaboom")

        monkeypatch.setattr(d._claude, "handle_message", boom)

        asyncio.run(d._handle_claude_new_message("C1", "100.1", "hi", "100.1"))

        assert "100.1" not in d._claude._stopped  # cleared despite the exception

    def test_stopped_thread_reply_skips_normal_reply(self, monkeypatch):
        # Mirror of the new-message suppression, for the thread-reply path
        # (keyed on thread_ts, with a distinct trigger_ts).
        d = make_daemon(monkeypatch)

        async def fake_reply(channel, thread_ts, text):
            d._claude._stopped.add(thread_ts)
            return "partial reply that must NOT be posted"

        posted = []

        async def fake_post(channel, thread_ts, text):
            posted.append((thread_ts, text))

        monkeypatch.setattr(d._claude, "handle_thread_reply", fake_reply)
        monkeypatch.setattr(d, "_post_response", fake_post)

        asyncio.run(d._handle_claude_thread_reply("C1", "100.1", "more", "200.9"))

        assert posted == []  # reply suppressed
        assert "100.1" not in d._claude._stopped  # flag cleared for next run
