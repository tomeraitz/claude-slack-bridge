"""Policy tests for claude_handler timeout handling (Finding 6).

A subprocess timeout must be surfaced to the user *immediately* and must NOT be
retried/scraped: the run was making progress but didn't fit the window, so
retrying (then scraping) just serially re-runs the same expensive work for up to
3× the timeout while the user sees nothing. Genuine transient failures must
still retry.
"""

import asyncio

import claude_handler
from claude_handler import ClaudeHandler, _RUN_FAILURE_SENTINELS, _TIMEOUT_REPLY

_THREAD = "1782206141.594959"


def _make_handler(tmp_path):
    return ClaudeHandler(slack_client=object(), store_path=tmp_path / "sessions.json")


def _arm_resume_path(handler, tmp_path, monkeypatch):
    """Wire a thread so handle_thread_reply takes the hot --resume branch."""
    handler._sessions[_THREAD] = "sess-abc"
    handler._thread_config[_THREAD] = ("/home/node/proj", None)
    # Pretend the session jsonl exists (tmp_path is a real dir → .exists()).
    monkeypatch.setattr(claude_handler, "_jsonl_path", lambda cwd, sid: tmp_path)


def test_timeout_reply_not_a_failure_sentinel():
    # If the timeout reply were a failure sentinel, handle_thread_reply would
    # retry it (then scrape) — exactly the silent-stall bug this fixes.
    assert _TIMEOUT_REPLY not in _RUN_FAILURE_SENTINELS


def test_thread_reply_delivers_timeout_without_retry(tmp_path, monkeypatch):
    handler = _make_handler(tmp_path)
    _arm_resume_path(handler, tmp_path, monkeypatch)

    calls = []

    async def fake_run(cmd, prompt, cwd=None, thread_ts=None, channel=None, progress_cb=None):
        calls.append(prompt)
        return _TIMEOUT_REPLY

    monkeypatch.setattr(handler, "_run_claude", fake_run)

    reply = asyncio.run(handler.handle_thread_reply("C123", _THREAD, "do the thing"))

    assert reply == _TIMEOUT_REPLY
    assert len(calls) == 1  # delivered immediately — NOT retried, NOT scraped


def test_thread_reply_still_retries_genuine_failure(tmp_path, monkeypatch):
    # The fix must not disable retry+scrape for genuine transient failures.
    handler = _make_handler(tmp_path)
    _arm_resume_path(handler, tmp_path, monkeypatch)

    sentinel = "Sorry, I encountered an error processing your request."
    calls = []

    async def fake_run(cmd, prompt, cwd=None, thread_ts=None, channel=None, progress_cb=None):
        calls.append(prompt)
        return sentinel

    async def fake_scrape(*a, **k):
        return "scraped-ok"

    monkeypatch.setattr(handler, "_run_claude", fake_run)
    monkeypatch.setattr(handler, "_scrape_and_run", fake_scrape)

    reply = asyncio.run(handler.handle_thread_reply("C123", _THREAD, "x"))

    assert reply == "scraped-ok"
    assert len(calls) == 2  # initial + one retry, then scrape fallback
