"""Unit tests for live-progress rendering (tracker, reporter, duration fmt).

These cover the pure, deterministic pieces: the tracker folds claude stream
events into a snapshot with zero clock/IO of its own, and the daemon reporter
posts/edits/deletes exactly one Slack status message. No subprocess involved.
"""

import asyncio

import pytest

import claude_handler
from claude_handler import (
    _DEFAULT_CONTEXT, _fmt_duration, _model_context, _Progress, _ProgressTracker,
)
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

def test_tracker_edit_action_in_live_files_block_breakdown_in_meta():
    t = _ProgressTracker(start=0.0)
    t.ingest(_assistant(_tool("Edit", file_path="/proj/src/session.py")))
    snap = t.snapshot(now=5.0)
    assert "✏️ Editing `session.py`" in snap.live   # action line
    assert "1 file" in snap.files and "session.py" in snap.files  # own files section
    assert "1 edit" in snap.meta                     # per-tool breakdown in muted line
    assert "5s" in snap.meta


def test_tracker_counts_unique_files_only():
    t = _ProgressTracker(start=0.0)
    t.ingest(_assistant(_tool("Edit", file_path="/a/x.py")))
    t.ingest(_assistant(_tool("Write", file_path="/a/y.py")))
    t.ingest(_assistant(_tool("Edit", file_path="/a/x.py")))  # repeat
    snap = t.snapshot(now=1.0)
    assert "2 files" in snap.files   # x.py counted once, in the files section
    assert "2 edit" in snap.meta and "1 write" in snap.meta  # per-tool breakdown


def test_tracker_files_block_shows_per_file_churn():
    t = _ProgressTracker(start=0.0)
    t.ingest(_assistant(_tool("Edit", file_path="/a/x.py",
                              old_string="a\nb", new_string="a\nB\nC")))
    t.ingest(_assistant(_tool("Write", file_path="/a/y.py", content="one\ntwo\nthree")))
    files = t.snapshot(now=1.0).files
    assert "📝 *2 files changed*" in files
    assert "`x.py`  +3/−2" in files   # per-file churn (Edit)
    assert "`y.py`  +3/−0" in files   # Write counts content lines as additions


def test_actions_show_rolling_history_newest_first():
    # All actions so far are shown (under the cap), newest first — not just the
    # current update window.
    t = _ProgressTracker(start=0.0)
    for f in ("one", "two", "three", "four", "five"):
        t.ingest(_assistant(_tool("Read", file_path=f"/a/{f}.py")))
    lines = t.snapshot(now=1.0).live.splitlines()
    assert len(lines) == 5
    assert "five.py" in lines[0]    # newest on top
    assert "one.py" in lines[-1]    # oldest at the bottom


def test_actions_persist_across_snapshots():
    # A later snapshot still shows the earlier actions (rolling history),
    # not just what happened since the previous snapshot.
    t = _ProgressTracker(start=0.0)
    for f in ("one", "two", "three", "four", "five"):
        t.ingest(_assistant(_tool("Read", file_path=f"/a/{f}.py")))
    t.snapshot(now=1.0)  # first update
    t.ingest(_assistant(_tool("Read", file_path="/a/six.py")))
    lines = t.snapshot(now=2.0).live.splitlines()
    assert len(lines) == 6           # all six, not just the one new action
    assert "six.py" in lines[0]      # newest on top
    assert "one.py" in lines[-1]


def test_actions_are_numbered_highest_on_top():
    t = _ProgressTracker(start=0.0)
    for f in ("a", "b", "c"):
        t.ingest(_assistant(_tool("Read", file_path=f"/a/{f}.py")))
    lines = t.snapshot(now=1.0).live.splitlines()
    assert lines[0].startswith("`3.`") and "c.py" in lines[0]   # newest = highest number
    assert lines[1].startswith("`2.`")
    assert lines[2].startswith("`1.`") and "a.py" in lines[2]


