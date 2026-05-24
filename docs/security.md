# Security & configuration

Full reference for `.env`, `.mcp.json`, and access control. The README's Quickstart covers the minimum to get running — this doc is the complete picture.

---

## What "secure" means here

By default the bridge will respond to **anyone in any channel where the Slack app is installed**. That is fine for a personal workspace but unsafe for a shared one — anybody who can DM the bot or invite it to a channel can drive Claude on your machine, read files in `PROJECTS_DIR`, and trigger `git`, `gh`, and other tools the daemon has access to.

The `SECURITY_*` env vars below let you turn the bridge into a **two-dimensional allowlist**:

- **Who** — only Slack user IDs in `SECURITY_ALLOWED_USERS` can invoke the bot. Everyone else gets `SECURITY_REJECTION_MESSAGE` and the request is dropped before a Claude session is ever spawned.
- **Where** — only channel IDs in `SECURITY_ALLOWED_CHANNELS` are routed to Claude. A message in any other channel is ignored, even from an allowed user (unless they are also in `SECURITY_ADMIN_USERS`, which bypasses the channel check).

Both checks run in the daemon **before** the message reaches a Claude session, so an unauthorized request never costs API tokens, never touches your filesystem, and never executes a tool.

For a hard lock-down, set `SECURITY_STRICT_MODE=true`: an empty allowlist then means "deny everyone" instead of "allow everyone", so you can't accidentally leave a dimension wide open. Combined with `SECURITY_LOG_UNAUTHORIZED=true`, every denial is logged so you can spot probing attempts.

Recommended posture for shared workspaces: `SECURITY_ENABLED=true`, `SECURITY_STRICT_MODE=true`, explicit user and channel lists, and admins only for the people who truly need cross-channel access.

---

## `.env` (daemon — set once, shared across all projects)

| Variable | Required | Description |
|---|---|---|
| `SLACK_BOT_TOKEN` | Yes | Bot OAuth token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | Yes | Socket Mode app token (`xapp-...`) |
| `PROJECTS_DIR` | Yes | Absolute path to the parent directory containing all your projects |
| `GITHUB_TOKEN` | No | Fine-grained PAT used by `gh` and `git push` inside the container. Required only for the `/process` GitHub-PR workflow. See [github-setup.md](github-setup.md). |
| `LOG_LEVEL` | No | Daemon log verbosity. `INFO` (default) logs lifecycle events only — sessions, channel mapping, errors. `DEBUG` additionally streams every Claude event (assistant text, thinking, `tool_use`, `tool_result`) as it arrives — useful for debugging Slack → Claude runs. |

---

## `.mcp.json` (per project — set per Claude Code project)

| Variable | Required | Default | Description |
|---|---|---|---|
| `SLACK_CHANNEL` | Yes | — | Target channel name or ID (e.g. `#my-project`) |
| `TIMEOUT_LIMIT_MINUTES` | No | `5` | Minutes to wait before timing out |

Set `SLACK_CHANNEL` per project so each project posts to its own dedicated channel.

See [mcp-client-setup.md](mcp-client-setup.md) for the full `.mcp.json` template and how Claude Code picks it up.

---

## Access control (optional)

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

### Flexible vs strict mode

- **Flexible** (`SECURITY_STRICT_MODE=false`, default): an empty list means "no restriction on that dimension". Useful when you only want to restrict users OR channels, not both.
- **Strict** (`SECURITY_STRICT_MODE=true`): an empty list means "deny everyone". Every permitted user and channel must be listed explicitly.

### Finding Slack IDs

- **User ID** — click a profile → **Copy member ID** (starts with `U`).
- **Channel ID** — open channel details → scroll to the bottom (starts with `C`).

### Example — lock the bot to a specific team

```env
SECURITY_ENABLED=true
SECURITY_STRICT_MODE=true
SECURITY_ALLOWED_USERS=U0123ABC,U0456DEF
SECURITY_ALLOWED_CHANNELS=C07ENG,C07DEVOPS
SECURITY_ADMIN_USERS=U0123ABC
```

With this config, only the two listed users can use the bot, only in the two listed channels, and the admin user can invoke the bot from any channel.

---

## The `ask_on_slack` tool

Claude calls this tool automatically whenever it needs a human decision it cannot resolve from context.

- **Input:** `message` — the question or statement to send.
- **Output:** the text of your reply.
- **Timeout:** raises an error if no reply arrives within `TIMEOUT_LIMIT_MINUTES`.

> **Reply in the thread.** When the message appears in Slack, click **Reply** to open the thread and type your answer there. A top-level message in the channel will not be picked up.

You can also prompt Claude explicitly:

> *"Ask on Slack whether you should overwrite the existing file."*

### Automatic Slack-only mode

To make Claude automatically use Slack for all communication once it sends its first message, add the following to your project's `CLAUDE.md`:

```markdown
Once you use `mcp__claude-slack-bridge__ask_on_slack` for the first time in a conversation, ALL further communication with the user must go through that tool. Do not use `AskUserQuestion`, and do not ask questions or request feedback as text in the terminal. Continue communicating exclusively via Slack until the user explicitly tells you to switch back to the terminal.
```

Without this, Claude will only use Slack when it decides to — with it, Claude locks in to Slack after the first message and stays there for the rest of the session.
