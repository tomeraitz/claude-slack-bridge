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

### Access control (optional)

The daemon can restrict **who** can message the bot and **where**. Access control is off by default — leave `SECURITY_ENABLED` unset and you can skip this section entirely.

Set the following in `.env` to enable:

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECURITY_ENABLED` | No | `false` | Master switch. When `false`, all other `SECURITY_*` vars are ignored. |
| `SECURITY_STRICT_MODE` | No | `false` | `false` = empty allowlist means "allow all" for that dimension. `true` = empty allowlist means "deny all". |
| `SECURITY_ALLOWED_USERS` | No | *(empty)* | Comma-separated Slack user IDs permitted to use the bot (e.g. `U0123ABC,U0456DEF`). |
| `SECURITY_ALLOWED_CHANNELS` | No | *(empty)* | Comma-separated Slack channel IDs the bot will respond in. |
| `SECURITY_ADMIN_USERS` | No | *(empty)* | User IDs that bypass the channel allowlist (still subject to the user allowlist). |
| `SECURITY_REJECTION_MESSAGE` | No | `You are not authorized to use this bot.` | Reply sent to unauthorized users. |
| `SECURITY_LOG_UNAUTHORIZED` | No | `true` | Emit a warning log line on each denial. |

**Flexible vs strict mode**

- **Flexible** (`SECURITY_STRICT_MODE=false`, default): an empty list means "no restriction on that dimension". Useful when you only want to restrict users OR channels, not both.
- **Strict** (`SECURITY_STRICT_MODE=true`): an empty list means "deny everyone". Every permitted user and channel must be listed explicitly.

**Finding Slack IDs**

- **User ID** — click a profile → **Copy member ID** (starts with `U`).
- **Channel ID** — open channel details → scroll to the bottom (starts with `C`).

**Example — lock the bot to a specific team**

```env
SECURITY_ENABLED=true
SECURITY_STRICT_MODE=true
SECURITY_ALLOWED_USERS=U0123ABC,U0456DEF
SECURITY_ALLOWED_CHANNELS=C07ENG,C07DEVOPS
SECURITY_ADMIN_USERS=U0123ABC
```

With this config, only the two listed users can use the bot, only in the two listed channels, and the admin user can invoke the bot from any channel.

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
          --output-format stream-json --verbose
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

## Worktrees

The bridge understands `git worktree` checkouts so you can drive multiple branches of the same project from a single Slack channel without juggling configs. Worktrees flow in **both directions**:

- **Slack → Claude:** prefix a top-level message with `[<worktree>]` to route it to that worktree.
- **Claude → Slack:** when Claude (running inside a worktree) calls `ask_on_slack`, the bridge tags the first Slack post with the worktree name so concurrent sessions in the same channel are easy to tell apart.

### Calling a worktree from Slack

When you tag the bot in a channel, prepend the worktree name in square brackets:

```
@claude-bot [feature-auth] add a unit test for the new login flow
@claude-bot [hotfix] why is /healthz returning 500?
```

The bridge:

1. Parses the leading `[label]` from the message.
2. Looks for a directory named `<label>` *next to* the channel's default `path` and verifies it's a git checkout (has a `.git` file or directory).
3. Strips the `[label]` prefix and runs `claude -p` from that worktree's directory — so Claude sees that branch's code, `CLAUDE.md`, and uncommitted changes.
4. Locks the resulting Slack thread to that worktree. **Reply in the thread normally — no need to repeat the `[label]` prefix.**

Slack formatting around the tag is tolerated, so `*[feature-auth]* fix login` (bolded by Slack) works the same as the plain version.

Create worktrees with `git worktree add ../<label>` and they become routable instantly with no config edits. If the label doesn't resolve to a sibling git directory, the message falls back to the channel's default project path and a warning is logged — messages are never silently dropped.

> **Security note:** labels are restricted to `[A-Za-z0-9._-]` so a crafted message like `[../etc]` cannot escape the project parent directory.

### How Claude shows the worktree in replies

When Claude calls `ask_on_slack` from a session running inside a worktree, the MCP server reads the client's first MCP root and uses its basename as the worktree label. That label is prepended (bolded) to the **first** message of the Slack thread:

```
*[feature-auth]* Should I overwrite the existing migration file or generate a new one?
```

Subsequent posts in the same thread are not re-tagged — the prefix is only there so you can tell threads apart at a glance when several worktrees are asking questions in the same channel. If the MCP client doesn't expose roots (or none are set), the message is posted untagged.

### Example workflow

```bash
# In your project repo:
git worktree add ../myproject-feature-auth feature/auth
git worktree add ../myproject-hotfix       hotfix/login-500
```

In Slack:

```
You:        @claude-bot [myproject-feature-auth] write a test for the new login redirect
Claude-bot: *[myproject-feature-auth]* I've added tests/auth/test_login_redirect.py — want
            me to also cover the failure path?
You:        (reply in thread) yes, and run the suite to confirm
```

Meanwhile in the same channel:

```
You:        @claude-bot [myproject-hotfix] what's causing /healthz to 500?
Claude-bot: *[myproject-hotfix]* The healthcheck imports a module renamed in main but
            not yet on this branch. ...
```

Both threads run independently, each in its own worktree, with no config changes needed beyond `git worktree add`.

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