def test_long_narration_is_clipped_with_ellipsis():
    t = _ProgressTracker(start=0.0)
    long = "Now it's clear and I owe you a correction so " * 12  # > 400 chars
    t.ingest(_assistant(_text(long)))
    line = t.snapshot(now=1.0).live.splitlines()[0]
    assert "💬 " in line             # numbered prefix then the narration
    assert line.endswith("…")        # clearly truncated, not cut mid-word silently
    assert not line.endswith(" …")   # trimmed on a word boundary


def test_tracker_tracks_line_churn_and_errors():
    t = _ProgressTracker(start=0.0)
    t.ingest(_assistant(_tool("Edit", file_path="/a/x.py",
                              old_string="a\nb\nc", new_string="a\nB")))
    t.ingest({"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": True, "content": "boom"}]}})
    snap = t.snapshot(now=1.0)
    assert "+2/−3" in snap.files         # churn shown in the files section
    assert "⚠️ 1 error: boom" in snap.meta  # error count + last error text
    assert "+2/−3" in snap.summary       # also surfaced in the final summary


def test_tracker_tokens_turns_model_and_ctx():
    t = _ProgressTracker(start=0.0)
    t.ingest({"type": "system", "subtype": "init", "model": "claude-opus-4-8"})
    usage = {"input_tokens": 1000, "output_tokens": 500,
             "cache_read_input_tokens": 99000, "cache_creation_input_tokens": 0}
    t.ingest({"type": "assistant", "message": {"usage": usage, "content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/a/x.py"}}]}})
    meta = t.snapshot(now=1.0).meta
    assert "🪙 ↑1k ↓500 ⚡99k" in meta       # in / out / cache tokens
    assert "ctx 100k/1M (10%)" in meta        # used/window AND percentage
    assert "1 turn" in meta
    assert "opus-4-8" in meta                 # model, claude- stripped


def test_tracker_ctx_uses_per_model_window():
    # Haiku 4.5 is 200K, not 1M — same prompt size, different window/%.
    t = _ProgressTracker(start=0.0)
    t.ingest({"type": "assistant", "message": {"model": "claude-haiku-4-5", "usage": {
        "input_tokens": 100_000, "output_tokens": 1}, "content": []}})
    assert "ctx 100k/200k (50%)" in t.snapshot(now=1.0).meta


def test_model_context_loaded_from_config_with_fallback():
    assert _model_context("claude-opus-4-8") == 1_000_000   # 1M families
    assert _model_context("claude-sonnet-4-6") == 1_000_000
    assert _model_context("claude-haiku-4-5") == 200_000    # 200K
    assert _model_context("some-unknown-model") == _DEFAULT_CONTEXT


def test_model_context_falls_back_when_config_missing(monkeypatch, tmp_path):
    # Point the loader at a non-existent file → built-in windows still work.
    monkeypatch.setattr(claude_handler, "_MODEL_CONTEXT_CONFIG", tmp_path / "nope.json")
    windows, default = claude_handler._load_model_context()
    assert windows["opus"] == 1_000_000 and default == 200_000


def test_tracker_model_from_assistant_skips_synthetic():
    t = _ProgressTracker(start=0.0)
    t.ingest({"type": "assistant", "message": {"model": "<synthetic>", "content": []}})
    t.ingest({"type": "assistant", "message": {"model": "claude-opus-4-8", "content": []}})
    assert "opus-4-8" in t.snapshot(now=1.0).meta   # real id wins; <synthetic> ignored


def test_tracker_tracks_skills_and_mcp_servers():
    t = _ProgressTracker(start=0.0)
    t.ingest(_assistant(_tool("Skill", skill="superpowers:brainstorming")))
    t.ingest(_assistant(_tool("mcp__plugin_playwright_playwright__browser_navigate", url="x")))
    t.ingest(_assistant(_tool("mcp__claude_ai_GoDaddy__domains_suggest", query="y")))
    meta = t.snapshot(now=1.0).meta
    assert "🧩 brainstorming" in meta                 # skill, plugin prefix stripped
    assert "🔌" in meta and "plugin_playwright_playwright" in meta and "claude_ai_GoDaddy" in meta
    assert "2 mcp" in meta                            # both mcp tools grouped in the breakdown


