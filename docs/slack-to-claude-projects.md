# Slack → Claude (Project-Aware Bot)

Tag the bot directly in Slack to drive a Claude session inside a specific project. The daemon picks the project (and optionally a worktree) based on the channel and an optional `[label]` prefix.

---

## How it works

1. You tag `@claude-bot` in a Slack channel (e.g. `#my-project`).
2. The daemon looks up the channel in `projects.json` to find the matching project directory.
3. It runs `claude -p` from that project directory inside the container — so Claude sees the project's `CLAUDE.md`, codebase, and full context.
4. The response is posted back as a thread reply.
5. You can continue the conversation by replying in the thread.

---

## Setup

### 1. Set `PROJECTS_DIR` in `.env`

Point it to the parent directory that contains all your projects:

```
PROJECTS_DIR=C:\Users\you\projects
```

This directory is mounted into the container at `/projects/`.

### 2. Create `projects.json`

Map each Slack channel to its project folder name (relative to `/projects/` inside the container):

```json
{
  "#my-project-channel": "/projects/my-project",
  "#another-channel": "/projects/another-project"
}
```

> **Tip:** The folder names must match the directory names inside `PROJECTS_DIR`. For example, if `PROJECTS_DIR=C:\Users\you\projects` and you have `C:\Users\you\projects\my-project`, then the container path is `/projects/my-project`.

See `projects.json.example` for a template.

### 3. Rebuild

```bash
docker compose up -d --build
```

### Adding new projects

Just add a line to `projects.json` and restart the daemon. No changes to `docker-compose.yml` needed.

---

## `projects.json` — channel → project routing

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

### `plugin_dir` — loading Claude Code skills

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
