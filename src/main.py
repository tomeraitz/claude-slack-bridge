"""
main.py — Daemon entry point.

Starts the SlackDaemon, which holds:
  - One Slack Socket Mode WebSocket connection (receives all reply events)
  - One Unix domain socket server (session processes connect here to wait for replies)

Session processes are started per Claude session via:
    docker exec -i -e SLACK_CHANNEL=#channel claude-slack-bridge python session.py

They post messages to Slack themselves and register with this daemon to
receive the reply when it arrives — no polling, OS-level blocking I/O.
"""

import asyncio
import logging

from config import Config
from slack_daemon import SlackDaemon

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def run(config: Config) -> None:
    """
    Start the daemon.

    Args:
        config: Validated application configuration.
    """
    daemon = SlackDaemon(
        bot_token=config.slack_bot_token,
        app_token=config.slack_app_token,
    )
    logger.info("Starting Claude <-> Slack Daemon.")
    await daemon.start()


if __name__ == "__main__":
    cfg = Config()  # type: ignore[call-arg]
    asyncio.run(run(cfg))
