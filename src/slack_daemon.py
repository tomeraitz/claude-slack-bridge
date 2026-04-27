"""
slack_daemon.py — Slack Socket Mode listener + Unix domain socket server.

The daemon holds exactly one Socket Mode WebSocket connection to Slack and
accepts local connections from session processes (started via docker exec).

Each session connects, sends ``REGISTER {thread_ts}\n``, and blocks. When a
Slack reply arrives for that thread_ts the daemon forwards it over the socket,
unblocking the waiting session with zero polling.

Additionally, the daemon handles Human→Claude messages: top-level Slack
messages (and threaded replies with no pending MCP session) are forwarded to
the Claude Code CLI, and the response is posted back as a thread reply.
"""

import asyncio
import logging
import os
from typing import Any

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from claude_handler import ClaudeHandler
from projects import ProjectResolver
from security import AccessControl, SecurityConfig
from workflow import WorkflowEngine

logger = logging.getLogger(__name__)

SOCKET_PATH = "/tmp/slack-bridge.sock"
SLACK_MAX_MESSAGE_LENGTH = 40000


class SlackDaemon:
    """
    Bridges Slack Socket Mode events to waiting session processes via a
    Unix domain socket, and handles Human→Claude messages via the Claude
    Code CLI.

    Args:
        bot_token: Slack bot OAuth token (xoxb-...).
        app_token: Slack app-level token for Socket Mode (xapp-...).
    """

    def __init__(self, bot_token: str, app_token: str) -> None:
        self._app = AsyncApp(token=bot_token)
        self._handler = AsyncSocketModeHandler(self._app, app_token)
        self._pending: dict[str, asyncio.StreamWriter] = {}
        self._lock = asyncio.Lock()
        self._claude = ClaudeHandler(slack_client=self._app.client)
        self._resolver = ProjectResolver()
        self._workflow = WorkflowEngine(
            slack_client=self._app.client,
            post_response=self._post_response,
            resolver=self._resolver,
        )
        self._active_threads: set[str] = set()
        self._bot_user_id: str = ""

        self._access_control = AccessControl(SecurityConfig.from_env())
        self._app.event("message")(self._handle_slack_message)
        self._app.event("app_mention")(self._handle_app_mention)

    async def _handle_slack_message(self, event: dict[str, Any]) -> None:
        # Filter: Ignore bot messages (prevents self-echo loops).
        if event.get("bot_id"):
            return

        user_id: str = event.get("user", "")
        channel: str = event.get("channel", "")

        # Access control: reject unauthorized users/channels before any processing.
        if not self._access_control.is_allowed(user_id=user_id, channel_id=channel):
            thread_ts = event.get("thread_ts") or event.get("ts", "")
            try:
                await self._app.client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=self._access_control.rejection_message(),
                )
            except Exception as exc:
                logger.warning("Failed to send rejection message to %s: %s", channel, exc)
            return

        thread_ts: str | None = event.get("thread_ts")
        text: str = event.get("text", "")
        text_stripped = text.strip()
        process_thread = thread_ts is not None and self._workflow.is_active_thread(thread_ts)

        # Strip @bot mention from text for /process detection (top-level posts may be @-prefixed)
        mention_tag = f"<@{self._bot_user_id}>"
        text_no_mention = text.replace(mention_tag, "").strip()

        # Case 0: /clean-process is an emergency stop — must reach the workflow
        # engine even if a step sub-Claude is currently blocked on ask_on_slack.
        if process_thread and text_stripped == "/clean-process":
            asyncio.create_task(self._workflow.handle_clean_process(thread_ts))
            return

        # Case 1: Threaded reply WITH a pending MCP session — forward to session.
        if thread_ts:
            async with self._lock:
                writer = self._pending.pop(thread_ts, None)

            if writer is not None:
                logger.info("Slack reply in thread %s: %r", thread_ts, text)
                try:
                    writer.write(text.encode() + b"\n")
                    await writer.drain()
                    logger.info("Reply forwarded to session for thread %s.", thread_ts)
                except Exception as exc:
                    logger.warning("Failed to forward reply for %s: %s", thread_ts, exc)
                finally:
                    writer.close()
                return

        # Case 1.5: thread is on the active /process registry, no pending session.
        if process_thread:
            if text_stripped == "/next-task":
                asyncio.create_task(self._workflow.handle_next_task(thread_ts))
                return
            if text_stripped.startswith("/reject"):
                reason = text_stripped[len("/reject"):].strip()
                asyncio.create_task(self._workflow.handle_reject(thread_ts, reason))
                return
            # Free text in /process thread (waiting_approval echo, running_step queueing,
            # failed-state preserve). Engine decides per phase.
            asyncio.create_task(self._workflow.handle_thread_message(thread_ts, text))
            return

        # Case 2: Threaded reply with NO pending session — continue Claude conversation.
        if thread_ts:
            if thread_ts in self._active_threads:
                return
            asyncio.create_task(self._handle_claude_thread_reply(channel, thread_ts, text))
            return

        # Case 3 (TOP-LEVEL): /process detection BEFORE @-mention check.
        # `/process` may be invoked plain or as `@bot /process`. Accept both.
        if text_no_mention == "/process":
            asyncio.create_task(self._handle_process_start(channel, event.get("ts", ""), user_id))
            return

        # Case 3 fallback: only respond if @-mentioned.
        if mention_tag not in text:
            return

        # Strip the mention from the text so Claude sees clean input.
        text = text.replace(mention_tag, "").strip()

        message_ts: str = event.get("ts", "")
        if message_ts in self._active_threads:
            return
        asyncio.create_task(self._handle_claude_new_message(channel, message_ts, text))

    async def _handle_app_mention(self, event: dict[str, Any]) -> None:
        """Handle app_mention events (bot @mentioned in any channel)."""
        user_id: str = event.get("user", "")
        channel: str = event.get("channel", "")

        if not self._access_control.is_allowed(user_id=user_id, channel_id=channel):
            thread_ts = event.get("thread_ts") or event.get("ts", "")
            try:
                await self._app.client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=self._access_control.rejection_message(),
                )
            except Exception as exc:
                logger.warning("Failed to send rejection message to %s: %s", channel, exc)
            return

        # Delegate to the normal message handler for authorized mentions.
        await self._handle_slack_message(event)

    async def _handle_claude_new_message(self, channel: str, message_ts: str, text: str) -> None:
        """Spawn Claude for a new top-level message and post the response as a thread reply."""
        self._active_threads.add(message_ts)
        try:
            response = await self._claude.handle_message(channel, message_ts, text)
            await self._post_response(channel, message_ts, response)
        except Exception as exc:
            logger.error("Error handling top-level message %s: %s", message_ts, exc)
        finally:
            self._active_threads.discard(message_ts)

    async def _handle_claude_thread_reply(self, channel: str, thread_ts: str, text: str) -> None:
        """Spawn Claude for a thread reply and post the response."""
        self._active_threads.add(thread_ts)
        try:
            response = await self._claude.handle_thread_reply(channel, thread_ts, text)
            await self._post_response(channel, thread_ts, response)
        except Exception as exc:
            logger.error("Error in thread continuation %s: %s", thread_ts, exc)
        finally:
            self._active_threads.discard(thread_ts)

    async def _handle_process_start(self, channel: str, message_ts: str, user_id: str) -> None:
        """Handle a top-level /process post: admit + spawn clarification sub-Claude."""
        project_dir, plugin_dir = self._resolver.get_project_config(channel)
        if not project_dir:
            await self._post_response(
                channel, message_ts,
                "/process requires a project mapping for this channel. "
                "Add this channel to projects.json.",
            )
            return

        admitted = await self._workflow.admit_process_start(channel, project_dir, message_ts)
        if not admitted:
            await self._post_response(
                channel, message_ts,
                "A process is active — finish it or post `/clean-process`.",
            )
            return

        # Spawn the clarification sub-Claude. cwd = main repo, env carries thread+channel.
        asyncio.create_task(
            self._spawn_clarification(channel, message_ts, project_dir, plugin_dir)
        )

    async def _spawn_clarification(
        self,
        channel: str,
        message_ts: str,
        project_dir: str,
        plugin_dir: str | None,
    ) -> None:
        """Spawn the clarification sub-Claude that drives the /process skill.

        The subprocess is responsible for sending ``START <worktree>`` over the
        Unix socket itself once it has written ``process.json`` and the active
        marker — this method does not advance workflow state.
        """
        cmd = [
            "claude", "-p",
            "--mcp-config", "/app/mcp.in-container.json",
            "--strict-mcp-config",
            "--dangerously-skip-permissions",
            "--output-format", "json",
        ]
        if plugin_dir:
            cmd.extend(["--plugin-dir", plugin_dir])

        prompt = "Run the `/process` clarification skill for a new feature."

        env = os.environ.copy()
        for _key in ("CLAUDECODE", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "ANTHROPIC_API_KEY"):
            env.pop(_key, None)
        env["SLACK_THREAD_TS"] = message_ts
        env["SLACK_CHANNEL"] = channel
        # NOTE: STEP_NAME is NOT set for clarification — only step sub-Claudes get it.

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=project_dir,
            )
            stdout_bytes, stderr_bytes = await process.communicate(input=prompt.encode("utf-8"))
            if process.returncode != 0:
                stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
                logger.error(
                    "Clarification sub-Claude failed (rc=%d): %s",
                    process.returncode, stderr_text[:1000],
                )
                await self._post_response(
                    channel, message_ts,
                    "Clarification step failed — see daemon logs. /clean-process to reset.",
                )
        except FileNotFoundError:
            logger.error("claude CLI not found")
            await self._post_response(channel, message_ts, "Claude CLI not available.")
        except Exception as exc:
            logger.error("Clarification spawn error: %s", exc)

    async def _post_response(self, channel: str, thread_ts: str, text: str) -> None:
        """Post a response to Slack, splitting if it exceeds the message length limit."""
        if len(text) <= SLACK_MAX_MESSAGE_LENGTH:
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=text, mrkdwn=True,
            )
            return

        for i in range(0, len(text), SLACK_MAX_MESSAGE_LENGTH):
            chunk = text[i : i + SLACK_MAX_MESSAGE_LENGTH]
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=chunk, mrkdwn=True,
            )

    async def _handle_session_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        thread_ts: str | None = None
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            parts = line.decode().strip().split(" ", 1)
            verb = parts[0] if parts else ""

            if verb == "REGISTER" and len(parts) == 2:
                thread_ts = parts[1]
                async with self._lock:
                    self._pending[thread_ts] = writer

                logger.info("Session registered for thread %s.", thread_ts)

                # Block until the session disconnects (reader.read returns b"" on close).
                # This ensures _pending is cleaned up if the session exits before a reply arrives.
                await reader.read(1)
            elif verb == "START" and len(parts) == 2:
                worktree_path = parts[1]
                logger.info("Workflow START for worktree %s", worktree_path)
                # The clarification sub-Claude has already written process.json and the
                # active marker; we just hand off to the engine.
                asyncio.create_task(self._workflow.handle_start_verb(worktree_path))
                # Close the socket immediately — the clarification skill doesn't wait for a reply.
                return
            else:
                logger.warning("Bad session registration: %r", line)
                return

        except Exception as exc:
            logger.error("Session connection error: %s", exc)
        finally:
            if thread_ts:
                async with self._lock:
                    self._pending.pop(thread_ts, None)
            if not writer.is_closing():
                writer.close()

    async def start(self) -> None:
        """Start the Unix socket server and Slack Socket Mode handler concurrently."""
        await self._claude.initialize()
        self._bot_user_id = self._claude._bot_user_id

        await self._resolver.resolve(self._app.client)
        await self._workflow.recover_on_startup()

        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)

        server = await asyncio.start_unix_server(
            self._handle_session_connection, path=SOCKET_PATH
        )
        logger.info("Unix socket server listening at %s.", SOCKET_PATH)

        async with server:
            await asyncio.gather(
                server.serve_forever(),
                self._handler.start_async(),
            )
