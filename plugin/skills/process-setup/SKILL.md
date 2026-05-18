---
name: process-setup
description: "One-time per-repo configuration for the /process workflow. Delegates verification that mcp__claude-slack-bridge is installed (via the verify-bridge skill), task-manager setup end-to-end (via the build-task-manager skill, which generates `.claude/skills/claude-slack-bridge_list-tasks/SKILL.md`), and workflow-steps capture end-to-end (via the build-workflow skill, which collects or scaffolds the user-defined slash commands, injects /review between steps, and ensures /required-fixes at the tail). Then writes `.claude/process-template.json` (version 1) and a `.claude/commands/process.md` orchestrator, and appends `.claude/worktrees/` and `.claude/processes/` to `.gitignore`. Use when the user runs /process-setup or asks to set up / re-configure the /process workflow for this repository."
---

# /process-setup — one-time per-repo configuration

You are running the `/process-setup` skill for the **claude-slack-bridge full-process plugin**. This is a one-time-per-repo configuration flow. It does NOT start a feature — it only writes the template, optional helper skill, and `.gitignore` entries that `/process` will need later.

You run **locally inside Claude Code** (not via the Slack daemon). All clarifications go through `AskUserQuestion`. Do not call `mcp__claude-slack-bridge__ask_on_slack` from this skill — Slack is only the runtime channel for `/process` itself, not for setup.

The plugin and the daemon's workflow engine are **version-locked**. The template you write below has `version: 1`; the daemon checks this on every step spawn and refuses to advance if the version is unsupported. Do not invent a different version.

---

## Step 1 — verify `mcp__claude-slack-bridge` is installed in the repo (delegated)

Delegate this check to the `verify-bridge` skill in a separate context. Spawn it via the Agent tool with `run_in_background: true` so the verification chatter does not pollute this orchestrator's context window:

```
Agent({
  description: "Verify Slack bridge MCP installed",
  subagent_type: "general-purpose",
  prompt: "Read plugin/skills/verify-bridge/SKILL.md (resolve the plugin root via ${CLAUDE_PLUGIN_ROOT} if set, otherwise search upward from cwd until you find plugin.json). Follow its instructions exactly: read cwd/.mcp.json and confirm a `claude-slack-bridge` entry exists under `mcpServers`. On success, return the literal string 'verify-bridge: ok'. On failure, return the exact fix-it message from the skill and report non-zero status. Do not write any files, do not modify .mcp.json, do not check container runtime health — only verify the declaration.",
  run_in_background: true
})
```

When the subagent's completion notification arrives, branch on the result:
- **ok** — continue to Step 2.
- **failure** — print the subagent's returned fix-it message verbatim to the user and exit non-zero. Do not write any files, do not proceed.

Do not re-implement the verification logic inline here — the `verify-bridge` skill is the single source of truth for that check, so a future change (e.g. requiring a specific bridge version) is made in one place.

---

## Step 2 — set up the task manager (delegated, end-to-end)

Delegate the entire task-manager phase to the `build-task-manager` skill via the Agent tool with `run_in_background: true`. The subagent owns picking the manager, picking the integration method, verifying install (with optional install help), capturing the concrete invocation and scope, smoke-testing the fetch, and — on success — writing `.claude/skills/claude-slack-bridge_list-tasks/SKILL.md` from the plugin template. Keeping all of this in a separate context window keeps the orchestrator's context clean.

```
Agent({
  description: "Set up task manager end-to-end",
  subagent_type: "general-purpose",
  prompt: "Read plugin/skills/build-task-manager/SKILL.md (resolve the plugin root via ${CLAUDE_PLUGIN_ROOT} if set, otherwise search upward from cwd until you find plugin.json) and follow it exactly, top to bottom. Use AskUserQuestion for all user clarifications. On the configured path, you will write .claude/skills/claude-slack-bridge_list-tasks/SKILL.md from the plugin template at <plugin-root>/templates/task-manager.md.tmpl. End your final reply with the fenced JSON return block specified by the skill so the caller can parse the status (configured or skipped) and the captured fields.",
  run_in_background: true
})
```

When the subagent's completion notification arrives, parse its JSON return block and record:
- `status` ∈ `{configured, skipped}`
- on `configured`: `task_manager_label`, `task_manager_slug`, `integration_method` (for the final summary in Step 7).
- on `skipped`: leave the captured fields as `none`.

Either way, continue to Step 3. Do not re-implement any of the task-manager flow inline here — `build-task-manager` is the single source of truth for that phase, including writing the `claude-slack-bridge_list-tasks` helper.

---

