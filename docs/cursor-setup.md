# Cursor IDE MCP Setup — Connecting Cursor to the Bridge

This guide explains how to configure Cursor IDE to use the Claude-Slack-Bridge as an MCP server. Cursor sessions use the same daemon + session model as Claude Code but post messages under a separate **cursor-bot** identity in Slack.

---

## Prerequisites

- The bridge daemon container is already running. If not, follow the main [README](../README.md) first.
- You have created **two** Slack Apps — one for claude-bot (existing) and one for cursor-bot (new). See Step 1 below.
- `CURSOR_SLACK_BOT_TOKEN` and `CURSOR_SLACK_APP_TOKEN` are set in your `.env` file and the container has been restarted.

---

## How It Works

The bridge uses the same **daemon + session** model as Claude Code:

- The **daemon** (persistent container) holds two Slack Socket Mode WebSocket connections — one for claude-bot and one for cursor-bot. Both share the same Unix socket server and reply-routing logic.
- Each Cursor session runs **`session.py`** inside the container via `docker exec`. Passing `CLIENT_ID=cursor` tells `session.py` to use the cursor-bot credentials, so messages appear under the cursor-bot name and avatar in Slack.
- Replies from Slack are routed back to the waiting Cursor session by `thread_ts`, exactly as they are for Claude Code sessions.

---

## Step 1 — Create a Second Slack App for cursor-bot

You need a separate Slack App for cursor-bot so that Cursor sessions are visually distinct from Claude Code sessions in Slack.

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click **Create New App → From scratch**.
2. Name it (e.g. `cursor-bot`) and select your workspace.
3. Under **Socket Mode**, enable Socket Mode and generate an **App-Level Token** (`xapp-...`) with the `connections:write` scope. Save this as `CURSOR_SLACK_APP_TOKEN`.
4. Under **OAuth & Permissions**, add the following Bot Token Scopes:
   - `chat:write`
   - `channels:history`
   - `app_mentions:read`
5. Install the app to your workspace and copy the **Bot User OAuth Token** (`xoxb-...`). Save this as `CURSOR_SLACK_BOT_TOKEN`.
6. Invite the cursor-bot to your Slack channel: `/invite @cursor-bot` in the channel.

For general Slack App creation guidance, see [docs/slack-setup.md](slack-setup.md).

---

## Step 2 — Add Cursor Credentials to `.env`

Open the `.env` file in your bridge repository and add the two new lines after your existing Slack tokens:

```
CURSOR_SLACK_BOT_TOKEN=xoxb-...
CURSOR_SLACK_APP_TOKEN=xapp-...
```

Then restart the container to pick up the new credentials:

```bash
docker compose up -d --build
```

The daemon will now start two Socket Mode connections — one for claude-bot and one for cursor-bot.

---

## Step 3 — Add `.cursor/mcp.json` to Your Project

Create `.cursor/mcp.json` in the root of any project where you want Cursor to have access to `ask_on_slack`:

```json
{
  "mcpServers": {
    "claude-slack-bridge": {
      "command": "docker",
      "args": [
        "exec", "-i",
        "-e", "SLACK_CHANNEL",
        "-e", "TIMEOUT_LIMIT_MINUTES",
        "-e", "CLIENT_ID",
        "claude-slack-bridge",
        "python", "session.py"
      ],
      "env": {
        "SLACK_CHANNEL": "#your-project-channel",
        "TIMEOUT_LIMIT_MINUTES": "5",
        "CLIENT_ID": "cursor"
      }
    }
  }
}
```

**Notes:**
- The project-level path is `.cursor/mcp.json`. For a global configuration that applies to all projects, use `~/.cursor/mcp.json`.
- `CLIENT_ID=cursor` is what tells the session process to post as cursor-bot.
- Add `.cursor/mcp.json` to your `.gitignore` — it contains your channel name and is project-specific.

```
# .gitignore
.cursor/mcp.json
```

---

## Step 4 — Verify the Setup

1. Make sure the daemon is running: `docker ps | grep claude-slack-bridge`
2. Open the project in Cursor IDE.
3. Open Cursor's MCP panel (Settings → MCP or the MCP sidebar) and confirm `claude-slack-bridge` is listed with the `ask_on_slack` tool available.
4. Trigger the tool from within Cursor (e.g. ask Cursor's AI to "ask on Slack which branch to use").
5. Check your Slack channel — the message should appear under the **cursor-bot** name/avatar.
6. Reply in the Slack thread — Cursor should receive the reply and resume.

---

## Environment Variables Reference

### `.env` — set once in the bridge repository

| Variable | Required | Description |
|---|---|---|
| `SLACK_BOT_TOKEN` | Yes | Bot OAuth token for claude-bot (`xoxb-...`) |
| `SLACK_APP_TOKEN` | Yes | Socket Mode app token for claude-bot (`xapp-...`) |
| `CURSOR_SLACK_BOT_TOKEN` | Yes (Cursor) | Bot OAuth token for cursor-bot (`xoxb-...`) |
| `CURSOR_SLACK_APP_TOKEN` | Yes (Cursor) | Socket Mode app token for cursor-bot (`xapp-...`) |

### `.cursor/mcp.json` `env` — set per project

| Variable | Required | Default | Description |
|---|---|---|---|
| `SLACK_CHANNEL` | Yes | — | Channel name or ID (e.g. `#my-project`) |
| `CLIENT_ID` | Yes | `claude` | Set to `cursor` to use cursor-bot credentials |
| `TIMEOUT_LIMIT_MINUTES` | No | `5` | Minutes to wait for a reply before timing out |

---

## Limitations

- **Slack → Cursor (Flow B) is not implemented.** Cursor has no CLI equivalent to `claude -p`, so the daemon cannot spawn a Cursor session in response to a top-level Slack message. This direction is deferred to a future feature.
- **MCP roots labelling may not work in Cursor.** The bridge uses `ctx.list_roots()` to derive a worktree label for visual identification in Slack. If Cursor does not send MCP roots, the label is silently omitted — sessions remain functional, just unlabelled.
- **Docker must be running on the same machine as Cursor.** The `docker exec` transport requires the bridge container to be reachable locally, same as for Claude Code.
- **Two Slack Apps must be created by the operator.** Each bot identity (Claude, Cursor) requires a separate Slack App registration with its own Bot Token and App-Level Token. This is a one-time setup step; no Dockerfile change is required since tokens are injected as env vars.
