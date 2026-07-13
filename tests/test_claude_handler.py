"""Unit tests for src/claude_handler.py.

Covers two areas merged from parallel branches:
  * Feature C — in-flight process tracking + stop() / _kill_process_tree.
  * Store integration — session persistence + resume/scrape policy.
"""

import asyncio
import json
from pathlib import Path

import pytest

import claude_handler
import session_store
from claude_handler import ClaudeHandler


# ---------------------------------------------------------------------------
# Feature C — process tracking + stop()
# ---------------------------------------------------------------------------

class StopFakeProcess:
    """Stand-in for an asyncio subprocess: records kill(), reports a returncode."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.killed = False
        self.returncode = None

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


def make_handler() -> ClaudeHandler:
    # slack_client is unused by the methods under test here.
    return ClaudeHandler(slack_client=object())


class TestProcessTrackingState:
    def test_processes_map_exists_and_starts_empty(self):
        h = make_handler()
        assert h._processes == {}

    def test_stopped_set_exists_and_starts_empty(self):
        h = make_handler()
        assert h._stopped == set()


class TestStop:
    def test_stop_kills_parent_pid_via_sigkill_and_returns_true(self, monkeypatch):
        """stop() must kill the tracked process's pid (via _sigkill) and return True."""
        h = make_handler()
        proc = StopFakeProcess(pid=4242)
        h._processes["T1"] = proc

        sigkill_calls = []
        monkeypatch.setattr(claude_handler, "_descendant_pids", lambda pid: [])
        monkeypatch.setattr(claude_handler, "_sigkill", lambda pid: sigkill_calls.append(pid))

        result = asyncio.run(h.stop("T1"))

        assert result is True
        assert 4242 in sigkill_calls

    def test_stop_marks_thread_stopped(self, monkeypatch):
        h = make_handler()
        h._processes["T1"] = StopFakeProcess()

        monkeypatch.setattr(claude_handler, "_descendant_pids", lambda pid: [])
        monkeypatch.setattr(claude_handler, "_sigkill", lambda pid: None)

        asyncio.run(h.stop("T1"))

        assert "T1" in h._stopped

    def test_stop_unknown_thread_returns_false(self):
        h = make_handler()
        result = asyncio.run(h.stop("nope"))
        assert result is False

    def test_stop_unknown_thread_does_not_mark_stopped(self):
        h = make_handler()
        asyncio.run(h.stop("nope"))
        assert "nope" not in h._stopped


class TestKillProcessTree:
    def test_kill_tree_kills_parent_before_descendants(self, monkeypatch):
        """Parent pid must be killed before descendants (parent-first ordering)."""
        kill_order = []
        monkeypatch.setattr(claude_handler, "_descendant_pids", lambda pid: [101, 102])
        monkeypatch.setattr(claude_handler, "_sigkill", lambda pid: kill_order.append(pid))

        proc = StopFakeProcess(pid=4242)
        claude_handler._kill_process_tree(proc)

        assert kill_order == [4242, 101, 102]

    def test_kill_tree_handles_no_descendants(self, monkeypatch):
        """When there are no descendants, _sigkill is called once with the parent pid."""
        sigkill_calls = []
        monkeypatch.setattr(claude_handler, "_descendant_pids", lambda pid: [])
        monkeypatch.setattr(claude_handler, "_sigkill", lambda pid: sigkill_calls.append(pid))

        proc = StopFakeProcess(pid=9999)
        claude_handler._kill_process_tree(proc)

        assert sigkill_calls == [9999]
        assert proc.killed is True

    def test_descendant_pids_empty_for_unknown_pid(self):
        """_descendant_pids returns [] for a pid that has no /proc entry — graceful degradation."""
        result = claude_handler._descendant_pids(2**30)
        assert result == []


# ---------------------------------------------------------------------------
# Store integration — session persistence + resume/scrape policy
# ---------------------------------------------------------------------------

