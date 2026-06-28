#!/usr/bin/env python3
"""Preview the live Slack status message locally — no Slack, no daemon, no restart.

Feeds a scripted sequence of claude stream-json events through the real
_ProgressTracker, renders the blocks the daemon would post via the real
_ProgressReporter, and prints a text approximation of how Slack would show it.

Run:  ./.venv/bin/python tools/preview_status.py
Use it to eyeball layout changes before merging/deploying.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from claude_handler import _ProgressTracker  # noqa: E402
from slack_daemon import _ProgressReporter  # noqa: E402


def _assistant(*blocks, usage=None, model=None):
    msg = {"content": list(blocks)}
    if usage is not None:
        msg["usage"] = usage
    if model is not None:
        msg["model"] = model
    return {"type": "assistant", "message": msg}


def tool(name, **inp):
    return {"type": "tool_use", "name": name, "input": inp}


def text(t):
    return {"type": "text", "text": t}


def tool_error(msg):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": True, "content": msg}]}}


# A representative run: model+usage, edits with churn, a long bash command,
# reads, a skill, two MCP tools, a subagent, todos, an error.
SCENARIO = [
    _assistant(text("Now it's clear — I'll wire the tracker into the daemon and add tests."),
               model="claude-opus-4-8",
               usage={"input_tokens": 12000, "output_tokens": 4200,
                      "cache_read_input_tokens": 368000, "cache_creation_input_tokens": 0}),
    _assistant(tool("Read", file_path="/proj/src/claude_handler.py")),
    _assistant(tool("Edit", file_path="/proj/src/claude_handler.py",
                    old_string="a\nb\nc\nd", new_string="a\nB")),
    _assistant(tool("Edit", file_path="/proj/src/slack_daemon.py",
                    old_string="x", new_string="x\ny\nz")),
    _assistant(tool("Write", file_path="/proj/tools/preview_status.py",
                    content="\n".join(["line"] * 40))),
    _assistant(tool("Bash",
                    command="sleep 8; cat /tmp/claude-1001/claude-1001/-home-node-proj-claude-slack-bridge/tasks/bszwg699i.output 2>/dev/null | tail -10")),
    _assistant(tool("Skill", skill="superpowers:brainstorming")),
    _assistant(tool("mcp__plugin_playwright_playwright__browser_navigate", url="http://localhost:3000")),
    _assistant(tool("mcp__claude_ai_GoDaddy__domains_suggest", query="example")),
    _assistant(tool("Task", description="review the diff")),
    tool_error("pytest: 1 failed in test_progress.py"),
    _assistant(tool("TodoWrite", todos=[
        {"content": "Add per-file churn", "status": "completed"},
        {"content": "Wire ctx used/window", "status": "in_progress"},
        {"content": "Update tests", "status": "pending"},
        {"content": "Preview + deploy", "status": "pending"},
    ])),
    _assistant(tool("Grep", pattern="def _meta")),
]


class _CaptureClient:
    def __init__(self):
        self.blocks = None

    async def chat_postMessage(self, **kw):
        self.blocks = kw.get("blocks")
        return {"ts": "1.1"}

    async def chat_update(self, **kw):
        self.blocks = kw.get("blocks")


def render(blocks) -> str:
    out = []
    for b in blocks or []:
        if b["type"] == "section":
            out.append(b["text"]["text"])
        elif b["type"] == "context":
            for el in b["elements"]:
                # Slack renders context blocks small/grey — prefix to suggest "muted".
                for line in el["text"].splitlines():
                    out.append(f"\x1b[90m{line}\x1b[0m")
        out.append("\x1b[90m" + "·" * 60 + "\x1b[0m")
    return "\n".join(out)


def show(title, progress):
    client = _CaptureClient()
    asyncio.run(_ProgressReporter(client, "C", "T")(progress))
    print(f"\n\x1b[1m=== {title} ===\x1b[0m")
    print(render(client.blocks))


def main():
    t = _ProgressTracker(start=0.0)
    for ev in SCENARIO:
        t.ingest(ev)
    show("LIVE (while running)", t.snapshot(now=134.0))
    show("FINAL SUMMARY (done)", t.snapshot(now=134.0, done=True))


if __name__ == "__main__":
    main()
