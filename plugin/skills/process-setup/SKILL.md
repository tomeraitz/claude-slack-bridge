---
name: process-setup
description: "One-time per-repo configuration for the /process workflow. Verifies that mcp__claude-slack-bridge is installed in the repo, asks the user how their task manager is integrated (MCP server / CLI / plugin / direct API), generates a .claude/skills/list-tasks/SKILL.md helper from the user's answers, writes .claude/process-template.json (version 1), and appends .claude/worktrees/ and .claude/processes/ to .gitignore. Use when the user runs /process-setup or asks to set up / re-configure the /process workflow for this repository. Refuses to run while a feature is already in progress (.claude/processes/active exists)."
---

# /process-setup — one-time per-repo configuration

You are running the `/process-setup` skill for the **claude-slack-bridge full-process plugin**. This is a one-time-per-repo configuration flow. It does NOT start a feature — it only writes the template, optional helper skill, and `.gitignore` entries that `/process` will need later.

You run **locally inside Claude Code** (not via the Slack daemon). All clarifications go through `AskUserQuestion`. Do not call `mcp__claude-slack-bridge__ask_on_slack` from this skill — Slack is only the runtime channel for `/process` itself, not for setup.

The plugin and the daemon's workflow engine are **version-locked**. The template you write below has `version: 1`; the daemon checks this on every step spawn and refuses to advance if the version is unsupported. Do not invent a different version.

---

## Step 0 — refuse if a feature is already active

Before doing anything else, check whether `.claude/processes/active` exists in `cwd`:

```python
import os
if os.path.exists(".claude/processes/active"):
    # Print and exit. Do NOT proceed.
    ...
```

If it exists, print this exact message and exit non-zero — do not write any files:

> A feature is in progress. Run `/clean-process` first or wait for it to finish before re-configuring.

---

## Step 1 — verify `mcp__claude-slack-bridge` is installed in the repo

Read `cwd/.mcp.json`. The file must exist and must contain a server entry whose key is `claude-slack-bridge` (the MCP tool prefix `mcp__claude-slack-bridge__*` is derived from this key).

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

> `mcp__claude-slack-bridge` is not installed in this repo. Add a `claude-slack-bridge` entry under `mcpServers` in `.mcp.json` (see the project README for the exact docker-exec snippet), then re-run `/process-setup`.

Do not check whether the bridge container is *running* — only that the repo declares the server. Runtime health is `/process`'s problem, not setup's.

---

## Step 2 — task manager: pick a manager

Ask the user via `AskUserQuestion`:

> Which task manager do you use for this repo?

Options: `Linear`, `Jira`, `GitHub Issues`, `Notion`, `None / skip`.

If the answer is **None / skip**, jump straight to Step 4 — do not write the helper skill, do not ask the integration questions.

Record:
- `task_manager_label` — the human label (e.g. `Linear`, `GitHub Issues`).
- `task_manager_slug` — lowercase slug (`linear`, `jira`, `github`, `notion`).

---

## Step 3 — task manager: how is it integrated, and where do tasks live

Now ask the user three follow-up questions (in this order, one `AskUserQuestion` call per question is fine, or batch into one call with multiple questions). All four integration methods are valid for every manager — including GitHub. Do **not** assume `gh` for github; the user may prefer the GitHub MCP server, a custom plugin, or direct REST.

### 3a. Integration method

> How is `{task_manager_label}` integrated in this environment?

Options (single-select, in this order):
1. **MCP server** — there is an MCP server providing task tools (e.g. `mcp__linear__list_issues`, `mcp__github__list_issues`).
2. **CLI tool** — there is a CLI installed (e.g. `gh`, `linear-cli`, `jira-cli`).
3. **Plugin / slash command** — there is a Claude plugin or slash command that lists tasks.
4. **Direct API (curl)** — call the manager's HTTP API directly with credentials from env vars.

Record the choice as `integration_method` ∈ `{mcp, cli, plugin, api}`.

### 3b. Concrete invocation

Based on the chosen method, ask one targeted follow-up via `AskUserQuestion` (use the free-text "Other" channel — these answers are repo-specific):

