"""Unit tests for src/session_broker.py — verifies the no-timeout, no-polling broker."""

import asyncio
import inspect

import pytest

import session_broker
from session_broker import SessionBroker


# ---------- fakes ----------

class FakeWriter:
    def __init__(self) -> None:
        self.written = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakeReader:
    def __init__(self, reply: bytes, delay: float = 0.0) -> None:
        self._reply = reply
        self._delay = delay

    async def readline(self) -> bytes:
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._reply


def patch_open_connection(monkeypatch, pairs):
    """Patch open_unix_connection to return successive (reader, writer) pairs."""
    queue = list(pairs)

    async def _open(_path):
        return queue.pop(0)

    # raising=False: open_unix_connection doesn't exist on Windows asyncio,
    # so monkeypatch can't find an existing attribute to replace.
    monkeypatch.setattr(
        session_broker.asyncio, "open_unix_connection", _open, raising=False
    )


def make_post_message(thread_ts: str):
    calls: list[tuple[str, str | None, str | None]] = []

    async def _post(text, existing_thread_ts, label):
        calls.append((text, existing_thread_ts, label))
        return thread_ts

    _post.calls = calls  # type: ignore[attr-defined]
    return _post


# ---------- send_and_wait behavior ----------

class TestSendAndWait:
    def test_returns_decoded_reply(self, monkeypatch):
        reader = FakeReader(b"hello back\n")
        writer = FakeWriter()
        patch_open_connection(monkeypatch, [(reader, writer)])
        post = make_post_message(thread_ts="1700000000.000100")

        broker = SessionBroker(post_message=post)
        reply = asyncio.run(broker.send_and_wait("hi"))

        assert reply == "hello back"

    def test_strips_trailing_newline_and_whitespace(self, monkeypatch):
        reader = FakeReader(b"  padded reply  \n")
        writer = FakeWriter()
        patch_open_connection(monkeypatch, [(reader, writer)])
        post = make_post_message(thread_ts="T")

        broker = SessionBroker(post_message=post)
        reply = asyncio.run(broker.send_and_wait("hi"))

        assert reply == "padded reply"

    def test_registers_thread_with_daemon(self, monkeypatch):
        reader = FakeReader(b"ok\n")
        writer = FakeWriter()
        patch_open_connection(monkeypatch, [(reader, writer)])
        post = make_post_message(thread_ts="T-123")

        broker = SessionBroker(post_message=post)
        asyncio.run(broker.send_and_wait("hi"))

        assert bytes(writer.written) == b"REGISTER T-123\n"

    def test_closes_writer_on_success(self, monkeypatch):
        reader = FakeReader(b"x\n")
        writer = FakeWriter()
        patch_open_connection(monkeypatch, [(reader, writer)])
        post = make_post_message(thread_ts="T")

        broker = SessionBroker(post_message=post)
        asyncio.run(broker.send_and_wait("hi"))

        assert writer.closed is True

    def test_reuses_thread_ts_on_subsequent_calls(self, monkeypatch):
        pairs = [
            (FakeReader(b"a\n"), FakeWriter()),
            (FakeReader(b"b\n"), FakeWriter()),
        ]
        patch_open_connection(monkeypatch, pairs)
        post = make_post_message(thread_ts="T-shared")
        broker = SessionBroker(post_message=post)

        async def two_calls():
            await broker.send_and_wait("first")
            await broker.send_and_wait("second")

        asyncio.run(two_calls())

        # First post starts a new thread; second post replies to the kept thread_ts.
        assert post.calls[0][1] is None
        assert post.calls[1][1] == "T-shared"

    def test_passes_label_to_post_message(self, monkeypatch):
        reader = FakeReader(b"ok\n")
        writer = FakeWriter()
        patch_open_connection(monkeypatch, [(reader, writer)])
        post = make_post_message(thread_ts="T")

        broker = SessionBroker(post_message=post)
        asyncio.run(broker.send_and_wait("hi", label="worktree-A"))

        assert post.calls[0][2] == "worktree-A"

    def test_delayed_reply_does_not_time_out(self, monkeypatch):
        """Confirms the timeout cap was removed: a slow reply still succeeds."""
        reader = FakeReader(b"slow\n", delay=0.5)
        writer = FakeWriter()
        patch_open_connection(monkeypatch, [(reader, writer)])
        post = make_post_message(thread_ts="T")

        broker = SessionBroker(post_message=post)
        reply = asyncio.run(broker.send_and_wait("hi"))

        assert reply == "slow"


# ---------- API/source regressions ----------

class TestNoTimeoutSurface:
    def test_init_rejects_timeout_minutes_kwarg(self):
        post = make_post_message(thread_ts="T")
        with pytest.raises(TypeError):
            SessionBroker(post_message=post, timeout_minutes=5)  # type: ignore[call-arg]

    def test_init_signature_has_no_timeout_param(self):
        sig = inspect.signature(SessionBroker.__init__)
        assert "timeout_minutes" not in sig.parameters
        assert "timeout" not in sig.parameters

    def test_source_has_no_wait_for_or_timeout_error(self):
        """Regression guard: indefinite block, no asyncio.wait_for wrapper."""
        src = inspect.getsource(session_broker)
        assert "wait_for" not in src
        assert "TimeoutError" not in src
