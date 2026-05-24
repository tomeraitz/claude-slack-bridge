"""Tests for plugin/skills/verify-bridge/SKILL.md.

The skill itself is markdown for an LLM to follow, but its core logic is a
small `.mcp.json` check. We mirror that check here and assert behavior
against tmp dirs for the three branches: success, missing file, key absent.

If SKILL.md changes its check or its messages, update the constants and
helper below to match.
"""

import json
from pathlib import Path

FAILURE_MESSAGE = (
    "`mcp__claude-slack-bridge` is not installed in this repo. "
    "Add a `claude-slack-bridge` entry under `mcpServers` in `.mcp.json` "
    "(see the project README for the exact docker-exec snippet), "
    "then re-run the calling workflow."
)
SUCCESS_MESSAGE = "verify-bridge: ok (claude-slack-bridge declared in .mcp.json)"


def verify_bridge(cwd: Path) -> tuple[int, str]:
    """Mirror of plugin/skills/verify-bridge/SKILL.md.

    Returns (exit_code, message). 0 = ok, non-zero = fix-it.
    """
    mcp_path = cwd / ".mcp.json"
    if not mcp_path.exists():
        return 1, FAILURE_MESSAGE
    cfg = json.loads(mcp_path.read_text())
    if "claude-slack-bridge" not in (cfg.get("mcpServers") or {}):
        return 1, FAILURE_MESSAGE
    return 0, SUCCESS_MESSAGE


class TestVerifyBridge:
    def test_ok_when_bridge_declared(self, tmp_path):
        (tmp_path / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"claude-slack-bridge": {"command": "docker"}}})
        )
        code, msg = verify_bridge(tmp_path)
        assert code == 0
        assert msg == SUCCESS_MESSAGE

    def test_fail_when_mcp_json_missing(self, tmp_path):
        code, msg = verify_bridge(tmp_path)
        assert code != 0
        assert msg == FAILURE_MESSAGE

    def test_fail_when_bridge_key_missing(self, tmp_path):
        (tmp_path / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"some-other-server": {}}})
        )
        code, msg = verify_bridge(tmp_path)
        assert code != 0
        assert msg == FAILURE_MESSAGE
