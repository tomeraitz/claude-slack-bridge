"""
session.py — Session entry point (docker exec target).

Each Claude Code session starts one instance of this process via:

    docker exec -i -e SLACK_CHANNEL=#my-channel claude-slack-bridge python session.py

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
    """
    app = AsyncApp(token=config.slack_bot_token)

    async def post_message(text: str) -> str:
        response = await app.client.chat_postMessage(
            channel=config.slack_channel,
            text=f"<!channel> {text}",
            mrkdwn=True,
        )
        if not response.get("ok"):
            raise RuntimeError(f"Slack API error: {response.get('error')}")
        thread_ts: str = response["ts"]
        logger.info("Posted to %s, thread_ts=%s", config.slack_channel, thread_ts)
        return thread_ts

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
