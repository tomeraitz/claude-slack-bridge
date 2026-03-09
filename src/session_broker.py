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

PostMessageFn = Callable[[str], Coroutine[Any, Any, str]]


class SessionBroker:
    """
    Coordinates a single request/reply cycle over the daemon Unix socket.

    Args:
        post_message:    Async callable that posts to Slack and returns thread_ts.
        timeout_minutes: Seconds to wait for a reply before raising RuntimeError.
    """

    def __init__(self, post_message: PostMessageFn, timeout_minutes: int = 5) -> None:
        self._post_message = post_message
        self._timeout = timeout_minutes * 60.0

    async def send_and_wait(self, message: str) -> str:
        """
        Post *message* to Slack and wait for the daemon to deliver the reply.

        Args:
            message: The text to post to the Slack channel.

        Returns:
            The text of the first human reply received.

        Raises:
            RuntimeError: If no reply arrives within the configured timeout.
        """
        thread_ts = await self._post_message(message)
        logger.info("Posted message, awaiting reply on thread %s.", thread_ts)

        reader, writer = await asyncio.open_unix_connection(SOCKET_PATH)
        try:
            writer.write(f"REGISTER {thread_ts}\n".encode())
            await writer.drain()

            reply_bytes = await asyncio.wait_for(
                reader.readline(), timeout=self._timeout
            )
            reply = reply_bytes.decode().strip()
            logger.info("Received reply for thread %s.", thread_ts)
            return reply

        except asyncio.TimeoutError:
            raise RuntimeError(
                f"No reply received within {int(self._timeout // 60)} minutes "
                f"for thread {thread_ts}."
            )
        finally:
            writer.close()