def test_actions_cap_and_count_the_rest():
    t = _ProgressTracker(start=0.0)
    for i in range(130):
        t.ingest(_assistant(_tool("Read", file_path=f"/a/f{i}.py")))
    lines = t.snapshot(now=1.0).live.splitlines()
    assert lines[-1].startswith("… and ")           # overflow line at the bottom
    assert "f129.py" in lines[0]                     # newest on top
    assert lines[0].startswith("`130.`")            # numbered: highest on top
    shown = len(lines) - 1                           # excl. overflow line
    assert shown <= 99                               # bounded by the count cap
    hidden = int(lines[-1].split("… and ")[1].split(" ")[0])
    assert shown + hidden == 130                     # nothing lost in the count


def test_tracker_counts_compactions():
    t = _ProgressTracker(start=0.0)
    t.ingest({"type": "system", "subtype": "compact_boundary"})
    t.ingest(_assistant({"type": "compaction"}))  # API compaction content block
    assert "🗜️ compacted 2×" in t.snapshot(now=1.0).meta


def test_tracker_cost_estimate_format():
    # ≥ $1 → one decimal; < $1 → two decimals.
    big = _ProgressTracker(start=0.0)
    big.ingest({"type": "assistant", "message": {"model": "claude-opus-4-8", "usage": {
        "input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 6_700_000}, "content": []}})
    assert "($3.4)" in big.snapshot(now=1.0).meta

    small = _ProgressTracker(start=0.0)
    small.ingest({"type": "assistant", "message": {"model": "claude-opus-4-8", "usage": {
        "input_tokens": 12000, "output_tokens": 4200, "cache_read_input_tokens": 368000}, "content": []}})
    assert "($0.35)" in small.snapshot(now=1.0).meta


def test_tracker_idle_hint_at_top_when_quiet():
    t = _ProgressTracker(start=0.0)
    t.ingest(_assistant(_tool("Bash", command="npm run build")))
    live = t.snapshot(now=60.0, idle=46.0).live
    assert live.splitlines()[0].startswith("⏳ idle ")
    # No hint when active (idle below threshold).
    t.ingest(_assistant(_tool("Read", file_path="/a/x.py")))
    assert "⏳ idle" not in t.snapshot(now=61.0, idle=2.0).live


def test_tracker_tool_breakdown_and_subagents():
    t = _ProgressTracker(start=0.0)
    t.ingest(_assistant(_tool("Read", file_path="/a/a.py")))
    t.ingest(_assistant(_tool("Read", file_path="/a/b.py")))
    t.ingest(_assistant(_tool("Task", description="sub")))
    meta = t.snapshot(now=1.0).meta
    assert "2 read" in meta            # per-tool breakdown, busiest first
    assert "🤖 1 subagent" in meta


def test_tracker_todos_render_as_checklist():
    t = _ProgressTracker(start=0.0)
    t.ingest(_assistant(_tool("TodoWrite", todos=[
        {"content": "First task", "status": "completed"},
        {"content": "Second task", "status": "in_progress"},
        {"content": "Third task", "status": "pending"},
    ])))
    todos = t.snapshot(now=1.0).todos
    assert "📋 *Todos 1/3*" in todos
    assert "✅ First task" in todos
    assert "🔄 Second task" in todos
    assert "⬜ Third task" in todos


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


def test_tracker_summary_is_one_line_headline_detail_kept_in_sections():
    t = _ProgressTracker(start=0.0)
    t.ingest(_assistant(_tool("Edit", file_path="/a/session.py")))
    t.ingest(_assistant(_tool("Read", file_path="/a/config.py")))
    t.ingest(_assistant(_tool("Bash", command="pytest -q\necho done")))
    snap = t.snapshot(now=134.0, done=True)
    assert snap.done is True
    assert snap.summary == "✅ Done · 1 file changed · 3 tools · 2m14s"  # one-line headline
    assert "```" not in snap.summary          # no collapsed detail block now
    # Detail lives in the kept sections instead:
    assert "session.py" in snap.files
    assert "1 read · 1 edit · 1 bash" in snap.meta or "1 bash" in snap.meta


def test_tracker_summary_no_detail_block_when_nothing_tracked():
    t = _ProgressTracker(start=0.0)
    t.ingest({"type": "system", "subtype": "init"})  # nothing trackable
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


def _has_actions(blocks):
    return any(b.get("type") == "actions" for b in blocks)


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


def test_reporter_live_message_has_no_button():
    # Stop is a 🛑 reaction the daemon adds, not an inline button.
    client = _FakeClient()
    reporter = _ProgressReporter(client, channel="C1", thread_ts="T1")
    asyncio.run(reporter(_Progress(live="🔄 working", summary="s", done=False)))
    assert not _has_actions(client.posts[0]["blocks"])


def test_reporter_calls_on_status_posted_with_ts_once():
    client = _FakeClient()
    hits = []

    async def hook(status_ts):
        hits.append(status_ts)

    reporter = _ProgressReporter(client, channel="C1", thread_ts="T1", on_status_posted=hook)
    asyncio.run(reporter(_Progress(live="x", summary="s", done=False)))
    asyncio.run(reporter(_Progress(live="y", summary="s", done=False)))
    assert hits == ["1700000000.0001"]          # fired once, with the message ts
    assert reporter.posted_ts == "1700000000.0001"


def test_reporter_renders_meta_as_context_block():
    client = _FakeClient()
    reporter = _ProgressReporter(client, channel="C1", thread_ts="T1")
    asyncio.run(reporter(_Progress(
        live="✏️ Editing `x.py`", summary="s", done=False, meta="🔄 1 file · 2 tools · 5s",
    )))
    blocks = client.posts[0]["blocks"]
    kinds = [b["type"] for b in blocks]
    assert kinds == ["section", "context"]   # action lines + muted tally, no button
    assert blocks[1]["elements"][0]["text"] == "🔄 1 file · 2 tools · 5s"


def test_reporter_renders_todos_as_its_own_section():
    client = _FakeClient()
    reporter = _ProgressReporter(client, channel="C1", thread_ts="T1")
    asyncio.run(reporter(_Progress(
        live="🔄 working", summary="s", done=False,
        meta="2 read · 5s", todos="📋 *Todos 1/2*\n✅ a\n⬜ b",
    )))
    blocks = client.posts[0]["blocks"]
    kinds = [b["type"] for b in blocks]
    assert kinds == ["section", "section", "context"]  # todos, actions, tally
    assert "Todos 1/2" in blocks[0]["text"]["text"]    # todos on top now


def test_reporter_renders_files_as_its_own_section():
    client = _FakeClient()
    reporter = _ProgressReporter(client, channel="C1", thread_ts="T1")
    asyncio.run(reporter(_Progress(
        live="🔄 working", summary="s", done=False,
        meta="1 edit · 5s", files="📝 *1 file changed*\n• `x.py`  +3/−1",
    )))
    blocks = client.posts[0]["blocks"]
    kinds = [b["type"] for b in blocks]
    assert kinds == ["section", "section", "context"]  # actions, files, tally
    assert "1 file changed" in blocks[1]["text"]["text"]


def test_reporter_done_snapshot_renders_summary():
    client = _FakeClient()
    reporter = _ProgressReporter(client, channel="C1", thread_ts="T1")
    asyncio.run(reporter(_Progress(live="🔄 working", summary="✅ done", done=False)))
    asyncio.run(reporter(_Progress(live="🔄 working", summary="✅ done", done=True)))
    assert client.updates[-1]["text"] == "✅ done"       # summary, not live
    assert not _has_actions(client.updates[-1]["blocks"])
    assert reporter.posted_ts == "1700000000.0001"  # kept for reaction cleanup


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
