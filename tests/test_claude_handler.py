"""Unit tests for src/claude_handler.py — Feature C process tracking + stop()."""

import asyncio
import signal

import claude_handler
from claude_handler import ClaudeHandler


class FakeProcess:
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
        proc = FakeProcess(pid=4242)
        h._processes["T1"] = proc

        sigkill_calls = []
        monkeypatch.setattr(claude_handler, "_descendant_pids", lambda pid: [])
        monkeypatch.setattr(claude_handler, "_sigkill", lambda pid: sigkill_calls.append(pid))

        result = asyncio.run(h.stop("T1"))

        assert result is True
        assert 4242 in sigkill_calls

    def test_stop_marks_thread_stopped(self, monkeypatch):
        h = make_handler()
        h._processes["T1"] = FakeProcess()

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

        proc = FakeProcess(pid=4242)
        claude_handler._kill_process_tree(proc)

        assert kill_order == [4242, 101, 102]

    def test_kill_tree_handles_no_descendants(self, monkeypatch):
        """When there are no descendants, _sigkill is called once with the parent pid."""
        sigkill_calls = []
        monkeypatch.setattr(claude_handler, "_descendant_pids", lambda pid: [])
        monkeypatch.setattr(claude_handler, "_sigkill", lambda pid: sigkill_calls.append(pid))

        proc = FakeProcess(pid=9999)
        claude_handler._kill_process_tree(proc)

        assert sigkill_calls == [9999]
        assert proc.killed is True

    def test_descendant_pids_empty_for_unknown_pid(self):
        """_descendant_pids returns [] for a pid that has no /proc entry — graceful degradation."""
        result = claude_handler._descendant_pids(2**30)
        assert result == []
