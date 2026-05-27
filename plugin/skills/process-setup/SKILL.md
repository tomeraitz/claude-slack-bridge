---
name: process-setup
description: "One-time per-repo configuration for the /process workflow. Delegates verification that mcp__claude-slack-bridge is installed (via the verify-bridge skill), task-manager setup end-to-end (via the build-task-manager skill, which generates `.claude/skills/claude-slack-bridge_list-tasks/SKILL.md`), and the three workflow phases — design, plan, and run-plan — each via its own leaf skill (`build-design-workflow`, `build-plan-workflow`, `build-run-plan-flow`) spawned directly so AskUserQuestion is never nested more than one level deep. Use when the user runs /process-setup or asks to set up / re-configure the /process workflow for this repository."
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

The workflow has three phases — design, plan, and run-plan. Each is owned by its own leaf skill (`build-design-workflow`, `build-plan-workflow`, `build-run-plan-flow`). Spawn one Agent per phase, sequentially, each in its own context window so the orchestrator stays clean. Run them as direct subagents of this skill — do **not** introduce an intermediate orchestrator subagent, because `AskUserQuestion` does not work reliably from subagents nested more than one level deep.

### Step 3a — design phase

```
Agent({
  description: "Configure design phase",
  subagent_type: "general-purpose",
  prompt: "Read plugin/skills/build-design-workflow/SKILL.md (resolve the plugin root via ${CLAUDE_PLUGIN_ROOT} if set, otherwise search upward from cwd until you find plugin.json) and follow it exactly, top to bottom. Use AskUserQuestion for all user clarifications. End your final reply with the fenced JSON return block specified by the skill so the caller can parse the result.",
  run_in_background: true
})
```

When the subagent's completion notification arrives, parse its JSON return block and record it as `design_result`.

### Step 3b — plan phase

```
Agent({
  description: "Configure plan phase",
  subagent_type: "general-purpose",
  prompt: "Read plugin/skills/build-plan-workflow/SKILL.md (resolve the plugin root via ${CLAUDE_PLUGIN_ROOT} if set, otherwise search upward from cwd until you find plugin.json) and follow it exactly, top to bottom. Use AskUserQuestion for all user clarifications. End your final reply with the fenced JSON return block specified by the skill so the caller can parse the result.",
  run_in_background: true
})
```

Record the parsed JSON as `plan_result`.

### Step 3c — run-plan phase

```
Agent({
  description: "Configure run-plan phase",
  subagent_type: "general-purpose",
  prompt: "Read plugin/skills/build-run-plan-flow/SKILL.md (resolve the plugin root via ${CLAUDE_PLUGIN_ROOT} if set, otherwise search upward from cwd until you find plugin.json) and follow it exactly, top to bottom. Use AskUserQuestion for all user clarifications. End your final reply with the fenced JSON return block specified by the skill so the caller can parse the result.",
  run_in_background: true
})
```

Record the parsed JSON as `run_plan_result`.

Do not re-implement any of the per-phase flows inline here — each leaf skill is the single source of truth for its phase, including writing its own `.claude/skills/claude-slack-bridge_<phase>/SKILL.md`.

---

## Step 4 — write the `/process` orchestrator command (delegated, end-to-end)

Delegate generation of `.claude/commands/process.md` to the `build-process-skill` skill via the Agent tool with `run_in_background: true`. The subagent owns checking whether the file already exists (offering keep / overwrite / rename), writing the scaffold atomically, and confirming with the user. Keeping all of this in a separate context window keeps the orchestrator's context clean.

```
Agent({
  description: "Write /process orchestrator command",
  subagent_type: "general-purpose",
  prompt: "Read plugin/skills/build-process-skill/SKILL.md (resolve the plugin root via ${CLAUDE_PLUGIN_ROOT} if set, otherwise search upward from cwd until you find plugin.json) and follow it exactly, top to bottom. Use AskUserQuestion for all user clarifications. You will write `.claude/commands/process.md` (or the user's chosen filename) from the scaffold defined in the skill. End your final reply with the fenced JSON return block specified by the skill so the caller can parse the status (configured or skipped) and the path written.",
  run_in_background: true
})
```

When the subagent's completion notification arrives, parse its JSON return block and record:
- `status` ∈ `{configured, skipped}`
- on `configured`: `path` (for the final summary in Step 5).

Either way, continue to Step 5. Do not re-implement any of the process-command flow inline here — `build-process-skill` is the single source of truth for that phase, including writing `.claude/commands/process.md`.

---

## Step 5 — confirm

Print a one-line summary to stdout and exit zero:

```
process-setup complete (steps=N, task_manager=X, integration=Y)
```

Where `X` is the slug (or `none`) and `Y` is the integration method (or `none`).

---

## Failure handling

- Any unrecoverable error (e.g. unreadable plugin template, malformed user reply that doesn't recover after one retry) → print a short error describing what went wrong and exit non-zero.
- Do not catch and ignore exceptions silently.