- **mcp** → "Which MCP tool should `list-tasks` call to fetch open tasks? (e.g. `mcp__linear__list_my_issues`)"
- **cli** → "Which command should `list-tasks` run to fetch open tasks? Paste the full command including flags (e.g. `gh issue list --assignee @me --state open --limit 20 --json number,title,body`)."
- **plugin** → "Which slash command or skill should `list-tasks` invoke? (e.g. `/my-tasks` or skill name `my-team-tasks`)"
- **api** → "Which HTTP endpoint and auth env var(s) should `list-tasks` use? (e.g. `https://api.linear.app/graphql` with `LINEAR_API_KEY`)"

Record as `integration_invocation` (free-text from the user).

### 3c. Scope (project / team / workspace)

> Which project, team, or workspace holds the tasks for this repo, and how does `list-tasks` scope its query to it? (e.g. Linear team `ENG`, Jira project `PROJ`, GitHub repo `acme/web`, Notion DB id `abc123…`. Include the filter/parameter name if relevant — e.g. `team=ENG`, `repo=acme/web`.)

Record as `scope` (single free-text field — keep it open-ended; the user types whatever identifier their tool needs).

### 3d. Confirm and write the helper skill

Read the plugin template at `<plugin-root>/templates/task-manager.md.tmpl` (use `${CLAUDE_PLUGIN_ROOT}` if set, otherwise resolve by searching upward from this skill's directory until you find `plugin.json`).

Substitute:
- `{{TASK_MANAGER}}` → `task_manager_label`
- `{{TASK_MANAGER_SLUG}}` → `task_manager_slug`
- `{{INTEGRATION_METHOD}}` → `integration_method` (one of `mcp`, `cli`, `plugin`, `api`)
- `{{INTEGRATION_INVOCATION}}` → `integration_invocation` (verbatim user reply)
- `{{SCOPE}}` → `scope` (verbatim user reply)

Create `.claude/skills/list-tasks/` if missing and write the substituted text to `.claude/skills/list-tasks/SKILL.md`. Use atomic write (`.SKILL.md.tmp` → `os.replace`).

The generated `list-tasks` skill is invoked by the `/process` clarification skill via the Skill tool. The frontmatter `name` must be `list-tasks`.

---

## Step 4 — ask for the workflow steps

Ask via `AskUserQuestion` (free-text reply expected via "Other"):

> What are your workflow steps and the slash commands to run for each? Default: `/design /plan /execute /create-pr /test`. Reply `default` to accept, or paste a space-separated list of slash commands in order.

Parse the reply into ordered step entries. For each `/foo`:
- `name` = `foo` (no leading slash)
- `command` = `/foo` (with the leading slash, exactly as written)

Confirm the parsed list back to the user with another `AskUserQuestion`:

> I'll configure these steps in order: `<step1> -> <step2> -> ...`. Confirm?

Options: `Yes, write it` / `Let me edit` (free-text). Loop until confirmed.

---

## Step 5 — write `.claude/process-template.json`

Create `.claude/process-template.json` in `cwd` with this exact shape (steps replaced by the user's confirmed list):

```json
{
  "version": 1,
  "branch_pattern": "feature/{slug}",
  "steps": [
    { "name": "design",     "command": "/design"     },
    { "name": "plan",       "command": "/plan"       },
    { "name": "execute",    "command": "/execute"    },
    { "name": "create-pr",  "command": "/create-pr"  },
    { "name": "test",       "command": "/test"       }
  ]
}
```

Always set `version: 1` and `branch_pattern: "feature/{slug}"`. Use atomic write (`.claude/process-template.json.tmp` → `os.replace`).

---

## Step 6 — append to `.gitignore`

Read `cwd/.gitignore` if it exists. If `.claude/worktrees/` is not present as its own line, append it (with a leading newline if the file doesn't end in one). Same for `.claude/processes/`. If `.gitignore` doesn't exist, create it with these two lines.

Do not rewrite or reorder existing entries.

---

## Step 7 — confirm

Print a one-line summary to stdout and exit zero:

```
process-setup complete (steps=N, task_manager=X, integration=Y)
```

Where `X` is the slug (or `none`) and `Y` is the integration method (or `none`).

---

## Failure handling

- Any unrecoverable error (e.g. unreadable plugin template, can't write `.claude/`, malformed user reply that doesn't recover after one retry) → print a short error describing what went wrong and exit non-zero. Do not leave a half-written `.claude/process-template.json` (use atomic write).
- Do not catch and ignore exceptions silently.
