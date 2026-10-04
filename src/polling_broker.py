"""
polling_broker.py — Daemon-free session broker.

Same interface as ``SessionBroker`` (``send_and_wait`` / ``send_only``), but
instead of waiting on the daemon's Unix socket it posts with the bot token and
reads the reply back with ``conversations.replies``. No daemon, no Socket Mode,
no ``xapp-`` token — one process that only needs ``SLACK_BOT_TOKEN``.

The trade-off is latency: replies are picked up on the next poll (every
``interval`` seconds) rather than pushed. Used by the native CLI (``cli.py``).
"""

import asyncio
import logging
from typing import Any

from slack_sdk.errors import SlackApiError

from slack_markdown import build_markdown_payloads

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 3.0

# Message subtypes that still carry a human's answer. Everything else
# (channel_join, bot_message, message_changed, ...) is ignored.
_HUMAN_SUBTYPES = {None, "thread_broadcast", "file_share"}


def _reply_text(message: dict[str, Any]) -> str:
    """Return a message's text, describing attachments when there is no text."""
    text = (message.get("text") or "").strip()
    files = message.get("files") or []
    if files:
        names = ", ".join(f.get("name") or f.get("title") or "file" for f in files)
        note = f"[attached: {names}]"
        text = f"{text}\n{note}" if text else note
    return text


class PollingBroker:
    """
    Posts to one Slack channel and polls the thread for the human reply.

    Args:
        client:    A ``slack_sdk`` ``AsyncWebClient`` authenticated with the bot token.
        channel:   Channel name or ID to post into.
        thread_ts: Optional existing thread to continue instead of starting one.
        interval:  Seconds between polls while waiting for a reply.
    """

    def __init__(
        self,
        client: Any,
        channel: str,
        thread_ts: str | None = None,
        interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self._client = client
        self._channel = channel
        self._channel_id: str | None = None
        self._thread_ts = thread_ts
        self._interval = interval

    @property
    def channel(self) -> str:
        """The channel this broker posts into, as configured."""
        return self._channel

    @property
    def thread_ts(self) -> str | None:
        """The thread this broker posts into (set after the first post)."""
        return self._thread_ts

    async def _post(self, text: str, label: str | None) -> str:
        """Post *text* (split into Markdown chunks) and return the LAST post's ts."""
        payloads = build_markdown_payloads(
            text,
            broadcast=True,
            label=label if self._thread_ts is None else None,
        )
        last_ts = ""
        for payload in payloads:
            kwargs: dict = dict(channel=self._channel, **payload)
            if self._thread_ts is not None:
                kwargs["thread_ts"] = self._thread_ts
            response = await self._client.chat_postMessage(**kwargs)
            if not response.get("ok"):
                raise RuntimeError(f"Slack API error: {response.get('error')}")
            last_ts = response["ts"]
            # conversations.replies needs the channel ID, not "#name".
            self._channel_id = response.get("channel") or self._channel
            if self._thread_ts is None:
                self._thread_ts = last_ts
        logger.info("Posted to %s, thread_ts=%s", self._channel, self._thread_ts)
        return last_ts

    async def _fetch_after(self, after_ts: str) -> list[dict[str, Any]]:
        """Return thread messages newer than *after_ts* (oldest first)."""
        while True:
            try:
                response = await self._client.conversations_replies(
                    channel=self._channel_id,
                    ts=self._thread_ts,
                    oldest=after_ts,
                    inclusive=False,
                    limit=100,
                )
            except SlackApiError as exc:
                if exc.response.status_code != 429:
                    raise
                retry_after = float(exc.response.headers.get("Retry-After", self._interval))
                logger.warning("Rate limited by Slack; retrying in %.0fs.", retry_after)
                await asyncio.sleep(retry_after)
                continue
            messages = response.get("messages") or []
            return [m for m in messages if float(m.get("ts", "0")) > float(after_ts)]

    async def _wait_for_reply(self, after_ts: str) -> str:
        """Poll until a human posts in the thread after *after_ts*; return its text."""
        while True:
            for message in await self._fetch_after(after_ts):
                if message.get("bot_id") or message.get("subtype") not in _HUMAN_SUBTYPES:
                    continue
                logger.info("Received reply for thread %s.", self._thread_ts)
                return _reply_text(message)
            await asyncio.sleep(self._interval)

    async def send_and_wait(self, message: str, label: str | None = None) -> str:
        """
        Post *message* and block until a human replies in the thread.

        Only replies posted after this message count, so earlier answers in a
        continued thread are never returned twice.

        Returns:
            The text of the first human reply.
        """
        last_ts = await self._post(message, label)
        logger.info("Awaiting reply on thread %s.", self._thread_ts)
        return await self._wait_for_reply(last_ts)

    async def send_only(self, message: str, label: str | None = None) -> str:
        """Post *message* without waiting for a reply."""
        await self._post(message, label)
        return "Notification sent."
