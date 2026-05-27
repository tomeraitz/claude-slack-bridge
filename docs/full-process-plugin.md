# The full-process plugin

A turnkey feature-development workflow driven entirely from Slack. You pick a task, the bot creates a git worktree, walks the work through your configured steps (typically **design → plan → run-plan**), opens a GitHub PR after each step, and waits for your approval in Slack before moving on.

The plugin lives at [plugin/](../plugin/) in this repo and ships two top-level slash commands:

| Command | Where it runs | What it does |
|---|---|---|
| `/process-setup` | **Locally** in Claude Code (one time per repo) | Scaffolds the per-repo configuration: verifies the bridge, sets up the task-manager integration, captures your workflow steps, and writes the `/process` orchestrator command. |
| `/process` | **From Slack** (runtime) | The orchestrator. `/process start` kicks off a new feature; subsequent invocations advance through the configured steps. |

---

## Why two commands?

Setup and runtime have different needs:

- **`/process-setup`** is interactive configuration — it asks about your task manager, your existing slash commands, your preferred workflow shape. That works best as a local terminal flow with `AskUserQuestion`.
- **`/process`** is the day-to-day runtime — it runs inside the Slack daemon's container, in a worktree, and uses Slack as its only UI. Once setup is done, you never touch the terminal for a feature again.

The plugin and the daemon's workflow engine are **version-locked**. `process-setup` writes `version: 1` into the template; the daemon refuses to advance steps if the version is unsupported.

---

## Step 1 — install the plugin

In Claude Code, add this repo as a plugin marketplace and install the `full-process` plugin:

```
/plugin marketplace add tomeraitz/claude-slack-bridge
/plugin install full-process@claude-slack-bridge
```

Once installed, the `/process-setup` is available as slash commands.

