"""
cli.py — Native command-line entry point (no Docker, no daemon).

    claude-slack-bridge send "Build finished."           # post, print thread ts
    claude-slack-bridge ask "Deploy to prod?"            # post, wait, print reply
    claude-slack-bridge mcp                              # MCP stdio server for Claude Code

Register the MCP server with Claude Code:

    claude mcp add claude-slack-bridge -e SLACK_CHANNEL="#my-channel" -- claude-slack-bridge mcp

Everything here talks to Slack directly with the bot token and reads replies by
polling the thread (see ``polling_broker.py``) — the Socket Mode daemon is not
needed. Tagging the bot from Slack (Slack → Claude) still requires the daemon.

Settings come from the environment, falling back to an env file: ``--env-file``,
else ``$CLAUDE_SLACK_BRIDGE_ENV``, else ``.env`` at the bridge repo root — never
the current directory, which for ``mcp`` is the Claude project, not the bridge.
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from pydantic_settings import BaseSettings

REPO_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger("claude-slack-bridge")


class CliConfig(BaseSettings):
    """Settings for the CLI. Unlike the daemon, no ``SLACK_APP_TOKEN`` is needed."""

    slack_bot_token: str
    slack_channel: str = ""
    slack_poll_interval: float = 3.0

    model_config = {"env_file_encoding": "utf-8", "extra": "ignore"}


def resolve_env_file(explicit: str | None) -> Path | None:
    """Pick the env file: explicit flag, then ``$CLAUDE_SLACK_BRIDGE_ENV``, then the repo's ``.env``."""
    if explicit:
        return Path(explicit).expanduser()
    from_env = os.environ.get("CLAUDE_SLACK_BRIDGE_ENV")
    if from_env:
        return Path(from_env).expanduser()
    default = REPO_ROOT / ".env"
    return default if default.is_file() else None


def load_config(env_file: Path | None) -> CliConfig:
    """Load settings; environment variables override the env file."""
    if env_file is not None and not env_file.is_file():
        raise SystemExit(f"error: env file not found: {env_file}")
    try:
        return CliConfig(_env_file=env_file)  # type: ignore[call-arg]
    except Exception:
        where = env_file or "the environment"
        raise SystemExit(f"error: SLACK_BOT_TOKEN is not set (looked in {where}).")


def read_message(arg: str) -> str:
    """Return the message argument, reading stdin when it is ``-``."""
    text = sys.stdin.read() if arg == "-" else arg
    if not text.strip():
        raise SystemExit("error: message is empty.")
    return text


def make_broker(config: CliConfig, channel: str | None, thread_ts: str | None):
    """Build a ``PollingBroker`` for *channel* (default: ``SLACK_CHANNEL``)."""
    from slack_sdk.web.async_client import AsyncWebClient

    from polling_broker import PollingBroker

    target = channel or config.slack_channel
    if not target:
        raise SystemExit("error: no channel — pass --channel or set SLACK_CHANNEL.")
    client = AsyncWebClient(token=config.slack_bot_token)
    return PollingBroker(
        client, target, thread_ts=thread_ts, interval=config.slack_poll_interval
    )


async def cmd_send(broker, message: str) -> int:
    await broker.send_only(message)
    print(broker.thread_ts)
    return 0


async def cmd_ask(broker, message: str, timeout: float | None) -> int:
    try:
        reply = await asyncio.wait_for(broker.send_and_wait(message), timeout=timeout)
    except asyncio.TimeoutError:
        print(f"error: no reply within {timeout:g}s (thread {broker.thread_ts}).", file=sys.stderr)
        return 2
    print(reply)
    return 0


async def cmd_mcp(broker) -> int:
    from fastmcp import FastMCP

    from mcp_server import MCPServer

    mcp = FastMCP(name="ClaudeSlackBridge")
    MCPServer(broker=broker).register(mcp)
    logger.info("MCP server started for channel %s.", broker.channel)
    await mcp.run_async()
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--channel", help="channel name or ID (default: $SLACK_CHANNEL)")
    common.add_argument("--env-file", help="env file with SLACK_BOT_TOKEN (default: bridge repo .env)")
    common.add_argument("-v", "--verbose", action="store_true", help="log progress to stderr")

    parser = argparse.ArgumentParser(
        prog="claude-slack-bridge",
        description="Talk to Slack from Claude Code or your shell — no Docker, no daemon.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_send = sub.add_parser("send", parents=[common], help="post a message, print its thread ts")
    p_send.add_argument("message", help="message text (Markdown), or - to read stdin")
    p_send.add_argument("--thread", help="post into this existing thread ts")

    p_ask = sub.add_parser("ask", parents=[common], help="post a question, wait for the reply, print it")
    p_ask.add_argument("message", help="question text (Markdown), or - to read stdin")
    p_ask.add_argument("--thread", help="ask in this existing thread ts")
    p_ask.add_argument("--timeout", type=float, default=None,
                       help="give up after N seconds (exit code 2); default: wait forever")

    sub.add_parser("mcp", parents=[common],
                   help="run the MCP stdio server (ask_on_slack, notify_on_slack)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Logs go to stderr: stdout is the MCP transport for `mcp` and the result for send/ask.
    quiet = args.command != "mcp" and not args.verbose
    logging.basicConfig(
        level=logging.WARNING if quiet else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    config = load_config(resolve_env_file(args.env_file))
    broker = make_broker(config, args.channel, getattr(args, "thread", None))

    try:
        if args.command == "send":
            return asyncio.run(cmd_send(broker, read_message(args.message)))
        if args.command == "ask":
            return asyncio.run(cmd_ask(broker, read_message(args.message), args.timeout))
        return asyncio.run(cmd_mcp(broker))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 — surface Slack errors as a clean message
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
