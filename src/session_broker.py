"""
session_broker.py — Session-side IPC broker.

Posts a message to Slack via the HTTP API, then connects to the daemon's
Unix socket and blocks until the daemon forwards the Slack reply. Uses the
OS-level blocking I/O of asyncio Unix sockets — no polling.
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)

SOCKET_PATH = "/tmp/slack-bridge.sock"

PostMessageFn = Callable[[str, str | None, str | None], Coroutine[Any, Any, str]]


class SessionBroker:
    """
    Coordinates a single request/reply cycle over the daemon Unix socket.

    Args:
        post_message: Async callable that posts to Slack and returns thread_ts.
                      Signature: (text, thread_ts | None) -> thread_ts.
    """

    def __init__(self, post_message: PostMessageFn) -> None:
        self._post_message = post_message
        self._thread_ts: str | None = None

    async def send_and_wait(self, message: str, label: str | None = None) -> str:
        """
        Post *message* to Slack and wait for the daemon to deliver the reply.

        Args:
            message: The text to post to the Slack channel.
            label:   Optional worktree tag — prepended to the first post in the
                     thread so multiple worktree sessions sharing a channel
                     can be told apart visually.

        Returns:
            The text of the first human reply received.
        """
        thread_ts = await self._post_message(message, self._thread_ts, label)
        if self._thread_ts is None:
            self._thread_ts = thread_ts
        logger.info("Posted message, awaiting reply on thread %s.", thread_ts)

        reader, writer = await asyncio.open_unix_connection(SOCKET_PATH)
        try:
            writer.write(f"REGISTER {thread_ts}\n".encode())
            await writer.drain()

            reply_bytes = await reader.readline()
            reply = reply_bytes.decode().strip()
            logger.info("Received reply for thread %s.", thread_ts)
            return reply
        finally:
            writer.close()
