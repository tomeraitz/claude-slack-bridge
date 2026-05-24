# Architecture

How the bridge is structured under the hood — read this if you want to extend it, debug it, or just understand why it's split the way it is.

---

## Daemon + session model

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

## How it works (Claude → Slack lifecycle)

1. **Daemon starts** (`docker compose up -d`): `SlackDaemon` connects to Slack via Socket Mode and opens a Unix domain socket at `/tmp/slack-bridge.sock` inside the container.
2. **Claude calls `ask_on_slack`**: a session process (`session.py`) is already running inside the container via `docker exec`. It posts the message to Slack via the HTTP API using `SLACK_CHANNEL` from the project's `.mcp.json`.
3. **Session registers with daemon**: the session connects to `/tmp/slack-bridge.sock` and sends `REGISTER {thread_ts}`. It then blocks — no polling, the OS wakes it when data arrives.
4. **User replies in Slack**: the Socket Mode event arrives at the daemon. The daemon looks up the registered session for that `thread_ts`, writes the reply text to the Unix socket, and closes the connection.
5. **Session unblocks**: reads the reply from the socket and returns it to Claude Code.

Multiple concurrent sessions each have their own `docker exec` process and their own socket connection to the daemon. Replies are routed by `thread_ts` so they always reach the correct waiter.

---

## How it works (Slack → Claude lifecycle)

1. **User tags the bot** in a Slack channel (e.g. `@claude-bot fix the login bug`).
2. **Daemon receives the event** via Socket Mode and looks up the channel in `projects.json` to find the matching project directory (and optional `plugin_dir`).
3. **Daemon parses an optional `[label]` prefix** — if present and the label resolves to a sibling git checkout, the daemon routes the run to that worktree instead of the channel's default `path`. See [slack-to-claude-projects.md](slack-to-claude-projects.md#worktrees) for the full rules.
4. **Daemon spawns `claude -p`** in the resolved working directory, streaming `--output-format stream-json --verbose` so the daemon can forward assistant text, thinking, and tool calls to Slack as they arrive.
5. **Daemon posts the response** as a thread reply on the original message. Subsequent replies in that thread are routed back into the same session.

---

## Two-file configuration design

The daemon uses two separate config files, kept intentionally separate:

| File | What it stores | Updated |
|---|---|---|
| `.env` | Secrets and runtime behavior — Slack tokens, security settings, timeouts | Set once at deployment |
| `projects.json` | Channel → project routing table | Updated as projects are added or removed |

**Why separate?** `.env` contains credentials that must never be committed. `projects.json` is a routing table — it changes frequently as teams onboard new projects, and it contains no secrets. Keeping them separate means you can share or version-control `projects.json` safely (if it contains no sensitive paths) without touching your secrets file.

Both files are gitignored by default.

---

## Project structure

```
claude-slack-two-way/
├── src/
│   ├── main.py            # Daemon entry point — starts SlackDaemon
│   ├── session.py         # Session entry point — MCP stdio server (docker exec target)
│   ├── slack_daemon.py    # Slack Socket Mode + Unix socket server
│   ├── session_broker.py  # Unix socket client — posts message, awaits reply
│   ├── mcp_server.py      # Registers the ask_on_slack MCP tool
│   └── config.py          # Environment variable validation (pydantic-settings)
├── plugin/                # The full-process Claude Code plugin (see docs/full-process-plugin.md)
│   ├── plugin.json
│   └── skills/
├── docs/
│   ├── architecture.md             # (this file)
│   ├── security.md                 # Access control + full env var reference
│   ├── slack-to-claude-projects.md # Tag-the-bot flow, projects.json, worktrees
│   ├── full-process-plugin.md      # /process-setup and /process workflow
│   ├── slack-setup.md              # Slack app creation
│   ├── github-setup.md             # PAT for /process GitHub workflow
│   └── mcp-client-setup.md         # Wiring .mcp.json in a project
├── projects.json          # Channel → project path mapping (gitignored)
├── projects.json.example  # Template for projects.json
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
