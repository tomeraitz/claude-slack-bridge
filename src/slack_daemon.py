"""
slack_daemon.py — Slack Socket Mode listener + Unix domain socket server.

The daemon holds exactly one Socket Mode WebSocket connection to Slack and
accepts local connections from session processes (started via docker exec).

Each session connects, sends ``REGISTER {thread_ts}\n``, and blocks. When a
Slack reply arrives for that thread_ts the daemon forwards it over the socket,
unblocking the waiting session with zero polling.
"""

import asyncio
import logging
import os
from typing import Any

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

logger = logging.getLogger(__name__)

SOCKET_PATH = "/tmp/slack-bridge.sock"


class SlackDaemon:
    """
    Bridges Slack Socket Mode events to waiting session processes via a
    Unix domain socket.

    Sessions connect to ``SOCKET_PATH``, send ``REGISTER {thread_ts}\\n``,
    and block. When a matching Slack reply arrives the daemon writes the reply
    text followed by a newline, waking the session immediately.

    Args:
        bot_token: Slack bot OAuth token (xoxb-...).
        app_token: Slack app-level token for Socket Mode (xapp-...).
    """

    def __init__(self, bot_token: str, app_token: str) -> None:
        self._app = AsyncApp(token=bot_token)
        self._handler = AsyncSocketModeHandler(self._app, app_token)
        self._pending: dict[str, asyncio.StreamWriter] = {}
        self._lock = asyncio.Lock()

        self._app.event("message")(self._handle_slack_message)

    async def _handle_slack_message(self, event: dict[str, Any]) -> None:
        # Filter 1: Ignore bot messages (prevents self-echo loops).
        if event.get("bot_id"):
            return

        # Filter 2: Only care about threaded replies.
        thread_ts: str | None = event.get("thread_ts")
        if not thread_ts:
            return

        reply_text: str = event.get("text", "")
        logger.info("Slack reply in thread %s: %r", thread_ts, reply_text)

        async with self._lock:
            writer = self._pending.pop(thread_ts, None)

        if writer is None:
            logger.debug("No session waiting for thread %s — ignoring.", thread_ts)
            return

        try:
            writer.write(reply_text.encode() + b"\n")
            await writer.drain()
            logger.info("Reply forwarded to session for thread %s.", thread_ts)
        except Exception as exc:
            logger.warning("Failed to forward reply for %s: %s", thread_ts, exc)
        finally:
            writer.close()

    async def _handle_session_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            parts = line.decode().strip().split(" ", 1)

            if len(parts) != 2 or parts[0] != "REGISTER":
                logger.warning("Bad session registration: %r", line)
                writer.close()
                return

            thread_ts = parts[1]
            async with self._lock:
                self._pending[thread_ts] = writer

            logger.info("Session registered for thread %s.", thread_ts)
            # Connection stays open — reply is sent by _handle_slack_message.

        except Exception as exc:
            logger.error("Session connection error: %s", exc)
            writer.close()

    async def start(self) -> None:
        """Start the Unix socket server and Slack Socket Mode handler concurrently."""
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
