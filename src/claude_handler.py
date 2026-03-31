"""
claude_handler.py — Spawns Claude Code CLI subprocesses for Human→Claude tasks.

When a human posts a message in Slack, this handler runs ``claude -p`` to
generate a response.  Thread continuations use ``--resume`` so Claude retains
full context (tool use, reasoning) across messages in the same thread.

If the session ID is lost (e.g. container restart), falls back to a one-shot
``claude -p`` with the formatted thread history as the prompt.

Project detection: reads ``/app/projects.json`` to map Slack channels to
project directories mounted at ``/projects/`` inside the container.  When a
message arrives, the handler resolves the channel to a project path and runs
``claude -p`` from that directory so Claude sees the project's CLAUDE.md and
codebase.
"""

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SUBPROCESS_TIMEOUT = 300  # 5 minutes
PROJECTS_CONFIG = Path("/app/projects.json")


def _load_project_map() -> dict[str, str]:
    """Load channel-name → container project-path mapping from projects.json."""
    if not PROJECTS_CONFIG.exists():
        logger.warning("No projects.json at %s — project detection disabled.", PROJECTS_CONFIG)
        return {}
    with open(PROJECTS_CONFIG) as f:
        mapping = json.load(f)
    logger.info("Loaded project map with %d entries.", len(mapping))
    return mapping


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
        self._project_map: dict[str, str] = _load_project_map()
        # Resolved at startup: channel ID → container project path.
        self._channel_id_to_project: dict[str, str] = {}

    async def initialize(self) -> None:
        """Cache the bot's own user ID and resolve channel names to IDs."""
        resp = await self._slack_client.auth_test()
        self._bot_user_id = resp["user_id"]
        logger.info("ClaudeHandler initialized, bot_user_id=%s", self._bot_user_id)

        if self._project_map:
            await self._resolve_channel_ids()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def handle_message(self, channel: str, message_ts: str, text: str) -> str:
        """Handle a new top-level Slack message (start a new Claude session)."""
        session_id = str(uuid.uuid4())
        self._sessions[message_ts] = session_id
        logger.info("New Claude session %s for thread %s", session_id, message_ts)

        project_dir = self._get_project_dir(channel)
        cmd = self._build_cmd(session_id=session_id)
        return await self._run_claude(cmd, text, cwd=project_dir)

    async def handle_thread_reply(self, channel: str, thread_ts: str, text: str) -> str:
        """Handle a threaded reply (resume existing session or fallback)."""
        session_id = self._sessions.get(thread_ts)
        project_dir = self._get_project_dir(channel)

        if session_id:
            logger.info("Resuming session %s for thread %s", session_id, thread_ts)
            cmd = self._build_cmd(resume=session_id)
            return await self._run_claude(cmd, text, cwd=project_dir)

        # Fallback: session lost (container restart) — use thread history as context.
        logger.info("No session for thread %s, falling back to thread history.", thread_ts)
        prompt = await self._build_thread_prompt(channel, thread_ts)
        cmd = self._build_cmd()
        return await self._run_claude(cmd, prompt, cwd=project_dir)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_project_dir(self, channel_id: str) -> str | None:
        """Look up the mounted project directory for a Slack channel ID."""
        project_dir = self._channel_id_to_project.get(channel_id)
        if project_dir:
            logger.info("Channel %s → project %s", channel_id, project_dir)
        else:
            logger.info("No project mapping for channel %s — using default cwd.", channel_id)
        return project_dir

    async def _resolve_channel_ids(self) -> None:
        """Resolve channel names from project_map to Slack channel IDs."""
        try:
            result = await self._slack_client.conversations_list(
                types="public_channel,private_channel", limit=1000,
            )
            channels = result.get("channels", [])

            name_to_id: dict[str, str] = {}
            for ch in channels:
                name_to_id[f"#{ch['name']}"] = ch["id"]
                name_to_id[ch["name"]] = ch["id"]
                name_to_id[ch["id"]] = ch["id"]  # allow raw IDs in config

            for channel_key, project_path in self._project_map.items():
                channel_id = name_to_id.get(channel_key)
                if channel_id:
                    self._channel_id_to_project[channel_id] = project_path
                    logger.info("Mapped %s (ID: %s) → %s", channel_key, channel_id, project_path)
                else:
                    logger.warning("Channel %s not found in workspace — skipping.", channel_key)

        except Exception as exc:
            logger.error("Failed to resolve channel IDs: %s", exc)

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

    async def _run_claude(self, cmd: list[str], prompt: str, cwd: str | None = None) -> str:
        """Spawn a ``claude -p`` subprocess and return the response text."""
        env = os.environ.copy()
        # Strip tokens that must never be reachable by the Claude subprocess.
        # A prompt-injection attack could otherwise instruct Claude to exfiltrate them.
        for _key in ("CLAUDECODE", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "ANTHROPIC_API_KEY"):
            env.pop(_key, None)

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
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
