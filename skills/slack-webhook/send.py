#!/usr/bin/env python3
"""
send.py — post a message to Slack through an incoming webhook.

    send.py "Deploy finished."
    send.py --to design --mention U0123ABCDEF "Which logo?"
    send.py --plain ":tada: shipped"

A webhook URL is a write-only pipe into one channel: it can post, and it can do
nothing else — no reading, no other channels, no files. That makes it the
smallest credential that still gets a message into Slack looking like it came
from Claude, which is the whole reason to prefer it over a bot token.

The flip side is that nothing here can read a reply. If you need an answer back,
you need the token-based skill (``slack-as-claude``) or the MCP bridge.

Runs in Claude's code-execution sandbox, so it uses ``urllib`` only — nothing to
install. The sandbox must allow egress to ``hooks.slack.com``; note that a
``slack.com`` allowlist entry does NOT cover that subdomain.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"

# Slack caps a markdown block at 12,000 characters.
MAX_BLOCK_CHARS = 11500

EXIT_ERROR = 1


class SendError(RuntimeError):
    """The message could not be sent."""


def load_webhooks() -> dict[str, str]:
    """Return the {name: url} webhook map, newest source winning.

    ``SLACK_WEBHOOK_URL`` in the environment defines/overrides ``default``, so
    the URL can stay out of the uploaded bundle if you'd rather paste it per
    conversation.
    """
    webhooks: dict[str, str] = {}

    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SendError(f"{CONFIG_PATH.name} is not valid JSON: {exc}") from None
        raw = data.get("webhooks", data)
        if isinstance(raw, str):
            webhooks["default"] = raw
        elif isinstance(raw, dict):
            webhooks.update(
                {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}
            )

    from_env = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if from_env:
        webhooks["default"] = from_env

    return {name: url for name, url in webhooks.items() if url.strip()}


def pick_webhook(webhooks: dict[str, str], name: str) -> str:
    """Return the URL registered under *name*, with a useful error if absent."""
    if not webhooks:
        raise SendError(
            "No webhook configured. Put one in config.json "
            '({"webhooks": {"default": "https://hooks.slack.com/services/…"}}) '
            "or set SLACK_WEBHOOK_URL."
        )
    url = webhooks.get(name)
    if not url:
        known = ", ".join(sorted(webhooks)) or "none"
        raise SendError(f"No webhook named {name!r}. Configured: {known}.")
    if not url.startswith("https://hooks.slack.com/"):
        raise SendError(
            f"{name!r} doesn't look like an incoming webhook URL "
            "(expected https://hooks.slack.com/services/…)."
        )
    return url


def build_payload(text: str, mention: str = "", plain: bool = False) -> dict:
    """Build the webhook body.

    Default is a ``markdown`` block, so Claude's headings, lists, tables, and
    fenced code render properly. ``plain`` sends Slack's own mrkdwn dialect
    instead — right for one-liners and raw ``:emoji:``/``<url|link>`` syntax.

    A mention is always its own ``section`` block: ``<@U…>`` inside a
    ``markdown`` block renders as literal text and notifies nobody.
    """
    body = text if text.strip() else "_(no content)_"
    if len(body) > MAX_BLOCK_CHARS:
        body = body[:MAX_BLOCK_CHARS] + "\n\n_…truncated._"

    if plain:
        return {"text": f"<@{mention}> {body}" if mention else body}

    blocks: list[dict] = []
    if mention:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"<@{mention}>"}}
        )
    blocks.append({"type": "markdown", "text": body})

    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "New message")
    preview = first[:150] + "…" if len(first) > 150 else first
    return {
        "text": f"<@{mention}> {preview}" if mention else preview,
        "blocks": blocks,
    }


def send(url: str, payload: dict) -> str:
    """POST *payload* to the webhook. Returns Slack's response body ("ok")."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode().strip()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace").strip()
        raise SendError(f"HTTP {exc.code}: {detail or exc.reason}") from None
    except urllib.error.URLError as exc:
        raise SendError(
            f"cannot reach hooks.slack.com ({exc.reason}). In Claude on the web "
            "this means egress is off, or the allowlist has slack.com but not "
            "hooks.slack.com — they're separate hosts, and the setting only "
            "applies to NEW chats."
        ) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Send a message to Slack via an incoming webhook."
    )
    # Optional so --list works on its own; required for an actual send.
    parser.add_argument(
        "message", nargs="?", help="the message (markdown by default)"
    )
    parser.add_argument(
        "--to", default="default", help="which configured webhook to use"
    )
    parser.add_argument(
        "--mention", default="", help="Slack member ID to @-mention, e.g. U0123ABCDEF"
    )
    parser.add_argument(
        "--plain", action="store_true",
        help="send as Slack mrkdwn text instead of a markdown block",
    )
    parser.add_argument(
        "--list", action="store_true", help="list configured webhooks and exit"
    )
    args = parser.parse_args(argv)

    try:
        webhooks = load_webhooks()
        if args.list:
            print("\n".join(sorted(webhooks)) or "none configured")
            return 0
        if args.message is None:
            raise SendError("nothing to send — pass the message as an argument.")
        url = pick_webhook(webhooks, args.to)
        result = send(url, build_payload(args.message, args.mention.lstrip("@"), args.plain))
    except SendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(f"sent to {args.to} ({result})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
