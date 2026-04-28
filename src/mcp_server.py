"""
mcp_server.py — MCP server module.

Registers the ``ask_on_slack`` tool on a FastMCP instance. This class does not
own the FastMCP instance; it receives it via ``register()`` so that the entry
point retains full control over the server lifecycle.
"""

import logging
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

from fastmcp import Context, FastMCP

logger = logging.getLogger(__name__)


async def _derive_worktree_label(ctx: Context) -> str | None:
    """
    Return the basename of the client's first MCP root, or None.

    The label is used to tag Slack posts so a user juggling multiple
    worktree sessions in one channel can tell threads apart.
    """
    try:
        roots = await ctx.list_roots()
    except Exception as exc:
        logger.debug("list_roots() unavailable: %s", exc)
        return None
    if not roots:
        return None
    path = unquote(urlparse(str(roots[0].uri)).path)
    name = PurePosixPath(path).name
    return name or None


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

    async def ask_on_slack(self, message: str, ctx: Context) -> str:
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
        label = await _derive_worktree_label(ctx)
        reply = await self._broker.send_and_wait(message, label)
        return reply
