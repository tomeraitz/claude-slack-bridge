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

- **`ask_on_slack` MCP tool** — Claude pauses mid-task, posts a question to Slack, blocks until you reply in the thread, then resumes. Multiple concurrent sessions are routed correctly by `thread_ts`.
- **Project-aware Slack bot** — `@claude-bot` in a Slack channel spawns `claude -p` in the matching project directory inside the container. Supports git worktrees via a `[label]` prefix.
- **Full-process plugin** — a turnkey feature-development workflow driven from Slack (`/process start` → pick a task → worktree → design → plan → run-plan → PR per step).

---

## Quickstart — Claude → Slack

### 1. Create a Slack app and get tokens

Follow [docs/slack-setup.md](docs/slack-setup.md) to create a Slack app, get your `xoxb-` and `xapp-` tokens, and invite the bot to a channel.

*(Optional)* If you plan to use the `/process` workflow — which opens GitHub PRs and reads review comments from inside the container — also follow [docs/github-setup.md](docs/github-setup.md) to create a fine-grained PAT and set `GITHUB_TOKEN`. Skip this if you don't need GitHub integration.

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
        "claude-slack-bridge",
        "python", "session.py"
      ],
      "env": {
        "SLACK_CHANNEL": "#your-project-channel"
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

Open the project in Claude Code and Claude will have access to `ask_on_slack`. Reply **in the Slack thread** (not the channel directly) and Claude resumes from where it left off.

---

## Quickstart — Slack → Claude

Tag the bot in a Slack channel and Claude runs inside the matching project directory.

### 1. Set `PROJECTS_DIR` in `.env`

```
PROJECTS_DIR=C:\Users\you\projects
```

This is the parent directory that contains all your projects. It's mounted into the container at `/projects/`.

### 2. Create `projects.json`

Map each Slack channel to a project folder:

```json
{
  "#my-project-channel": "/projects/my-project"
}
```

### 3. Rebuild

```bash
docker compose up -d --build
```

Then in Slack:

```
@claude-bot fix the login redirect bug
```

The bot replies in a thread. Continue the conversation by replying in that thread.

→ Full reference (channel formats, `plugin_dir`, worktrees, routing rules): **[docs/slack-to-claude-projects.md](docs/slack-to-claude-projects.md)**

---

## The full-process plugin

A turnkey feature-development workflow driven entirely from Slack. After a one-time `/process-setup` in your repo, you can start a feature from Slack with:

```
@claude-bot /process start
```

The bot lists your open tasks (from Notion, Linear, Jira, …), creates a git worktree for the one you pick, walks the work through your configured steps (typically **design → plan → run-plan**), opens a GitHub PR after each step, and waits for your approval in Slack before moving on.

→ Full guide: **[docs/full-process-plugin.md](docs/full-process-plugin.md)**

---

## Next steps

| Want to... | See |
|---|---|
| Tag the bot from Slack | [docs/slack-to-claude-projects.md#how-it-works](docs/slack-to-claude-projects.md#how-it-works) |
| Route channels to projects | [docs/slack-to-claude-projects.md#projectsjson--channel--project-routing](docs/slack-to-claude-projects.md#projectsjson--channel--project-routing) |
| Use git worktrees from Slack | [docs/slack-to-claude-projects.md#worktrees](docs/slack-to-claude-projects.md#worktrees) |
| Run the turnkey feature-dev workflow from Slack | [docs/full-process-plugin.md](docs/full-process-plugin.md) |
| Configure access control or see all env vars | [docs/security.md](docs/security.md) |
| Understand the daemon + session internals | [docs/architecture.md](docs/architecture.md) |
| Use the `/process` GitHub PR workflow | [docs/github-setup.md](docs/github-setup.md) |
| Wire `.mcp.json` in a Claude Code project | [docs/mcp-client-setup.md](docs/mcp-client-setup.md) |

---

## Requirements

- Docker (with Docker Compose)
- A Slack workspace where you can create apps
- Claude Code (or any MCP-compatible client)

---

## License

MIT