## Step 3 — capture workflow steps (delegated, end-to-end)

Delegate the entire workflow-steps phase to the `build-workflow` skill via the Agent tool with `run_in_background: true`. The subagent owns asking whether an AI workflow already exists, either collecting references to the user's existing slash commands or scaffolding starter command files for them, and confirming the final list with the user. Keeping all of this in a separate context window keeps the orchestrator's context clean.

```
Agent({
  description: "Capture workflow steps end-to-end",
  subagent_type: "general-purpose",
  prompt: "Read plugin/skills/build-workflow/SKILL.md (resolve the plugin root via ${CLAUDE_PLUGIN_ROOT} if set, otherwise search upward from cwd until you find plugin.json) and follow it exactly, top to bottom. Use AskUserQuestion for all user clarifications. You will create `.claude/commands/<name>.md` scaffolds for any steps that don't already have a backing command file. Do not write `.claude/process-template.json` — that is the caller's job. End your final reply with the fenced JSON return block specified by the skill so the caller can parse the confirmed `steps` array.",
  run_in_background: true
})
```

When the subagent's completion notification arrives, parse its JSON return block and record:
- `status` — must be `configured`.
- `steps` — the confirmed array of `{name, command, reference}` entries, in order. This is the array that gets written verbatim into `.claude/process-template.json` in Step 4.

Do not re-implement any of the workflow-steps flow inline here — `build-workflow` is the single source of truth for that phase, including writing the `.claude/commands/*.md` scaffolds.

---

## Step 4 — write `.claude/process-template.json`

Create `.claude/process-template.json` in `cwd` with this exact shape, populating `steps` from the array returned by `build-workflow` in Step 3:

```json
{
  "version": 1,
  "branch_pattern": "feature/{slug}",
  "steps": [ ...the steps array from Step 3, verbatim... ]
}
```

For reference, a typical confirmed list from Step 3 looks like:

```json
[
  { "name": "design",         "command": "/design",         "reference": ".claude/commands/design.md" },
  { "name": "review",         "command": "/review",         "reference": ".claude/commands/review.md" },
  { "name": "plan",           "command": "/plan",           "reference": ".claude/commands/plan.md" },
  { "name": "review",         "command": "/review",         "reference": ".claude/commands/review.md" },
  { "name": "execute",        "command": "/execute",        "reference": ".claude/commands/execute.md" },
  { "name": "review",         "command": "/review",         "reference": ".claude/commands/review.md" },
  { "name": "required-fixes", "command": "/required-fixes", "reference": ".claude/commands/required-fixes.md" }
]
```

Always set `version: 1` and `branch_pattern: "feature/{slug}"`. Each step entry must carry `name`, `command`, and `reference` (path to the slash-command file or `null` if the command is plugin-provided). Use atomic write (`.claude/process-template.json.tmp` → `os.replace`).

---

## Step 5 — create the `/process` orchestrator command

Create `.claude/commands/process.md` if it does not already exist. This is the local entry point that knows how to activate each step in the configured workflow — it reads `.claude/process-template.json` and walks through the steps in order, dispatching to each step's `command` and routing to `/required-fixes` when a `/review` step surfaces reviewer comments.

If `.claude/commands/process.md` already exists, leave it alone (the user — or the plugin — has already provided one). Otherwise write this scaffold:

```markdown
---
name: process
description: "Orchestrate the configured workflow: read .claude/process-template.json and run each step in order, with /review between steps and /required-fixes when reviewers leave comments."
---

# /process

Activate each step of the configured workflow.

1. Read `.claude/process-template.json`. Verify `version == 1`; otherwise abort and tell the user to re-run `/process-setup`.
2. For each step in `steps[]`, in order:
   - Look up the step's slash command and its `reference` file.
   - Invoke the slash command (e.g. `/design`, `/review`, `/execute`).
   - When a `/review` step reports that reviewers left comments, jump to the `/required-fixes` step before continuing the next user-defined step. After `/required-fixes` reports the PR is approved, resume the next step.
3. Stop after the last user-defined step's `/review` is approved. The trailing `/required-fixes` step is only run on demand (when a review surfaces comments) — it is not run unconditionally at the end.

When run through the claude-slack-bridge daemon, this orchestration is performed by the bridge's workflow engine instead — `/process` then only runs the clarification phase (`plugin/skills/process/SKILL.md`) and hands the step list off to the daemon. Use this local scaffold when running the workflow directly in Claude Code without the Slack bridge.
```

Use atomic write (`.claude/commands/process.md.tmp` → `os.replace`). Create `.claude/commands/` first if missing.

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
