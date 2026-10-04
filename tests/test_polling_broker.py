"""Unit tests for src/polling_broker.py — the daemon-free, polling broker."""

import asyncio

import pytest
from slack_sdk.errors import SlackApiError

import polling_broker
from polling_broker import PollingBroker


class FakeResponse:
    def __init__(self, status_code: int, headers: dict | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}


class FakeClient:
    """Records posts; serves queued conversations.replies results (or exceptions)."""

    def __init__(self, replies: list | None = None, channel_id: str = "C123") -> None:
        self.posts: list[dict] = []
        self.reply_calls: list[dict] = []
        self._replies = list(replies or [])
        self._channel_id = channel_id
        self._next_ts = 1700000000.0

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        self._next_ts += 1
        return {"ok": True, "ts": f"{self._next_ts:.6f}", "channel": self._channel_id}

    async def conversations_replies(self, **kwargs):
        self.reply_calls.append(kwargs)
        item = self._replies.pop(0) if self._replies else []
        if isinstance(item, Exception):
            raise item
        return {"ok": True, "messages": item}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    sleeps: list[float] = []

    async def _sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(polling_broker.asyncio, "sleep", _sleep)
    return sleeps


QUESTION_TS = "1700000001.000000"  # ts FakeClient assigns to the first post


class TestSendAndWait:
    def test_returns_first_human_reply(self):
        client = FakeClient(replies=[[{"ts": "1700000005.0", "user": "U1", "text": "yes"}]])
        broker = PollingBroker(client, "#dev")

        assert asyncio.run(broker.send_and_wait("deploy?")) == "yes"
        assert broker.thread_ts == QUESTION_TS

    def test_polls_until_reply_arrives(self, no_sleep):
        client = FakeClient(replies=[[], [], [{"ts": "1700000005.0", "user": "U1", "text": "ok"}]])
        broker = PollingBroker(client, "#dev", interval=2.5)

        assert asyncio.run(broker.send_and_wait("q")) == "ok"
        assert len(client.reply_calls) == 3
        assert no_sleep == [2.5, 2.5]

    def test_ignores_bot_and_system_messages(self):
        client = FakeClient(replies=[[
            {"ts": "1700000002.0", "bot_id": "B1", "text": "bot noise"},
            {"ts": "1700000003.0", "user": "U1", "subtype": "channel_join", "text": "joined"},
            {"ts": "1700000004.0", "user": "U1", "text": "real answer"},
        ]])
        assert asyncio.run(PollingBroker(client, "#dev").send_and_wait("q")) == "real answer"

    def test_ignores_messages_at_or_before_the_question(self):
        client = FakeClient(replies=[[
            {"ts": QUESTION_TS, "user": "U1", "text": "the question itself"},
            {"ts": "1700000000.5", "user": "U1", "text": "old answer"},
            {"ts": "1700000009.0", "user": "U1", "text": "new answer"},
        ]])
        assert asyncio.run(PollingBroker(client, "#dev").send_and_wait("q")) == "new answer"

    def test_polls_with_channel_id_and_thread(self):
        client = FakeClient(replies=[[{"ts": "1700000005.0", "user": "U1", "text": "y"}]])
        asyncio.run(PollingBroker(client, "#dev").send_and_wait("q"))

        call = client.reply_calls[0]
        assert call["channel"] == "C123"  # resolved from the post, not "#dev"
        assert call["ts"] == QUESTION_TS
        assert call["oldest"] == QUESTION_TS
        assert call["inclusive"] is False

    def test_follow_up_stays_in_thread_and_waits_for_a_newer_reply(self):
        client = FakeClient(replies=[
            [{"ts": "1700000001.5", "user": "U1", "text": "first"}],
            [{"ts": "1700000003.0", "user": "U1", "text": "second"}],
        ])
        broker = PollingBroker(client, "#dev")
        assert asyncio.run(broker.send_and_wait("q1")) == "first"
        assert asyncio.run(broker.send_and_wait("q2")) == "second"

        assert "thread_ts" not in client.posts[0]
        assert client.posts[1]["thread_ts"] == QUESTION_TS
        assert client.reply_calls[1]["oldest"] == "1700000002.000000"

    def test_continues_an_existing_thread(self):
        client = FakeClient(replies=[[{"ts": "1700000005.0", "user": "U1", "text": "y"}]])
        broker = PollingBroker(client, "#dev", thread_ts="1699999999.000000")
        asyncio.run(broker.send_and_wait("q"))
        assert client.posts[0]["thread_ts"] == "1699999999.000000"

    def test_describes_attached_files(self):
        client = FakeClient(replies=[[{
            "ts": "1700000005.0", "user": "U1", "subtype": "file_share",
            "text": "see this", "files": [{"name": "shot.png"}],
        }]])
        reply = asyncio.run(PollingBroker(client, "#dev").send_and_wait("q"))
        assert reply == "see this\n[attached: shot.png]"

    def test_retries_after_rate_limit(self, no_sleep):
        limited = SlackApiError("ratelimited", FakeResponse(429, {"Retry-After": "7"}))
        client = FakeClient(replies=[limited, [{"ts": "1700000005.0", "user": "U1", "text": "y"}]])

        assert asyncio.run(PollingBroker(client, "#dev").send_and_wait("q")) == "y"
        assert no_sleep == [7.0]

    def test_other_api_errors_propagate(self):
        err = SlackApiError("not_in_channel", FakeResponse(200))
        client = FakeClient(replies=[err])
        with pytest.raises(SlackApiError):
            asyncio.run(PollingBroker(client, "#dev").send_and_wait("q"))


class TestSendOnly:
    def test_posts_without_polling(self):
        client = FakeClient()
        broker = PollingBroker(client, "#dev")

        assert asyncio.run(broker.send_only("done")) == "Notification sent."
        assert client.reply_calls == []
        assert broker.thread_ts == QUESTION_TS

    def test_label_only_on_first_post(self):
        client = FakeClient()
        broker = PollingBroker(client, "#dev")
        asyncio.run(broker.send_only("one", label="wt-a"))
        asyncio.run(broker.send_only("two", label="wt-a"))

        assert "[wt-a]" in str(client.posts[0]["blocks"])
        assert "[wt-a]" not in str(client.posts[1]["blocks"])

    def test_post_failure_raises(self):
        class FailingClient(FakeClient):
            async def chat_postMessage(self, **kwargs):
                return {"ok": False, "error": "channel_not_found"}

        with pytest.raises(RuntimeError, match="channel_not_found"):
            asyncio.run(PollingBroker(FailingClient(), "#nope").send_only("x"))
