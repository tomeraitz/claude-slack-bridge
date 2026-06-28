"""Integration tests for _run_claude's idle watchdog + live progress.

A fake subprocess feeds scripted stream-json lines (or hangs) so we can assert:
  * a streaming run returns the final result AND drives the progress callback,
    finishing with a done=True summary snapshot;
  * a run with no activity is killed by the idle watchdog and surfaces the
    (non-retryable) timeout reply;
  * the public entrypoints forward progress_cb down to _run_claude.
No real ``claude`` binary is involved.
"""

import asyncio
import json

import pytest

import claude_handler
from claude_handler import ClaudeHandler, _TIMEOUT_REPLY


# --- fake asyncio subprocess ----------------------------------------------

class _FakeStdin:
    def write(self, _data): pass
    async def drain(self): pass
    def close(self): pass


class _FakeStream:
    """Async-iterable byte-line stream. Drains scripted lines, then ends —
    or, in ``hang`` mode, blocks until the process is killed."""

    def __init__(self, lines, delay, hang, stop, exhausted):
        self._lines = list(lines)
        self._delay = delay
        self._hang = hang
        self._stop = stop
        self._exhausted = exhausted

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._stop.is_set():
            raise StopAsyncIteration
        if self._lines:
            if self._delay:
                await asyncio.sleep(self._delay)
            if self._stop.is_set():
                raise StopAsyncIteration
            return self._lines.pop(0)
        if self._hang:
            await self._stop.wait()
            raise StopAsyncIteration
        self._exhausted.set()
        raise StopAsyncIteration


class _FakeProcess:
    def __init__(self, *, stdout_lines=(), delay=0.0, hang=False, finish_rc=0):
        self.pid = 999999
        self.returncode = None
        self._stop = asyncio.Event()
        self._exhausted = asyncio.Event()
        self._finish_rc = finish_rc
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(stdout_lines, delay, hang, self._stop, self._exhausted)
        self.stderr = _FakeStream((), 0.0, False, self._stop, asyncio.Event())

    async def wait(self):
        stop = asyncio.ensure_future(self._stop.wait())
        done = asyncio.ensure_future(self._exhausted.wait())
        await asyncio.wait({stop, done}, return_when=asyncio.FIRST_COMPLETED)
        stop.cancel()
        done.cancel()
        self.returncode = -9 if self._stop.is_set() else self._finish_rc
        return self.returncode

    def kill(self):
        self._stop.set()


def _line(event: dict) -> bytes:
    return (json.dumps(event) + "\n").encode()


def _patch_spawn(monkeypatch, fake: _FakeProcess) -> None:
    async def fake_exec(*_args, **_kwargs):
        return fake
    monkeypatch.setattr(claude_handler.asyncio, "create_subprocess_exec", fake_exec)


def _handler(tmp_path) -> ClaudeHandler:
    return ClaudeHandler(slack_client=object(), store_path=tmp_path / "sessions.json")


# --- tests -----------------------------------------------------------------

def test_streaming_run_returns_result_and_drives_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_handler, "PROGRESS_INTERVAL", 0.02)
    monkeypatch.setattr(claude_handler, "IDLE_TIMEOUT", 1000.0)

    lines = [
        _line({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/a/session.py"}}]}}),
        _line({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]}}),
        _line({"type": "result", "result": "All done!"}),
    ]
    fake = _FakeProcess(stdout_lines=lines, delay=0.03, finish_rc=0)
    _patch_spawn(monkeypatch, fake)

    snaps = []
    async def progress_cb(p):
        snaps.append(p)

    handler = _handler(tmp_path)
    reply = asyncio.run(handler._run_claude(
        ["claude"], "hi", cwd=None, thread_ts=None, channel=None, progress_cb=progress_cb,
    ))

    assert reply == "All done!"
    assert snaps, "progress_cb should have been called at least once"
    assert snaps[-1].done is True                      # final summary snapshot
    assert "✅ Done" in snaps[-1].summary
    assert "session.py" in snaps[-1].summary


def test_idle_run_is_killed_and_returns_timeout_reply(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_handler, "PROGRESS_INTERVAL", 0.02)
    monkeypatch.setattr(claude_handler, "IDLE_TIMEOUT", 0.05)

    fake = _FakeProcess(hang=True)  # never emits an event, never exits on its own
    _patch_spawn(monkeypatch, fake)

    handler = _handler(tmp_path)
    reply = asyncio.run(handler._run_claude(
        ["claude"], "hi", cwd=None, thread_ts=None, channel=None,
    ))

    assert reply == _TIMEOUT_REPLY
    assert fake._stop.is_set()  # the watchdog killed it


def test_short_run_emits_no_progress(tmp_path, monkeypatch):
    # Finishes before the first throttle tick -> no status message created.
    monkeypatch.setattr(claude_handler, "PROGRESS_INTERVAL", 5.0)
    monkeypatch.setattr(claude_handler, "IDLE_TIMEOUT", 1000.0)

    lines = [_line({"type": "result", "result": "quick"})]
    fake = _FakeProcess(stdout_lines=lines, delay=0.0, finish_rc=0)
    _patch_spawn(monkeypatch, fake)

    calls = []
    async def progress_cb(p):
        calls.append(p)

    handler = _handler(tmp_path)
    reply = asyncio.run(handler._run_claude(
        ["claude"], "hi", cwd=None, thread_ts=None, channel=None, progress_cb=progress_cb,
    ))

    assert reply == "quick"
    assert calls == []  # silent: no flush, no final summary


def test_handle_message_forwards_progress_cb(tmp_path, monkeypatch):
    handler = _handler(tmp_path)
    captured = {}

    async def fake_run(cmd, prompt, cwd=None, thread_ts=None, channel=None, progress_cb=None):
        captured["progress_cb"] = progress_cb
        return "ok"

    monkeypatch.setattr(handler, "_run_claude", fake_run)

    async def cb(_p): ...
    reply = asyncio.run(handler.handle_message("C1", "T1", "do it", None, progress_cb=cb))

    assert reply == "ok"
    assert captured["progress_cb"] is cb
