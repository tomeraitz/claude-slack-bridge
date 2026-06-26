"""Unit tests for src/crash_recovery.py — boot-time orphan kill + mechanical notice."""

import asyncio
from pathlib import Path

import session_store
import crash_recovery
from crash_recovery import recover_interrupted_runs, INTERRUPTED_NOTICE


class RecordingSlackClient:
    def __init__(self):
        self.posts: list[dict] = []

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True}


def _store(tmp_path) -> Path:
    return tmp_path / "data" / "sessions.json"


def _seed(path, **rec_overrides):
    rec = {"session_id": "sid", "cwd": "/c", "plugin_dir": None,
           "in_flight": True, "pid": 1234, "channel": "C1"}
    rec.update(rec_overrides)
    session_store.save({"T1": rec}, path)


class TestRecover:
    def test_kills_live_pid_and_posts_notice_and_clears(self, tmp_path):
        path = _store(tmp_path)
        _seed(path)
        killed = []
        client = RecordingSlackClient()

        recovered = asyncio.run(recover_interrupted_runs(
            client, store_path=path,
            is_alive=lambda pid: True,
            kill=lambda pid: killed.append(pid),
        ))

        assert recovered == ["T1"]
        assert killed == [1234]
        assert client.posts == [{
            "channel": "C1", "thread_ts": "T1", "text": INTERRUPTED_NOTICE,
        }]
        rec = session_store.load(path)["T1"]
        assert rec["in_flight"] is False
        assert rec["pid"] is None

    def test_dead_pid_not_killed_but_still_notified(self, tmp_path):
        path = _store(tmp_path)
        _seed(path)
        killed = []
        client = RecordingSlackClient()
        asyncio.run(recover_interrupted_runs(
            client, store_path=path,
            is_alive=lambda pid: False,
            kill=lambda pid: killed.append(pid),
        ))
        assert killed == []                       # nothing alive to kill
        assert len(client.posts) == 1             # but the user is still told
        assert session_store.load(path)["T1"]["in_flight"] is False

    def test_not_in_flight_is_ignored(self, tmp_path):
        path = _store(tmp_path)
        _seed(path, in_flight=False, pid=None)
        client = RecordingSlackClient()
        recovered = asyncio.run(recover_interrupted_runs(
            client, store_path=path, is_alive=lambda pid: True, kill=lambda pid: None,
        ))
        assert recovered == []
        assert client.posts == []

    def test_missing_channel_is_skipped_gracefully(self, tmp_path):
        path = _store(tmp_path)
        _seed(path, channel=None)
        client = RecordingSlackClient()
        recovered = asyncio.run(recover_interrupted_runs(
            client, store_path=path, is_alive=lambda pid: True, kill=lambda pid: None,
        ))
        # Still cleared so it won't re-trigger, but no post (no channel to post to).
        assert recovered == ["T1"]
        assert client.posts == []
        assert session_store.load(path)["T1"]["in_flight"] is False

    def test_null_pid_in_flight_notifies_without_kill(self, tmp_path):
        path = _store(tmp_path)
        _seed(path, pid=None)
        killed = []
        client = RecordingSlackClient()
        asyncio.run(recover_interrupted_runs(
            client, store_path=path,
            is_alive=lambda pid: True, kill=lambda pid: killed.append(pid),
        ))
        assert killed == []
        assert len(client.posts) == 1


def test_is_pid_alive_false_for_bogus_pid():
    # PID 0 / a huge unused pid should not be reported alive.
    assert crash_recovery.is_pid_alive(2**30) is False


class _InitAndPostClient(RecordingSlackClient):
    """Supports both ClaudeHandler.initialize() and the recovery notice post."""

    async def auth_test(self):
        return {"user_id": "UBOT"}

    async def conversations_list(self, **kwargs):
        return {"channels": []}

    async def conversations_replies(self, **kwargs):
        return {"messages": []}


def test_initialize_then_recover_end_to_end(tmp_path):
    """An interrupted run persisted to the store is loaded by ClaudeHandler.
    initialize() and then cleared + announced by recover_interrupted_runs —
    exercising both halves of the feature against one store file."""
    from claude_handler import ClaudeHandler

    path = _store(tmp_path)
    _seed(path, pid=999999)  # an interrupted run; pid is long dead
    client = _InitAndPostClient()

    handler = ClaudeHandler(client, store_path=path)
    asyncio.run(handler.initialize())
    # The persistence half restored the thread's session + config from disk.
    assert handler._sessions["T1"] == "sid"
    assert handler._thread_config["T1"] == ("/c", None)

    # The boot/recovery half announces the interruption and clears the flag.
    recovered = asyncio.run(recover_interrupted_runs(
        client, store_path=path, is_alive=lambda pid: False,
    ))
    assert recovered == ["T1"]
    assert client.posts == [{
        "channel": "C1", "thread_ts": "T1", "text": INTERRUPTED_NOTICE,
    }]
    assert session_store.load(path)["T1"]["in_flight"] is False
