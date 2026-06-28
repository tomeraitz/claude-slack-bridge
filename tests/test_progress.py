"""Unit tests for live-progress rendering (tracker, reporter, duration fmt).

These cover the pure, deterministic pieces: the tracker folds claude stream
events into a snapshot with zero clock/IO of its own, and the daemon reporter
posts/edits/deletes exactly one Slack status message. No subprocess involved.
"""

import asyncio

import pytest

from claude_handler import _fmt_duration, _Progress, _ProgressTracker
from slack_daemon import _ProgressReporter


# --- helpers to build CLI stream-json events -------------------------------

def _assistant(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"content": list(blocks)}}


def _tool(name: str, **inp) -> dict:
    return {"type": "tool_use", "name": name, "input": inp}


def _text(t: str) -> dict:
    return {"type": "text", "text": t}


def _thinking(t: str) -> dict:
    return {"type": "thinking", "thinking": t}


# --- _fmt_duration ---------------------------------------------------------

@pytest.mark.parametrize("seconds,expected", [
    (0, "0s"), (5, "5s"), (59, "59s"),
    (60, "1m00s"), (74, "1m14s"), (599, "9m59s"),
    (3600, "1h00m"), (3661, "1h01m"),
])
def test_fmt_duration(seconds, expected):
    assert _fmt_duration(seconds) == expected


# --- _ProgressTracker ------------------------------------------------------

def test_tracker_edit_sets_action_file_and_tally():
    t = _ProgressTracker(start=0.0)
    t.ingest(_assistant(_tool("Edit", file_path="/proj/src/session.py")))
    snap = t.snapshot(now=5.0)
    assert "✏️ Editing `session.py`" in snap.live
    assert "1 file" in snap.live
    assert "1 tool" in snap.live
    assert "5s" in snap.live


def test_tracker_counts_unique_files_only():
    t = _ProgressTracker(start=0.0)
    t.ingest(_assistant(_tool("Edit", file_path="/a/x.py")))
    t.ingest(_assistant(_tool("Write", file_path="/a/y.py")))
    t.ingest(_assistant(_tool("Edit", file_path="/a/x.py")))  # repeat
    snap = t.snapshot(now=1.0)
    assert "2 files" in snap.live  # x.py counted once
    assert "3 tools" in snap.live


def test_tracker_dirty_lifecycle():
    t = _ProgressTracker(start=0.0)
    assert t.dirty is False                       # nothing yet
    t.ingest(_assistant(_tool("Read", file_path="/a/b.py")))
    assert t.dirty is True                        # event arrived
    t.snapshot(now=1.0)
    assert t.dirty is False                       # consumed by snapshot
    t.ingest({"type": "system", "subtype": "init"})  # non-activity event
    assert t.dirty is False


def test_tracker_text_and_thinking_update_action():
    t = _ProgressTracker(start=0.0)
    t.ingest(_assistant(_thinking("let me look at this")))
    assert "🤔 Thinking" in t.snapshot(now=1.0).live
    t.ingest(_assistant(_text("Now I'll refactor the handler\nsecond line")))
    live = t.snapshot(now=2.0).live
    assert "💬 Now I'll refactor the handler" in live
    assert "second line" not in live  # only the first line is shown


def test_tracker_summary_lists_changes_reads_and_commands():
    t = _ProgressTracker(start=0.0)
    t.ingest(_assistant(_tool("Edit", file_path="/a/session.py")))
    t.ingest(_assistant(_tool("Read", file_path="/a/config.py")))
    t.ingest(_assistant(_tool("Bash", command="pytest -q\necho done")))
    snap = t.snapshot(now=134.0, done=True)
    assert snap.done is True
    assert snap.summary.startswith("✅ Done · 1 file changed · 3 tools · 2m14s")
    assert "Changed: session.py" in snap.summary
    assert "Read 1 file" in snap.summary
    assert "Ran: pytest -q" in snap.summary  # only first line of the command
    assert "```" in snap.summary             # detail block is fenced (auto-collapses)


def test_tracker_summary_no_detail_block_when_nothing_tracked():
    t = _ProgressTracker(start=0.0)
    t.ingest(_assistant(_thinking("hmm")))
    summary = t.snapshot(now=3.0, done=True).summary
    assert summary == "✅ Done · 0 tools · 3s"  # headline only, no fence
    assert "```" not in summary


# --- _ProgressReporter (daemon side) ---------------------------------------

class _FakeClient:
    def __init__(self):
        self.posts: list[dict] = []
        self.updates: list[dict] = []
        self.deletes: list[dict] = []

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ts": "1700000000.0001"}

    async def chat_update(self, **kwargs):
        self.updates.append(kwargs)

    async def chat_delete(self, **kwargs):
        self.deletes.append(kwargs)


def test_reporter_creates_then_edits_one_message():
    client = _FakeClient()
    reporter = _ProgressReporter(client, channel="C1", thread_ts="T1")

    asyncio.run(reporter(_Progress(live="🔄 step 1", summary="s", done=False)))
    asyncio.run(reporter(_Progress(live="🔄 step 2", summary="s", done=False)))

    assert len(client.posts) == 1                       # only ONE new message
    assert client.posts[0]["text"] == "🔄 step 1"
    assert client.posts[0]["thread_ts"] == "T1"
    assert len(client.updates) == 1                      # second was an in-place edit
    assert client.updates[0]["text"] == "🔄 step 2"
    assert client.updates[0]["ts"] == "1700000000.0001"


def test_reporter_done_snapshot_renders_summary():
    client = _FakeClient()
    reporter = _ProgressReporter(client, channel="C1", thread_ts="T1")
    asyncio.run(reporter(_Progress(live="🔄 working", summary="✅ done", done=False)))
    asyncio.run(reporter(_Progress(live="🔄 working", summary="✅ done", done=True)))
    assert client.updates[-1]["text"] == "✅ done"       # summary, not live


def test_reporter_delete_removes_posted_message():
    client = _FakeClient()
    reporter = _ProgressReporter(client, channel="C1", thread_ts="T1")
    asyncio.run(reporter(_Progress(live="🔄 working", summary="s", done=False)))
    asyncio.run(reporter.delete())
    assert client.deletes == [{"channel": "C1", "ts": "1700000000.0001"}]


def test_reporter_delete_is_noop_when_never_posted():
    client = _FakeClient()
    reporter = _ProgressReporter(client, channel="C1", thread_ts="T1")
    asyncio.run(reporter.delete())  # short run never posted a status
    assert client.deletes == []
    assert client.posts == []
