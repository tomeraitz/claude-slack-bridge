"""Tests that inbound attachment paths are injected into the Claude prompt."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import claude_handler
from claude_handler import ClaudeHandler


def _handler() -> ClaudeHandler:
    h = ClaudeHandler(slack_client=AsyncMock())
    h._bot_user_id = "UBOT"
    return h


class TestHandleMessageInjectsAttachments:
    def test_new_message_appends_attachment_note_to_prompt(self):
        h = _handler()
        captured = {}

        async def fake_run(cmd, prompt, cwd=None, thread_ts=None, channel=None, progress_cb=None):
            captured["prompt"] = prompt
            return "done"

        h._run_claude = fake_run  # type: ignore[assignment]

        result = asyncio.run(
            h.handle_message(
                "C1", "T-1", "look at this",
                files=[("/tmp/slack-bridge/in/T-1/diagram.png", "image/png")],
            )
        )

        assert result == "done"
        assert captured["prompt"] == (
            "look at this\n\n[User attached: "
            "/tmp/slack-bridge/in/T-1/diagram.png (image/png)]"
        )

    def test_new_message_without_files_prompt_unchanged(self):
        h = _handler()
        captured = {}

        async def fake_run(cmd, prompt, cwd=None, thread_ts=None, channel=None, progress_cb=None):
            captured["prompt"] = prompt
            return "ok"

        h._run_claude = fake_run  # type: ignore[assignment]
        asyncio.run(h.handle_message("C1", "T-2", "plain text"))
        assert captured["prompt"] == "plain text"


class TestHandleThreadReplyInjectsAttachments:
    def test_resume_path_appends_note(self, monkeypatch):
        h = _handler()
        h._sessions["T-9"] = "sess-uuid"
        # Resume policy requires the session's jsonl to exist; point it at a
        # real file so the --resume path (not the scrape fallback) is taken.
        monkeypatch.setattr(claude_handler, "_jsonl_path", lambda cwd, sid: Path(__file__))
        captured = {}

        async def fake_run(cmd, prompt, cwd=None, thread_ts=None, channel=None, progress_cb=None):
            captured["prompt"] = prompt
            return "ok"

        h._run_claude = fake_run  # type: ignore[assignment]
        asyncio.run(
            h.handle_thread_reply(
                "C1", "T-9", "and this one",
                files=[("/tmp/slack-bridge/in/T-9/spec.pdf", "application/pdf")],
            )
        )
        assert captured["prompt"] == (
            "and this one\n\n[User attached: "
            "/tmp/slack-bridge/in/T-9/spec.pdf (application/pdf)]"
        )

    def test_scrape_fallback_path_appends_note(self):
        # When the session is lost, the note must still be appended onto the
        # scraped thread-history prompt.
        h = _handler()  # no _sessions entry for T-10 -> fallback path
        captured = {}

        async def fake_build_prompt(channel, thread_ts):
            return "scraped history prompt"

        async def fake_run(cmd, prompt, cwd=None, thread_ts=None, channel=None, progress_cb=None):
            captured["prompt"] = prompt
            return "ok"

        h._build_thread_prompt = fake_build_prompt  # type: ignore[assignment]
        h._run_claude = fake_run  # type: ignore[assignment]
        asyncio.run(
            h.handle_thread_reply(
                "C1", "T-10", "more",
                files=[("/tmp/slack-bridge/in/T-10/img.png", "image/png")],
            )
        )
        assert captured["prompt"] == (
            "scraped history prompt\n\n[User attached: "
            "/tmp/slack-bridge/in/T-10/img.png (image/png)]"
        )


class TestSystemPromptDocumentsMarker:
    def test_flow_b_prompt_explains_attach_marker(self):
        prompt = ClaudeHandler._FLOW_B_SYSTEM_PROMPT
        assert "@@attach" in prompt
        # Marker must be described as one absolute path per line.
        assert "absolute path" in prompt.lower()
