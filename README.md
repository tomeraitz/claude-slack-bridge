# Claude ↔ Slack Bridge

An MCP server that lets Claude Code pause and ask a human a question via Slack — then resume once you reply.

```
Claude Code  ──ask_on_slack──▶  Slack channel  ──your reply──▶  Claude Code resumes
```

---

## What It Does

When Claude is mid-task and needs a human decision — approval, clarification, a missing credential — it calls the `ask_on_slack` MCP tool. The bridge:

1. Posts the question to a Slack channel.
2. Blocks Claude's execution and waits.
3. Captures your reply — **you must reply in the Slack thread, not in the channel directly**.
4. Returns the reply text to Claude, which continues from where it left off.

Multiple concurrent sessions and requests are all handled correctly — each is keyed to its own Slack thread so replies always reach the right waiter.

---

## Architecture

The bridge uses a **daemon + session** model to support multiple Claude Code sessions simultaneously:

- **Daemon** (persistent Docker container): holds one Slack Socket Mode WebSocket connection and a Unix domain socket server. Receives all Slack reply events and routes them to the correct waiting session.
- **Session** (started per Claude session via `docker exec`): runs the MCP stdio server, posts messages to Slack, and blocks on the Unix socket waiting for the daemon to forward the reply. Zero polling — OS-level blocking I/O.

```
Container (always running):
  main.py → SlackDaemon
    ├── Slack Socket Mode WebSocket
    └── Unix socket at /tmp/slack-bridge.sock

Per Claude session (docker exec):
  session.py
    ├── Posts message → Slack HTTP API  (uses SLACK_CHANNEL from .mcp.json)
    └── Awaits reply  → /tmp/slack-bridge.sock
```

This means `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` live only in `.env` (set once). Each project's `.mcp.json` only needs `SLACK_CHANNEL`.

---

## Quickstart

### 1. Create a Slack app and get tokens

Follow [docs/slack-setup.md](docs/slack-setup.md) to create a Slack app, get your `xoxb-` and `xapp-` tokens, and invite the bot to a channel.

### 2. Clone, configure, and start the daemon

```bash
git clone https://github.com/your-username/claude-slack-bridge.git
cd claude-slack-bridge
cp .env.example .env   # fill in SLACK_BOT_TOKEN and SLACK_APP_TOKEN
docker compose up -d --build
```

The container starts automatically on system boot (`restart: unless-stopped`) and uses Socket Mode — no public URL or inbound firewall rules needed.

**You only do this once.** The daemon stays running in the background and serves all your Claude Code projects.

### 3. Add `.mcp.json` to your Claude Code project

Create `.mcp.json` in the root of any project where you want Claude to be able to ask you questions:

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
        "SLACK_CHANNEL": "#your-project-channel",
        "TIMEOUT_LIMIT_MINUTES": "5"
      }
    }
  }
}
```

> **Important:** Add `.mcp.json` to your `.gitignore` — it contains your channel name and is project-specific.

That's it. Open the project in Claude Code and Claude will have access to `ask_on_slack`.

---

## Configuration

### `.env` (daemon — set once, shared across all projects)

| Variable | Required | Description |
|---|---|---|
| `SLACK_BOT_TOKEN` | Yes | Bot OAuth token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | Yes | Socket Mode app token (`xapp-...`) |

### `.mcp.json` (per project — set per Claude Code project)

| Variable | Required | Default | Description |
|---|---|---|---|
| `SLACK_CHANNEL` | Yes | — | Target channel name or ID (e.g. `#my-project`) |
| `TIMEOUT_LIMIT_MINUTES` | No | `5` | Minutes to wait before timing out |

Set `SLACK_CHANNEL` per project so each project posts to its own dedicated channel.

---

## The `ask_on_slack` Tool

Claude calls this tool automatically whenever it needs a human decision it cannot resolve from context.

**Input:** `message` — the question or statement to send.
**Output:** the text of your reply.
**Timeout:** raises an error if no reply arrives within `TIMEOUT_LIMIT_MINUTES`.

> **Reply in the thread.** When the message appears in Slack, click **Reply** to open the thread and type your answer there. A top-level message in the channel will not be picked up.

You can also prompt Claude explicitly:

> *"Ask on Slack whether you should overwrite the existing file."*

---

## Project Structure

```
claude-slack-two-way/
├── src/
│   ├── main.py            # Daemon entry point — starts SlackDaemon
│   ├── session.py         # Session entry point — MCP stdio server (docker exec target)
│   ├── slack_daemon.py    # Slack Socket Mode + Unix socket server
│   ├── session_broker.py  # Unix socket client — posts message, awaits reply
│   ├── mcp_server.py      # Registers the ask_on_slack MCP tool
│   └── config.py          # Environment variable validation (pydantic-settings)
├── docs/
│   ├── slack-setup.md        # Step-by-step Slack app creation guide
│   └── mcp-client-setup.md   # How to wire .mcp.json in a Claude Code project
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## How It Works (Internals)

1. **Daemon starts** (`docker compose up -d`): `SlackDaemon` connects to Slack via Socket Mode and opens a Unix domain socket at `/tmp/slack-bridge.sock` inside the container.
2. **Claude calls `ask_on_slack`**: a session process (`session.py`) is already running inside the container via `docker exec`. It posts the message to Slack via the HTTP API using `SLACK_CHANNEL` from the project's `.mcp.json`.
3. **Session registers with daemon**: the session connects to `/tmp/slack-bridge.sock` and sends `REGISTER {thread_ts}`. It then blocks — no polling, the OS wakes it when data arrives.
4. **User replies in Slack**: the Socket Mode event arrives at the daemon. The daemon looks up the registered session for that `thread_ts`, writes the reply text to the Unix socket, and closes the connection.
5. **Session unblocks**: reads the reply from the socket and returns it to Claude Code.

Multiple concurrent sessions each have their own `docker exec` process and their own socket connection to the daemon. Replies are routed by `thread_ts` so they always reach the correct waiter.

---

## Requirements

- Docker (with Docker Compose)
- A Slack workspace where you can create apps
- Claude Code (or any MCP-compatible client)

---

## License

MIT