> See [Claude Code's plugin docs](https://docs.claude.com/en/docs/claude-code/plugins) for more on managing plugins and marketplaces.

---

## Step 2 — run `/process-setup` (once per repo)

Open the repo in Claude Code locally and run:

```
/process-setup
```

This runs **locally inside Claude Code**, not via Slack. All clarifications go through `AskUserQuestion`. The skill delegates the heavy lifting to several subskills, each in its own context window so the orchestrator stays clean. Each subskill is spawned as a direct child of `process-setup` (no intermediate orchestrator) because `AskUserQuestion` does not work reliably from subagents nested more than one level deep:

| Subskill | What it does |
|---|---|
| `verify-bridge` | Reads `.mcp.json` and confirms a `claude-slack-bridge` entry exists under `mcpServers`. Fails fast with a fix-it message if not. |
| `build-task-manager` | Asks which task manager you use (Notion, Linear, Jira, etc.), how to integrate (MCP server, CLI, REST), captures the concrete invocation and scope, smoke-tests the fetch, and writes `.claude/skills/claude-slack-bridge_list-tasks/SKILL.md`. |
| `build-design-workflow` | Configures the design phase. Either wraps an existing `/design` flow or bakes an inline design prompt into `.claude/skills/claude-slack-bridge_design/SKILL.md`. |
| `build-plan-workflow` | Configures the plan phase. Either wraps an existing `/plan` flow or bakes an inline plan prompt into `.claude/skills/claude-slack-bridge_plan/SKILL.md`. |
| `build-run-plan-flow` | Configures the run-plan (implementation) phase. Either wraps an existing `/run-plan` flow or bakes an inline implementation prompt into `.claude/skills/claude-slack-bridge_run-plan/SKILL.md`. |
| `build-process-skill` | Writes `.claude/commands/process.md` — the runtime orchestrator that `/process` from Slack will invoke. Offers keep / overwrite / rename if the file already exists. |

When `/process-setup` finishes, your repo has everything `/process` needs:

```
.claude/
├── commands/
│   ├── process.md             # the runtime orchestrator
│   ├── design.md              # (or whatever you named your first step)
│   ├── plan.md
│   └── run-plan.md
├── skills/
│   └── claude-slack-bridge_list-tasks/
│       └── SKILL.md           # how to fetch your open tasks
└── process-template.json      # version-locked config (version: 1)
```

You should also have a `claude-slack-bridge` entry in `.mcp.json` (set up via [mcp-client-setup.md](mcp-client-setup.md)) and your project's channel registered in [projects.json](slack-to-claude-projects.md).

---

## Step 3 — activate `/process` from Slack

Once setup is done, switch to Slack and tag the bot in the project's channel:

```
@claude-bot /process start
```

The bot will then walk you through this flow:

### 3a. Confirm and pick a task

The bot posts back in a Slack thread:

> You're about to start a new process. Confirm to begin?

Reply `yes` (or `start`, `go`, etc.) in the thread. The bot then invokes the `claude-slack-bridge_list-tasks` skill in a separate Agent context and posts back your open tasks:

> Your open tasks:
> - Fix the login redirect bug
> - Add audit logging to the admin API
> - Migrate billing to the new pricing model
>
> Which one?

Reply with the task name. The bot normalizes it into a git-safe slug — that becomes your **feature name and branch name** for the rest of the workflow.

### 3b. Worktree creation

The bot creates a sibling worktree and copies in your gitignored files so the worktree has the same local-only setup (`.env`, `.mcp.json`, `.claude/`, etc.):

```
git worktree add ../<feature> -b <feature>
# + relative-path rewrite so host and container both see it cleanly
# + cp -r for each gitignored file/dir that exists locally
```

It then writes the workflow state file:

```
.roadmap_features/<feature>/process.json
{
  "step": "design",         // or "plan" if no design skill is installed
  "status": "started"
}
```

### 3c. Hand-off to the worktree

The bot posts a hand-off message in the channel:

```
@claude-bot [<feature>] /process start first step
```

That message gets picked up by the daemon, which spawns a fresh Claude session **inside the new worktree** and runs `/process start first step` — which reads `process.json` and dispatches to the first configured step.

The original session ends its turn here. **State lives in `process.json`** — sessions don't supervise each other.

---

## Step 4 — walk through the steps

Each step (design, plan, run-plan, …) runs in its own Slack thread inside the worktree. The skill for each step:

1. Does its work (writes a design doc, a plan doc, or implements code).
2. Commits and pushes.
3. Opens a GitHub PR.
4. Posts the PR URL back via `ask_on_slack`:

```
Step `design` finished. PR: https://github.com/you/repo/pull/42

• If you approve, reply in a NEW thread:
    @claude-bot [<feature>] /process next step
• If you do not approve, reply in a NEW thread:
    @claude-bot [<feature>] /process not approved
```

### Approving — `/process next step`

The bot advances `process.json` to the next step in the flow (`design` → `plan` → `run-plan`), then runs that step's skill. When `run-plan` finishes, there is no next step and the workflow completes.

### Rejecting — `/process not approved`

The bot does **not** advance `process.json`. Instead it:

1. Spawns a separate Agent to collect every reviewer comment on the PR (top-level reviews, inline comments, issue comments).
2. Spawns another Agent to read the step's skill and report how it expects to be re-run against feedback.
3. Invokes the step's skill again with the feedback bundle — pushing an update to the same PR.
4. Posts the new PR URL with the same approve / not-approved choice.

You can loop on the same step as many times as you need.

---

## State and threading model

Two things make the runtime composable:

- **`process.json` is the single source of truth.** Every step re-reads it on entry. Sessions never call each other directly — they hand off through Slack.
- **Each step uses a new Slack thread.** The hand-off message (`@claude-bot [<feature>] /process …`) is a top-level channel message, which lets the daemon route it into the right worktree with a fresh session. Mid-step questions stay in the same thread the step started in.

This means you can pause for hours or days between steps. The next thread picks up exactly where the last one left off — there's no in-memory state to lose.

---

## Step flow diagram

```
        ┌──────────────────────────────┐
        │  @claude-bot /process start  │
        └──────────┬───────────────────┘
                   │
              pick task ──► create worktree ──► write process.json
                   │
                   ▼
        ┌─────────────────────┐
        │  design  (optional) │ ─── PR ──► approve? ──┐
        └─────────────────────┘                       │
                   │ next step                 ▲      │ not approved
                   ▼                           │      │ (loops back)
        ┌─────────────────────┐                └──────┘
        │  plan               │ ─── PR ──► approve? ──┐
        └─────────────────────┘                       │
                   │ next step                 ▲      │
                   ▼                           │      │
        ┌─────────────────────┐                └──────┘
        │  run-plan           │ ─── PR ──► approve? ──┐
        └─────────────────────┘                       │
                   │                           ▲      │
                   ▼                           └──────┘
                  done
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `verify-bridge: failure` during `/process-setup` | `.mcp.json` doesn't have a `claude-slack-bridge` entry | Follow [mcp-client-setup.md](mcp-client-setup.md) first |
| Host's `git worktree list` shows the worktree as `prunable` | Container wrote absolute paths into the gitdir pointers | `/process start` rewrites these to relative paths automatically — if you hit this on an older bot version, update the bridge image |
| Task list comes back empty | Task-manager integration captured the wrong scope or query | Re-run `/process-setup` and redo step 2 (build-task-manager) |
| `/process next step` does nothing | `process.json` is at the final step (`run-plan`) | Workflow is done — start a new feature with `/process start` |
| PR never gets created | `GITHUB_TOKEN` not set in `.env` | See [github-setup.md](github-setup.md) |
