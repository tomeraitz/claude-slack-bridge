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
from slack_sdk.errors import SlackApiError

import attachments
from claude_handler import (
    ClaudeHandler,
    _INTERRUPTED_REPLY,
    _RUN_FAILURE_SENTINELS,
    _TIMEOUT_REPLY,
)
from crash_recovery import recover_interrupted_runs
from security import AccessControl, SecurityConfig
from slack_markdown import build_markdown_payloads

logger = logging.getLogger(__name__)

SOCKET_PATH = "/tmp/slack-bridge.sock"

# action_id of the 🛑 Stop button on the live status message. Clicking it routes
# to _handle_stop_button (delivered over Socket Mode — requires Interactivity to
# be enabled in the Slack app; no request URL is needed). The 🛑 *reaction* on
# the trigger message remains as a no-config fallback.
_STOP_ACTION_ID = "stop_run"

# Replies that mean the run failed/stalled rather than producing an answer.
# When one is returned, the live status message is removed so only the error
# reply remains in the thread.
_TERMINAL_ERRORS: frozenset[str] = _RUN_FAILURE_SENTINELS | {
    _TIMEOUT_REPLY, _INTERRUPTED_REPLY,
}


class _ProgressReporter:
    """Posts and live-edits ONE Slack status message for a running Claude job.

    Handed to :class:`ClaudeHandler` as its ``progress_cb``. The first snapshot
    creates the message; later snapshots edit it in place — no new posts, so no
    thread spam. A ``done`` snapshot collapses it into the summary. On stop or
    error the daemon calls :meth:`delete` so only the final reply remains.
    """

    def __init__(self, client: Any, channel: str, thread_ts: str) -> None:
        self._client = client
        self._channel = channel
        self._thread_ts = thread_ts
        self._status_ts: str | None = None

    def _blocks(self, text: str, *, with_stop: bool) -> list[dict]:
        """Status text as a section, plus a 🛑 Stop button while still running."""
        blocks: list[dict] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}}
        ]
        if with_stop:
            blocks.append({
                "type": "actions",
                "elements": [{
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🛑 Stop", "emoji": True},
                    "style": "danger",
                    "action_id": _STOP_ACTION_ID,
                    "value": self._thread_ts,  # what _handle_stop_button stops
                }],
            })
        return blocks

    async def __call__(self, progress: Any) -> None:
        text = progress.summary if progress.done else progress.live
        # Drop the button once the run is done (summary snapshot).
        blocks = self._blocks(text, with_stop=not progress.done)
        if self._status_ts is None:
            resp = await self._client.chat_postMessage(
                channel=self._channel, thread_ts=self._thread_ts, text=text, blocks=blocks,
            )
            self._status_ts = resp["ts"]
        else:
            await self._client.chat_update(
                channel=self._channel, ts=self._status_ts, text=text, blocks=blocks,
            )

    async def delete(self) -> None:
        """Remove the status message if one was posted. Best-effort."""
        if self._status_ts is None:
            return
        try:
            await self._client.chat_delete(channel=self._channel, ts=self._status_ts)
        except Exception as exc:  # noqa: BLE001 — cleanup must not raise
            logger.warning("Failed to delete status message %s: %s", self._status_ts, exc)
        finally:
            self._status_ts = None


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
        self._bot_token = bot_token
        self._handler = AsyncSocketModeHandler(self._app, app_token)
        self._pending: dict[str, asyncio.StreamWriter] = {}
        self._lock = asyncio.Lock()
        self._claude = ClaudeHandler(slack_client=self._app.client)
        self._active_threads: set[str] = set()
        # Feature C: trigger_ts (the reacted-to message ts) → thread_ts, so a
        # reaction_added event can find the run to stop.
        self._trigger_to_thread: dict[str, str] = {}
        self._bot_user_id: str = ""

        self._access_control = AccessControl(SecurityConfig.from_env())
        self._app.event("message")(self._handle_slack_message)
        self._app.event("app_mention")(self._handle_app_mention)
        self._app.event("reaction_added")(self._handle_reaction_added)
        self._app.action(_STOP_ACTION_ID)(self._handle_stop_button)

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
        raw_files: list[dict] = event.get("files") or []
        event_ts: str = event.get("ts", "")

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

        # Case 2: Threaded reply with NO pending session — continue Claude conversation.
        if thread_ts:
            if thread_ts in self._active_threads:
                return
            asyncio.create_task(
                self._handle_claude_thread_reply(channel, thread_ts, text, event_ts, raw_files)
            )
            return

        # Case 3: Top-level message — only respond if the bot is mentioned.
        mention_tag = f"<@{self._bot_user_id}>"
        if mention_tag not in text:
            return

        # Strip the mention from the text so Claude sees clean input.
        text = text.replace(mention_tag, "").strip()

        message_ts: str = event.get("ts", "")
        if message_ts in self._active_threads:
            return
        asyncio.create_task(
            self._handle_claude_new_message(channel, message_ts, text, message_ts, raw_files)
        )

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

    async def _handle_reaction_added(self, event: dict[str, Any]) -> None:
        """Stop a running Flow-B agent when a non-bot user reacts 🛑 to its trigger."""
        if event.get("reaction") != "octagonal_sign":
            return
        if event.get("user") == self._bot_user_id:
            return  # ignore the bot's own 🛑 (it adds the reaction itself)

        item = event.get("item") or {}
        trigger_ts = item.get("ts", "")
        thread_ts = self._trigger_to_thread.get(trigger_ts)
        if thread_ts is None:
            return  # unknown or already-finished run

        channel = item.get("channel", "")
        killed = await self._claude.stop(thread_ts)
        if not killed:
            return  # nothing in flight to stop

        try:
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text="⏹️ Stopped."
            )
        except Exception as exc:
            logger.warning("Failed to post stop notice for %s: %s", thread_ts, exc)

    async def _handle_stop_button(self, ack, body: dict) -> None:
        """Stop a run when the 🛑 Stop button on its status message is clicked.

        Mirrors the 🛑-reaction path: the button's ``value`` carries the run's
        thread_ts, so we kill that run and post the stop notice. The run task
        then deletes its (button-bearing) status message via the reporter.
        """
        await ack()  # acknowledge within Slack's 3s window before doing work
        actions = body.get("actions") or []
        thread_ts = actions[0].get("value") if actions else None
        if not thread_ts:
            return
        channel = (body.get("channel") or {}).get("id", "")
        killed = await self._claude.stop(thread_ts)
        if not killed:
            return  # nothing in flight (already finished / stopped)

        try:
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text="⏹️ Stopped."
            )
        except Exception as exc:
            logger.warning("Failed to post stop notice for %s: %s", thread_ts, exc)

    async def _handle_claude_new_message(
        self, channel: str, message_ts: str, text: str, trigger_ts: str,
        raw_files: list[dict] | None = None,
    ) -> None:
        """Spawn Claude for a new top-level message and post the response as a thread reply."""
        self._active_threads.add(message_ts)
        self._trigger_to_thread[trigger_ts] = message_ts
        await self._add_stop_reaction(channel, trigger_ts)
        reporter = _ProgressReporter(self._app.client, channel, message_ts)
        try:
            files = await attachments.download_files(
                raw_files or [], message_ts, self._bot_token
            )
            response = await self._claude.handle_message(
                channel, message_ts, text, files, progress_cb=reporter
            )
            if message_ts in self._claude._stopped:
                logger.info("Run for %s was stopped; suppressing reply.", message_ts)
                await reporter.delete()
            else:
                if response in _TERMINAL_ERRORS:
                    await reporter.delete()
                await self._deliver_response(channel, message_ts, response)
        except Exception as exc:
            logger.error("Error handling top-level message %s: %s", message_ts, exc)
            await reporter.delete()
        finally:
            # Clear the stopped flag on EVERY exit path (incl. exceptions), so a
            # later run on this thread_ts is not silently suppressed.
            self._claude._stopped.discard(message_ts)
            await self._remove_stop_reaction(channel, trigger_ts)
            self._trigger_to_thread.pop(trigger_ts, None)
            self._active_threads.discard(message_ts)

    async def _handle_claude_thread_reply(
        self, channel: str, thread_ts: str, text: str, trigger_ts: str,
        raw_files: list[dict] | None = None,
    ) -> None:
        """Spawn Claude for a thread reply and post the response."""
        self._active_threads.add(thread_ts)
        self._trigger_to_thread[trigger_ts] = thread_ts
        await self._add_stop_reaction(channel, trigger_ts)
        reporter = _ProgressReporter(self._app.client, channel, thread_ts)
        try:
            files = await attachments.download_files(
                raw_files or [], thread_ts, self._bot_token
            )
            response = await self._claude.handle_thread_reply(
                channel, thread_ts, text, files, progress_cb=reporter
            )
            if thread_ts in self._claude._stopped:
                logger.info("Run for %s was stopped; suppressing reply.", thread_ts)
                await reporter.delete()
            else:
                if response in _TERMINAL_ERRORS:
                    await reporter.delete()
                await self._deliver_response(channel, thread_ts, response)
        except Exception as exc:
            logger.error("Error in thread continuation %s: %s", thread_ts, exc)
            await reporter.delete()
        finally:
            # Clear the stopped flag on EVERY exit path (incl. exceptions), so a
            # later run on this thread_ts is not silently suppressed.
            self._claude._stopped.discard(thread_ts)
            await self._remove_stop_reaction(channel, trigger_ts)
            self._trigger_to_thread.pop(trigger_ts, None)
            self._active_threads.discard(thread_ts)

    async def _deliver_response(self, channel: str, thread_ts: str, response: str) -> None:
        """Upload any ``@@attach`` files from *response*, then post the rest.

        Marker lines are parsed out, existing paths are ``files_upload_v2``'d into
        the thread, and the cleaned text is posted via ``_post_response``. When no
        text remains after stripping, no text message is posted.
        """
        text, paths = attachments.parse_attach_markers(response)
        if paths:
            await attachments.upload_files(
                self._app.client, channel=channel, thread_ts=thread_ts, paths=paths
            )
        if text:
            await self._post_response(channel, thread_ts, text)

    async def _add_stop_reaction(self, channel: str, trigger_ts: str) -> None:
        """Add the 🛑 reaction marking a run as stoppable. Best-effort."""
        try:
            await self._app.client.reactions_add(
                channel=channel, name="octagonal_sign", timestamp=trigger_ts
            )
        except Exception as exc:
            logger.warning("Failed to add stop reaction on %s: %s", trigger_ts, exc)

    async def _remove_stop_reaction(self, channel: str, trigger_ts: str) -> None:
        """Remove the 🛑 reaction when the run ends by any path. Best-effort."""
        try:
            await self._app.client.reactions_remove(
                channel=channel, name="octagonal_sign", timestamp=trigger_ts
            )
        except Exception as exc:
            logger.warning("Failed to remove stop reaction on %s: %s", trigger_ts, exc)

    async def _post_response(self, channel: str, thread_ts: str, text: str) -> None:
        """Post a response to Slack, rendering Markdown via a markdown block.

        Long replies are split under Slack's per-message markdown limit; each
        piece is posted in the same thread.
        """
        for payload in build_markdown_payloads(text):
            try:
                await self._app.client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts, **payload
                )
            except SlackApiError as exc:
                # Slack rejects markdown blocks whose tables exceed 10k chars
                # (and a few other block limits). Fall back to plain text so the
                # content is still delivered instead of silently dropped.
                if exc.response.get("error") != "invalid_blocks":
                    raise
                logger.warning(
                    "markdown block rejected (%s); posting as text",
                    exc.response.get("error"),
                )
                await self._app.client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text=payload["blocks"][0]["text"], mrkdwn=True,
                )

    async def _handle_session_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        thread_ts: str | None = None
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            parts = line.decode().strip().split(" ", 1)

            if len(parts) != 2 or parts[0] != "REGISTER":
                logger.warning("Bad session registration: %r", line)
                return

            thread_ts = parts[1]
            async with self._lock:
                self._pending[thread_ts] = writer

            logger.info("Session registered for thread %s.", thread_ts)

            # Block until the session disconnects (reader.read returns b"" on close).
            # This ensures _pending is cleaned up if the session exits before a reply arrives.
            await reader.read(1)

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

        try:
            recovered = await recover_interrupted_runs(self._app.client)
            if recovered:
                logger.warning("Recovered %d interrupted thread(s): %s",
                               len(recovered), recovered)
        except Exception as exc:  # noqa: BLE001 — recovery must not block startup
            logger.error("Crash recovery failed: %s", exc)

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
