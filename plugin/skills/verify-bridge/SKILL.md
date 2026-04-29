---
name: verify-bridge
description: "Precondition check that mcp__claude-slack-bridge is installed in the current repo. Reads cwd/.mcp.json and verifies a `claude-slack-bridge` entry exists under `mcpServers`. Exits non-zero with a fix-it message if the entry is missing. Use as a standalone precondition for /process-setup, /process, or any flow that depends on the Slack bridge MCP being declared. Does not check whether the bridge container is running — only that the repo declares the server."
---

# verify-bridge — confirm the Slack bridge MCP is declared in this repo

Verify that `mcp__claude-slack-bridge` is installed in the current repo by reading `cwd/.mcp.json`. The file must exist and must contain a server entry whose key is `claude-slack-bridge` (the MCP tool prefix `mcp__claude-slack-bridge__*` is derived from this key).

```python
import json, os
mcp_path = os.path.join(os.getcwd(), ".mcp.json")
if not os.path.exists(mcp_path):
    # hard fail — see message below
    ...
with open(mcp_path) as f:
    cfg = json.load(f)
if "claude-slack-bridge" not in (cfg.get("mcpServers") or {}):
    # hard fail — see message below
    ...
```

If either check fails, print this exact message and exit non-zero. Do not offer to write the entry yourself, do not continue:

> `mcp__claude-slack-bridge` is not installed in this repo. Add a `claude-slack-bridge` entry under `mcpServers` in `.mcp.json` (see the project README for the exact docker-exec snippet), then re-run the calling workflow.

Do not check whether the bridge container is *running* — only that the repo declares the server. Runtime health is the caller's problem, not this skill's.

On success, print one short confirmation line and exit zero:

> verify-bridge: ok (claude-slack-bridge declared in .mcp.json)
