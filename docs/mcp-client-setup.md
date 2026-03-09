# MCP Client Setup — Connecting Claude Code to the Bridge

This guide explains how to configure a Claude Code project to use the bridge via an `.mcp.json` file.

---

## Prerequisites

The daemon container must be running before any Claude session can use the bridge.
If you haven't done this yet, follow the steps in the main [README](../README.md) first:

```bash
cp .env.example .env   # fill in SLACK_BOT_TOKEN and SLACK_APP_TOKEN
docker compose up -d --build
```

You only need to do this once. The container restarts automatically on system boot.

---

## How It Works

The bridge uses a **daemon + session** model:

- The **daemon** (persistent container) holds the Slack Socket Mode WebSocket and a Unix socket server at `/tmp/slack-bridge.sock` inside the container.
- Each Claude session runs **`session.py`** inside the container via `docker exec`. It posts messages to Slack and blocks on the Unix socket until the daemon delivers the reply — zero polling.

Your `.mcp.json` only needs `SLACK_CHANNEL` (and optionally `TIMEOUT_LIMIT_MINUTES`). The tokens (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`) are already inside the container from `.env`.

---

## Step 1 — Add `.mcp.json` to Your Project

Create a `.mcp.json` file in the root of any Claude Code project where you want the tool available:

```json
{
  "mcpServers": {
    "claude-slack-bridge": {
      "command": "docker",
      "args": [
        "exec", "-i",
        "-e", "SLACK_CHANNEL",
        "-e", "TIMEOUT_LIMIT_MINUTES",
        "claude-slack-bridge",
        "python", "session.py"
      ],
      "env": {
        "SLACK_CHANNEL": "#your-channel",
        "TIMEOUT_LIMIT_MINUTES": "5"
      }
    }
  }
}
```

> **Tip:** Set `SLACK_CHANNEL` per project so each project posts to its own dedicated channel.
> `TIMEOUT_LIMIT_MINUTES` is optional — omit it to use the default of `5`.

---

## Step 2 — Add `.mcp.json` to `.gitignore`

Your `.mcp.json` is project-specific. Add it to `.gitignore`:

```
# .gitignore
.mcp.json
.env
```

---

## Environment Variables Reference

### `.env` — set once in the bridge repository, shared by all projects

| Variable | Required | Description |
|---|---|---|
| `SLACK_BOT_TOKEN` | Yes | Bot OAuth token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | Yes | Socket Mode app token (`xapp-...`) |

### `.mcp.json` `env` — set per project

| Variable | Required | Default | Description |
|---|---|---|---|
| `SLACK_CHANNEL` | Yes | — | Channel name or ID (e.g. `#my-project`) |
| `TIMEOUT_LIMIT_MINUTES` | No | `5` | Minutes to wait for a reply before timing out |

---

## Verifying the Setup

1. Make sure the daemon is running: `docker ps | grep claude-slack-bridge`
2. Open a project that has `.mcp.json` in Claude Code.
3. Ask Claude: *"What MCP tools do you have available?"* — it should list `ask_on_slack`.
4. Ask Claude to use it: *"Ask on Slack whether I should use tabs or spaces."*
5. Check your Slack channel — the message should appear, and Claude will block until you reply in the thread.

---

## Multiple Projects

Each project can target a different Slack channel by setting a different `SLACK_CHANNEL` in its `.mcp.json`. All sessions share the same running daemon container — there is no conflict.

```
Project A → SLACK_CHANNEL=#backend  → posts to #backend
Project B → SLACK_CHANNEL=#frontend → posts to #frontend
```

No additional containers or configuration needed.
