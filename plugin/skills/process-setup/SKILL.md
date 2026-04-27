---
name: process-setup
description: "One-time per-repo configuration for the /process workflow. Detects a task manager, verifies GitHub credentials inside the bridge container, asks the user for the workflow steps and slash commands, writes .claude/process-template.json (version 1), generates an optional .claude/skills/list-tasks/SKILL.md helper, and appends .claude/worktrees/ and .claude/processes/ to .gitignore. Use when the user posts /process-setup in Slack or asks to set up / re-configure the /process workflow for this repository. Refuses to run while a feature is already in progress (.claude/processes/active exists)."
---

# /process-setup — one-time per-repo configuration

You are running the `/process-setup` skill for the **claude-slack-bridge full-process plugin**. This is a one-time-per-repo configuration flow. It does NOT start a feature — it only writes the template, optional helper skill, and `.gitignore` entries that `/process` will need later.

You are running inside a sub-Claude that the daemon spawned with:

- `cwd` = the **main repo root** (the consumer project, not the bridge).
- `env["SLACK_THREAD_TS"]` = the ts of the user's `/process-setup` message (already injected; the broker reads it automatically).
- `env["SLACK_CHANNEL"]` = the channel id.

All user-facing communication MUST go through `mcp__claude-slack-bridge__ask_on_slack`. Do not print prose to the terminal expecting the user to read it. From the very first `ask_on_slack` call onward, every clarification, confirmation, and final status update goes through Slack.

The plugin and the daemon's workflow engine are **version-locked**. The template you write below has `version: 1`; the daemon checks this on every step spawn and refuses to advance if the version is unsupported. Do not invent a different version.

---

## Step 0 — refuse if a feature is already active

Before doing anything else, check whether `.claude/processes/active` exists in `cwd`:

```python
import os
if os.path.exists(".claude/processes/active"):
    # Tell the user via Slack and exit. Do NOT proceed.
    ...
```

If it exists, send this exact message via `ask_on_slack` (no answer needed — but `ask_on_slack` is the only Slack channel you have, so frame it as a notice the user can reply "ok" to) or simply post and then exit:

> A feature is in progress. Run `/clean-process` first or wait for it to finish before re-configuring.

Then exit cleanly. Do not write any files.

---

## Step 1 — detect the task manager

Ask the user via `ask_on_slack`:

> Which task manager do you use for this repo? Reply with one of: `linear`, `jira`, `github`, `notion`, or `none`.

If the answer is `none`, skip to Step 2 — do not write the helper skill.

If the answer is one of `linear` / `jira` / `github` / `notion`:

1. Read the template at the plugin's `templates/task-manager.md.tmpl`. The plugin path is the directory containing `plugin.json`; the template lives at `<plugin-root>/templates/task-manager.md.tmpl`. Use `${CLAUDE_PLUGIN_ROOT}` if set, otherwise resolve the plugin root by searching upward from this skill's directory.
2. Substitute the placeholders:
   - `{{TASK_MANAGER}}` → human label (`Linear`, `Jira`, `GitHub Issues`, `Notion`).
   - `{{TASK_MANAGER_SLUG}}` → lowercase slug (`linear`, `jira`, `github`, `notion`).
   - `{{CLI_OR_API_INSTRUCTIONS}}` → the right block from the table inside the template (the template has commented-out blocks for each manager — keep the matching one and delete the others).
3. Create the directory `.claude/skills/list-tasks/` if missing and write the substituted text to `.claude/skills/list-tasks/SKILL.md`.

The generated `list-tasks` skill is invoked by the `/process` clarification skill via the Skill tool to fetch the user's open tasks. Make sure the frontmatter `name` is `list-tasks`.

---

## Step 2 — verify GitHub credentials inside the bridge container

You are running inside the bridge container (the daemon spawned you). Check, in this order:

1. **`GH_TOKEN` env var.** `os.getenv("GH_TOKEN")` — if non-empty, you are good.
2. **Mounted `~/.config/gh/`.** Check `os.path.exists(os.path.expanduser("~/.config/gh/hosts.yml"))` — if it exists, you are good.
3. **`gh auth status`.** Run `gh auth status` via subprocess; exit code 0 means you are good.

If none succeed, send this exact message via `ask_on_slack` and exit without writing the template:

> GitHub auth missing. On your host machine run `gh auth login`, then either set `GH_TOKEN=$(gh auth token)` in your `.env` for the bridge or mount `~/.config/gh` into the container (see README). Re-run `/process-setup` once that's done.

Do not try to launch a browser-based OAuth flow inside the container — the user has no browser here.

---

## Step 3 — ask for the workflow steps

Send this via `ask_on_slack`:

> What are your workflow steps and the slash commands to run for each? The default is: `/design /plan /execute /create-pr /test`. Reply `default` to accept, or send a space-separated list of slash commands in order (e.g. `/design /plan /execute /create-pr /test`).

If the user replies `default` (case-insensitive) or returns the default list, use the default.

Otherwise, parse the user's reply into an ordered list of step entries. For each slash command `/foo`:

- `name` = the command without the leading slash (`foo`).
- `command` = the slash command exactly as the user wrote it, with the leading `/`.

Confirm the parsed list back to the user with `ask_on_slack`:

> I'll configure these steps in order: `<step1> -> <step2> -> ...`. Reply `yes` to write the template, or send a corrected space-separated list.

Loop until the user replies `yes`.

---

## Step 4 — write `.claude/process-template.json`

Create `.claude/process-template.json` in `cwd` with this exact shape:

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

— but with the `steps` array replaced by the user's confirmed list. Always set `version: 1` and `branch_pattern: "feature/{slug}"`.

Use atomic write (write to `.claude/process-template.json.tmp` then `os.replace` to the final path).

---

## Step 5 — append to `.gitignore`

Read `cwd/.gitignore` if it exists. If `.claude/worktrees/` is not present as its own line, append it (with a leading newline if the file doesn't end in one). Same for `.claude/processes/`. If `.gitignore` doesn't exist, create it with these two lines.

Do not rewrite or reorder existing entries.

---

## Step 6 — confirm in Slack

Send this final message via `ask_on_slack` (you can phrase it as a "reply ok" to follow the tool contract, or just post and exit):

> Setup complete. Post `/process` in this channel to start a feature.

Then exit zero with a short stdout summary like `process-setup complete (steps=N, task_manager=X)`.

---

## Communication rules (project CLAUDE.md)

Once you call `ask_on_slack` for the first time in this skill, ALL further communication with the user must go through that tool. Do NOT use `AskUserQuestion`. Do NOT ask questions or print status to the terminal. Continue exclusively via Slack until you exit.

## Failure handling

- Any unrecoverable error (e.g. unreadable plugin template, can't write `.claude/`, malformed user reply that doesn't recover after one retry) → post a short error message via `ask_on_slack` describing what went wrong and exit non-zero. Do not leave a half-written `.claude/process-template.json` (use atomic write).
- Do not catch and ignore exceptions silently.