class FakeSlackClient:
    """Minimal async Slack client: auth_test + conversations_replies."""

    def __init__(self, bot_user_id="UBOT", replies=None):
        self._bot_user_id = bot_user_id
        self._replies = replies or []

    async def auth_test(self):
        return {"user_id": self._bot_user_id}

    async def conversations_list(self, **kwargs):
        return {"channels": []}

    async def conversations_replies(self, **kwargs):
        return {"messages": self._replies}


def _store(tmp_path) -> Path:
    return tmp_path / "data" / "sessions.json"


def _write_store(path: Path, data: dict) -> None:
    session_store.save(data, path)


class TestInitializeLoadsStore:
    def test_loads_sessions_and_thread_config(self, tmp_path):
        path = _store(tmp_path)
        _write_store(path, {
            "T1": {"session_id": "sid-1", "cwd": "/proj",
                   "plugin_dir": "/plug", "in_flight": False, "pid": None},
        })
        handler = ClaudeHandler(FakeSlackClient(), store_path=path)
        asyncio.run(handler.initialize())
        assert handler._sessions["T1"] == "sid-1"
        assert handler._thread_config["T1"] == ("/proj", "/plug")

    def test_empty_store_leaves_maps_empty(self, tmp_path):
        handler = ClaudeHandler(FakeSlackClient(), store_path=_store(tmp_path))
        asyncio.run(handler.initialize())
        assert handler._sessions == {}
        assert handler._thread_config == {}


class TestHandleMessagePersists:
    def test_new_message_writes_record(self, tmp_path, monkeypatch):
        path = _store(tmp_path)
        handler = ClaudeHandler(FakeSlackClient(), store_path=path)
        asyncio.run(handler.initialize())

        async def fake_run(cmd, prompt, cwd=None, thread_ts=None, channel=None, progress_cb=None):
            return "reply"
        monkeypatch.setattr(handler, "_run_claude", fake_run)

        asyncio.run(handler.handle_message("C1", "T2", "hello"))

        rec = json.loads(path.read_text())["T2"]
        assert rec["session_id"] == handler._sessions["T2"]
        assert rec["session_id"]  # a real uuid was minted and stored


class FakeProcess:
    """Stand-in for asyncio subprocess used by _run_claude."""

    def __init__(self, lines, returncode=0, pid=4321):
        self.pid = pid
        self.returncode = returncode
        self._lines = lines
        self.stdin = self._Stdin()
        self.stdout = self._aiter(lines)
        self.stderr = self._aiter([])

    class _Stdin:
        def write(self, data): pass
        async def drain(self): pass
        def close(self): pass

    @staticmethod
    def _aiter(lines):
        class _It:
            def __init__(self, items): self._items = list(items)
            def __aiter__(self): return self
            async def __anext__(self):
                if not self._items:
                    raise StopAsyncIteration
                return self._items.pop(0)
        return _It(lines)

    async def wait(self):
        return self.returncode

    def kill(self): pass


def _result_line(text="done"):
    return (json.dumps({"type": "result", "result": text}) + "\n").encode()


def _patch_subprocess(monkeypatch, proc):
    async def _create(*args, **kwargs):
        return proc
    monkeypatch.setattr(
        "claude_handler.asyncio.create_subprocess_exec", _create
    )


