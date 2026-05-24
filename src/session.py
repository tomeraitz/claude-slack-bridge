"""
session.py — Session entry point (docker exec target).

Each Claude Code or Cursor IDE session starts one instance of this process via:

    docker exec -i -e SLACK_CHANNEL=#my-channel claude-slack-bridge python session.py

For Cursor IDE sessions, pass ``CLIENT_ID=cursor`` as an additional env var so
the session uses the cursor-bot Slack credentials instead of the default claude-bot:

    docker exec -i -e SLACK_CHANNEL=#my-channel -e CLIENT_ID=cursor claude-slack-bridge python session.py

The ``CLIENT_ID`` env var selects which Slack bot identity is used to post
messages. ``CLIENT_ID=claude`` (the default) uses ``SLACK_BOT_TOKEN``;
``CLIENT_ID=cursor`` uses ``CURSOR_SLACK_BOT_TOKEN``.

The process runs an MCP stdio server with the ``ask_on_slack`` tool.
It posts messages to the channel in SLACK_CHANNEL and waits for replies
via the daemon's Unix socket — zero polling, OS-level blocking.
"""

import asyncio
import logging

from fastmcp import FastMCP
from slack_bolt.async_app import AsyncApp

from config import Config
from mcp_server import MCPServer
from session_broker import SessionBroker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def run(config: Config) -> None:
    """
    Wire the session components and run the MCP stdio server.

    Args:
        config: Validated configuration (reads SLACK_CHANNEL from env,
                overridden per-project via ``docker exec -e``).
                When ``config.client_id == "cursor"``, the session uses
                ``CURSOR_SLACK_BOT_TOKEN`` to post messages so they appear
                under the cursor-bot identity in Slack.
    """
    if config.client_id == "cursor":
        bot_token = config.cursor_slack_bot_token
        if not bot_token:
            raise RuntimeError(
                "CLIENT_ID=cursor requires CURSOR_SLACK_BOT_TOKEN to be set."
            )
    else:
        bot_token = config.slack_bot_token

    app = AsyncApp(token=bot_token)

    async def post_message(
        text: str,
        thread_ts: str | None = None,
        label: str | None = None,
    ) -> str:
        if thread_ts is None and label:
            text = f"*[{label}]* {text}"
        kwargs: dict = dict(
            channel=config.slack_channel,
            text=f"<!channel> {text}",
            mrkdwn=True,
        )
        if thread_ts is not None:
            kwargs["thread_ts"] = thread_ts
        response = await app.client.chat_postMessage(**kwargs)
        if not response.get("ok"):
            raise RuntimeError(f"Slack API error: {response.get('error')}")
        ts: str = response["ts"]
        logger.info("Posted to %s, thread_ts=%s", config.slack_channel, thread_ts or ts)
        return thread_ts or ts

    broker = SessionBroker(
        post_message=post_message,
        timeout_minutes=config.timeout_limit_minutes,
    )
    mcp_server = MCPServer(broker=broker)
    mcp = FastMCP(name="ClaudeSlackBridge")
    mcp_server.register(mcp)

    logger.info("Session started for channel %s.", config.slack_channel)
    await mcp.run_async()


if __name__ == "__main__":
    cfg = Config()  # type: ignore[call-arg]
    asyncio.run(run(cfg))
