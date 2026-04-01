# Claude ↔ Slack Bridge

A two-way bridge between Claude Code and Slack:

- **Claude → Slack:** Claude pauses mid-task, asks a question via Slack, waits for your reply, and resumes.
- **Slack → Claude:** Tag the bot in a Slack channel and Claude runs with full project context — it knows which project to work on based on the channel.

```
Claude Code  ──ask_on_slack──▶  Slack channel  ──your reply──▶  Claude Code resumes
Slack @bot   ──────────────────▶  claude -p (in project dir) ──▶  reply in thread
```

---
![slack-claude-small](https://github.com/user-attachments/assets/d4460f40-5c68-48a0-8fc5-9b386881a765)



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

### 4. Add the Slack communication rule to your `CLAUDE.md`

To make Claude automatically use Slack for all communication once it sends its first message, add the following to your project's `CLAUDE.md`:

```markdown
Once you use `mcp__claude-slack-bridge__ask_on_slack` for the first time in a conversation, ALL further communication with the user must go through that tool. Do not use `AskUserQuestion`, and do not ask questions or request feedback as text in the terminal. Continue communicating exclusively via Slack until the user explicitly tells you to switch back to the terminal.
```

Without this, Claude will only use Slack when it decides to — with it, Claude locks in to Slack after the first message and stays there for the rest of the session.

That's it. Open the project in Claude Code and Claude will have access to `ask_on_slack`.

---

## Configuration

### `.env` (daemon — set once, shared across all projects)

| Variable | Required | Description |
|---|---|---|
| `SLACK_BOT_TOKEN` | Yes | Bot OAuth token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | Yes | Socket Mode app token (`xapp-...`) |
| `PROJECTS_DIR` | Yes | Absolute path to the parent directory containing all your projects |

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

## Slack → Claude (Project-Aware Bot)

You can also tag the bot directly in Slack to interact with a project. The bot detects which project to use based on the channel.

### How it works

1. You tag `@claude-bot` in a Slack channel (e.g. `#my-project`).
2. The daemon looks up the channel in `projects.json` to find the matching project directory.
3. It runs `claude -p` from that project directory inside the container — so Claude sees the project's `CLAUDE.md`, codebase, and full context.
4. The response is posted back as a thread reply.
5. You can continue the conversation by replying in the thread.

### Setup

#### 1. Set `PROJECTS_DIR` in `.env`

Point it to the parent directory that contains all your projects:

```
PROJECTS_DIR=C:\Users\you\projects
```

This directory is mounted into the container at `/projects/`.

#### 2. Create `projects.json`

Map each Slack channel to its project folder name (relative to `/projects/` inside the container):

```json
{
  "#my-project-channel": "/projects/my-project",
  "#another-channel": "/projects/another-project"
}
```

> **Tip:** The folder names must match the directory names inside `PROJECTS_DIR`. For example, if `PROJECTS_DIR=C:\Users\you\projects` and you have `C:\Users\you\projects\my-project`, then the container path is `/projects/my-project`.

See `projects.json.example` for a template.

#### 3. Rebuild

```bash
docker compose up -d --build
```

#### Adding new projects

Just add a line to `projects.json` and restart the daemon. No changes to `docker-compose.yml` needed.

---

## `projects.json` — Channel → Project Routing

`projects.json` maps Slack channel keys to project configurations. It is gitignored and lives at the repo root.

### Channel key formats

| Format | Example | When to use |
|---|---|---|
| `#channel-name` | `#my-project` | Named public/private channels |
| Channel ID | `C012AB3CD45` | When you know the raw Slack channel ID |
| DM channel ID | `D095AGC9LLF` | Direct messages to the bot |

### Entry formats

**Plain string (legacy — still fully supported):**

```json
{
  "#my-project": "/path/to/project"
}
```

**Dict with optional `plugin_dir`:**

```json
{
  "#my-project": {
    "path": "/path/to/project",
    "plugin_dir": "/path/to/skill"
  }
}
```

Both formats can coexist in the same file. See `projects.json.example` for a full template.

### `plugin_dir` — Loading Claude Code Skills

When `plugin_dir` is set, the daemon passes `--plugin-dir <dir>` to `claude -p` so that a project-specific skill is loaded for every message in that channel.

**Use case:** You have a Claude Code skill — a directory with custom slash commands and a `CLAUDE.md` — that you want Claude to use automatically when someone messages the bot in a particular channel or DM.

**Worked example — PE Support Skill:**

The `pe-support-skill` handles Platform Engineering support tickets. It lives at `/Users/yen.chuang/repo/pe-support-skill` and its working directory is `/Users/yen.chuang/repo/pe-support-skill/pe-support-workspace`. When someone DMs the bot, the daemon runs:

```
claude -p --plugin-dir /Users/yen.chuang/repo/pe-support-skill \
          --dangerously-skip-permissions \
          --output-format json
```

from the workspace directory, so the skill's commands and `CLAUDE.md` are active for every response.

`projects.json` entry:

```json
{
  "D095AGC9LLF": {
    "path": "/Users/yen.chuang/repo/pe-support-skill/pe-support-workspace",
    "plugin_dir": "/Users/yen.chuang/repo/pe-support-skill"
  }
}
```

---

## Two-File Configuration Design

The daemon uses two separate config files, kept intentionally separate:

| File | What it stores | Updated |
|---|---|---|
| `.env` | Secrets and runtime behavior — Slack tokens, security settings, timeouts | Set once at deployment |
| `projects.json` | Channel → project routing table | Updated as projects are added or removed |

**Why separate?** `.env` contains credentials that must never be committed. `projects.json` is a routing table — it changes frequently as teams onboard new projects, and it contains no secrets. Keeping them separate means you can share or version-control `projects.json` safely (if it contains no sensitive paths) without touching your secrets file.

Both files are gitignored by default.

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
├── projects.json          # Channel → project path mapping (gitignored)
├── projects.json.example  # Template for projects.json
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