class TestRunClaudePersistsLifecycle:
    def test_marks_in_flight_then_clears_on_clean_finish(self, tmp_path, monkeypatch):
        path = _store(tmp_path)
        handler = ClaudeHandler(FakeSlackClient(), store_path=path)
        asyncio.run(handler.initialize())
        proc = FakeProcess([_result_line("hi")], returncode=0, pid=7777)
        _patch_subprocess(monkeypatch, proc)

        reply = asyncio.run(
            handler._run_claude(["claude"], "p", cwd="/c", thread_ts="T3")
        )
        assert reply == "hi"
        rec = json.loads(path.read_text())["T3"]
        assert rec["in_flight"] is False
        assert rec["pid"] is None

    def test_clears_in_flight_on_nonzero_rc(self, tmp_path, monkeypatch):
        # A run that exits non-zero has TERMINATED (not interrupted): the daemon
        # survived and replied, so in_flight/pid must be cleared. Leaving them set
        # would trigger a spurious "bridge restarted" notice on the next boot.
        path = _store(tmp_path)
        handler = ClaudeHandler(FakeSlackClient(), store_path=path)
        asyncio.run(handler.initialize())
        proc = FakeProcess([], returncode=1, pid=8888)
        _patch_subprocess(monkeypatch, proc)

        reply = asyncio.run(
            handler._run_claude(["claude"], "p", cwd="/c", thread_ts="T4")
        )
        assert reply == "Sorry, I encountered an error processing your request."
        rec = json.loads(path.read_text())["T4"]
        assert rec["in_flight"] is False
        assert rec["pid"] is None

    def test_clears_in_flight_on_no_result(self, tmp_path, monkeypatch):
        # Exited cleanly (rc=0) but emitted no result event — still terminated,
        # so the run must be marked finished.
        path = _store(tmp_path)
        handler = ClaudeHandler(FakeSlackClient(), store_path=path)
        asyncio.run(handler.initialize())
        proc = FakeProcess([], returncode=0, pid=9999)
        _patch_subprocess(monkeypatch, proc)

        reply = asyncio.run(
            handler._run_claude(["claude"], "p", cwd="/c", thread_ts="T5")
        )
        assert reply == "Sorry, I couldn't parse the response."
        rec = json.loads(path.read_text())["T5"]
        assert rec["in_flight"] is False
        assert rec["pid"] is None

    def test_signal_kill_returns_interrupted_reply_not_generic_error(self, tmp_path, monkeypatch):
        # A process killed by a signal (returncode < 0, e.g. SIGKILL=-9) must
        # return _INTERRUPTED_REPLY, NOT the generic error sentinel. Signal kills
        # are not transient failures so they must not be retried by the resume policy.
        path = _store(tmp_path)
        handler = ClaudeHandler(FakeSlackClient(), store_path=path)
        asyncio.run(handler.initialize())
        proc = FakeProcess([], returncode=-9, pid=1111)
        _patch_subprocess(monkeypatch, proc)

        reply = asyncio.run(
            handler._run_claude(["claude"], "p", cwd="/c", thread_ts="T6")
        )
        assert reply == claude_handler._INTERRUPTED_REPLY
        assert reply != "Sorry, I encountered an error processing your request."
        rec = json.loads(path.read_text())["T6"]
        assert rec["in_flight"] is False
        assert rec["pid"] is None

    def test_nonzero_positive_rc_still_returns_generic_error(self, tmp_path, monkeypatch):
        # rc=1 (non-signal failure) must still return the generic error sentinel
        # so the resume policy can retry it.
        path = _store(tmp_path)
        handler = ClaudeHandler(FakeSlackClient(), store_path=path)
        asyncio.run(handler.initialize())
        proc = FakeProcess([], returncode=1, pid=2222)
        _patch_subprocess(monkeypatch, proc)

        reply = asyncio.run(
            handler._run_claude(["claude"], "p", cwd="/c", thread_ts="T7")
        )
        assert reply == "Sorry, I encountered an error processing your request."
        assert reply != claude_handler._INTERRUPTED_REPLY


class TestJsonlPath:
    def test_encodes_cwd_with_dashes(self):
        p = claude_handler._jsonl_path("/home/node/proj/x", "sid-1")
        assert p.name == "sid-1.jsonl"
        assert p.parent.name == "-home-node-proj-x"
        assert p.parent.parent.name == "projects"


class _SpyHandler(ClaudeHandler):
    """Records every _run_claude call and returns scripted replies."""

    def __init__(self, *a, replies, **k):
        super().__init__(*a, **k)
        self.runs: list[dict] = []
        self._scripted = list(replies)

    async def _run_claude(self, cmd, prompt, cwd=None, thread_ts=None, channel=None, progress_cb=None):
        self.runs.append({"cmd": cmd, "prompt": prompt, "cwd": cwd, "thread_ts": thread_ts})
        return self._scripted.pop(0)


