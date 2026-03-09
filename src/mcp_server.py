"""
mcp_server.py — MCP server module.

Registers the ``ask_on_slack`` tool on a FastMCP instance. This class does not
own the FastMCP instance; it receives it via ``register()`` so that the entry
point retains full control over the server lifecycle.
"""

import logging
from typing import Any

from fastmcp import FastMCP

logger = logging.getLogger(__name__)


class MCPServer:
    """
    Registers MCP tools that bridge Claude Code to Slack.

    This class is intentionally thin: it owns only the tool definitions and
    delegates all Slack I/O to the broker.

    Args:
        broker: Any object with ``send_and_wait(message: str) -> str``.
                Injected at construction time for testability.
    """

    def __init__(self, broker: Any) -> None:
        self._broker = broker

    def register(self, mcp: FastMCP) -> None:
        """
        Register all MCP tools on the provided FastMCP instance.

        Call this once during application startup, before running the server.

        Args:
            mcp: The FastMCP server instance owned by ``main.py``.
        """
        mcp.tool()(self.ask_on_slack)
        logger.info("Registered 'ask_on_slack' tool on MCP server.")

    async def ask_on_slack(self, message: str) -> str:
        """
        Post a message to Slack and wait for a human reply.

        Use this tool whenever you need a human decision, clarification, or
        approval that cannot be determined from existing context. The tool
        blocks until a reply is received in the Slack thread (up to 5 minutes).

        Args:
            message: The question or message to send to the Slack channel.

        Returns:
            The text of the human's reply.

        Raises:
            RuntimeError: If no reply is received within 5 minutes.
        """
        logger.info("ask_on_slack called with message: %r", message)
        reply = await self._broker.send_and_wait(message)
        return reply
