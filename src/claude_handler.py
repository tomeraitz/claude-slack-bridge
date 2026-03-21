"""
claude_handler.py — Spawns Claude Code CLI subprocesses for Human→Claude tasks.

When a human posts a message in Slack, this handler runs ``claude -p`` to
generate a response.  Thread continuations use ``--resume`` so Claude retains
full context (tool use, reasoning) across messages in the same thread.

If the session ID is lost (e.g. container restart), falls back to a one-shot
``claude -p`` with the formatted thread history as the prompt.
"""

import asyncio
import json
import logging
import os
import uuid
from typing import Any

logger = logging.getLogger(__name__)

SUBPROCESS_TIMEOUT = 300  # 5 minutes


class ClaudeHandler:
    """
    Manages Claude Code CLI invocations for Slack messages.

    Args:
        slack_client: An async Slack WebClient (``self._app.client``).
    """

    def __init__(self, slack_client: Any) -> None:
        self._slack_client = slack_client
        self._bot_user_id: str = ""
        self._sessions: dict[str, str] = {}  # thread_ts → session UUID

    async def initialize(self) -> None:
        """Cache the bot's own user ID (needed to label messages in fallback mode)."""
        resp = await self._slack_client.auth_test()
        self._bot_user_id = resp["user_id"]
        logger.info("ClaudeHandler initialized, bot_user_id=%s", self._bot_user_id)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def handle_message(self, channel: str, message_ts: str, text: str) -> str:
        """Handle a new top-level Slack message (start a new Claude session)."""
        session_id = str(uuid.uuid4())
        self._sessions[message_ts] = session_id
        logger.info("New Claude session %s for thread %s", session_id, message_ts)

        cmd = self._build_cmd(session_id=session_id)
        return await self._run_claude(cmd, text)

    async def handle_thread_reply(self, channel: str, thread_ts: str, text: str) -> str:
        """Handle a threaded reply (resume existing session or fallback)."""
        session_id = self._sessions.get(thread_ts)

        if session_id:
            logger.info("Resuming session %s for thread %s", session_id, thread_ts)
            cmd = self._build_cmd(resume=session_id)
            return await self._run_claude(cmd, text)

        # Fallback: session lost (container restart) — use thread history as context.
        logger.info("No session for thread %s, falling back to thread history.", thread_ts)
        prompt = await self._build_thread_prompt(channel, thread_ts)
        cmd = self._build_cmd()
        return await self._run_claude(cmd, prompt)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _build_cmd(
        session_id: str | None = None,
        resume: str | None = None,
    ) -> list[str]:
        cmd = ["claude", "-p", "--dangerously-skip-permissions", "--output-format", "json"]
        if session_id:
            cmd.extend(["--session-id", session_id])
        if resume:
            cmd.extend(["--resume", resume])
        return cmd

    async def _run_claude(self, cmd: list[str], prompt: str) -> str:
        """Spawn a ``claude -p`` subprocess and return the response text."""
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError:
            logger.error("claude CLI not found — is it installed and in PATH?")
            return "Sorry, the Claude CLI is not available."

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(input=prompt.encode("utf-8")),
                timeout=SUBPROCESS_TIMEOUT,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            logger.error("Claude subprocess timed out after %ds", SUBPROCESS_TIMEOUT)
            return "Sorry, the request timed out. Please try again."

        if process.returncode != 0:
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
            logger.error(
                "Claude CLI failed (rc=%d) stderr: %s | stdout: %s | cmd: %s | prompt: %r",
                process.returncode, stderr_text, stdout_text, cmd, prompt[:200],
            )
            return "Sorry, I encountered an error processing your request."

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        return self._parse_response(stdout_text)

    @staticmethod
    def _parse_response(raw: str) -> str:
        """Extract the response text from JSON output, or return raw text."""
        try:
            data = json.loads(raw)
            # The JSON output has a "result" field with the response text.
            if isinstance(data, dict) and "result" in data:
                return data["result"]
            return raw
        except (json.JSONDecodeError, KeyError):
            return raw

    async def _build_thread_prompt(self, channel: str, thread_ts: str) -> str:
        """Fetch Slack thread history and format as a conversation prompt."""
        resp = await self._slack_client.conversations_replies(
            channel=channel, ts=thread_ts
        )
        messages = resp.get("messages", [])

        lines = ["The following is a Slack conversation. Continue assisting the user.\n"]
        for msg in messages:
            is_bot = (
                msg.get("user") == self._bot_user_id
                or msg.get("bot_id")
            )
            label = "[Assistant]" if is_bot else "[Human]"
            text = msg.get("text", "")
            lines.append(f"{label}: {text}")

        return "\n".join(lines)