class TestResumePolicy:
    def _handler(self, tmp_path, replies, **kw):
        h = _SpyHandler(FakeSlackClient(**kw), store_path=_store(tmp_path), replies=replies)
        asyncio.run(h.initialize())
        return h

    def test_resume_hot_path_when_jsonl_exists(self, tmp_path, monkeypatch):
        h = self._handler(tmp_path, replies=["resumed-ok"])
        h._sessions["T1"] = "sid-1"
        h._thread_config["T1"] = ("/c", None)
        monkeypatch.setattr(claude_handler, "_jsonl_path",
                            lambda cwd, sid: Path("/exists"))
        monkeypatch.setattr(claude_handler.Path, "exists", lambda self: True)
        reply = asyncio.run(h.handle_thread_reply("C", "T1", "hi"))
        assert reply == "resumed-ok"
        assert len(h.runs) == 1
        assert "--resume" in h.runs[0]["cmd"]

    def test_resume_retries_once_then_scrapes(self, tmp_path, monkeypatch):
        # resume fails twice -> scrape succeeds (3 runs total)
        fail = "Sorry, I encountered an error processing your request."
        h = self._handler(tmp_path, replies=[fail, fail, "scraped-ok"])
        h._sessions["T1"] = "sid-1"
        h._thread_config["T1"] = ("/c", None)
        monkeypatch.setattr(claude_handler.Path, "exists", lambda self: True)
        reply = asyncio.run(h.handle_thread_reply("C", "T1", "hi"))
        assert reply == "scraped-ok"
        assert len(h.runs) == 3
        # First two runs resume the old id; the third mints a NEW --session-id.
        assert "--resume" in h.runs[0]["cmd"]
        assert "--resume" in h.runs[1]["cmd"]
        assert "--session-id" in h.runs[2]["cmd"]
        assert "--resume" not in h.runs[2]["cmd"]

    def test_no_session_scrapes_and_persists_new_id(self, tmp_path):
        h = self._handler(tmp_path, replies=["scraped-ok"])
        h._thread_config["T1"] = ("/c", None)  # known cwd, but no session_id
        reply = asyncio.run(h.handle_thread_reply("C", "T1", "hi"))
        assert reply == "scraped-ok"
        assert len(h.runs) == 1
        assert "--session-id" in h.runs[0]["cmd"]
        new_id = h._sessions["T1"]
        assert new_id  # captured in memory
        assert json.loads(_store(tmp_path).read_text())["T1"]["session_id"] == new_id

    def test_session_present_but_jsonl_missing_scrapes(self, tmp_path, monkeypatch):
        h = self._handler(tmp_path, replies=["scraped-ok"])
        h._sessions["T1"] = "sid-gone"
        h._thread_config["T1"] = ("/c", None)
        monkeypatch.setattr(claude_handler.Path, "exists", lambda self: False)
        reply = asyncio.run(h.handle_thread_reply("C", "T1", "hi"))
        assert reply == "scraped-ok"
        assert "--session-id" in h.runs[0]["cmd"]

    def test_interrupted_reply_is_not_retried(self, tmp_path, monkeypatch):
        # When _run_claude returns _INTERRUPTED_REPLY (signal kill), the resume
        # policy must return it immediately — exactly ONE _run_claude call, no
        # retry, no scrape. Signal kills are not transient failures.
        h = self._handler(tmp_path, replies=[claude_handler._INTERRUPTED_REPLY])
        h._sessions["T1"] = "sid-1"
        h._thread_config["T1"] = ("/c", None)
        monkeypatch.setattr(claude_handler.Path, "exists", lambda self: True)
        reply = asyncio.run(h.handle_thread_reply("C", "T1", "hi"))
        assert reply == claude_handler._INTERRUPTED_REPLY
        assert len(h.runs) == 1  # no retry, no scrape
