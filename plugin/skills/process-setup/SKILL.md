---
name: process-setup
description: "One-time per-repo configuration for the /process workflow. Verifies that mcp__claude-slack-bridge is installed in the repo, asks the user how their task manager is integrated (MCP server / CLI / plugin / direct API), generates a .claude/skills/claude-slack-bridge_list-tasks/SKILL.md helper from the user's answers, captures the workflow steps (asks whether an AI workflow already exists; if not, scaffolds starter command files for design/plan/execute), injects a /review step between each user-defined step, ensures a /required-fixes step is present at the tail, writes .claude/process-template.json (version 1) and a .claude/commands/process.md orchestrator, and appends .claude/worktrees/ and .claude/processes/ to .gitignore. Use when the user runs /process-setup or asks to set up / re-configure the /process workflow for this repository."
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

## Step 3 — capture workflow steps

This step captures the ordered list of steps that `/process` will run. It also makes sure that every step has a backing slash-command file in `.claude/commands/` (so the workflow engine can spawn the step), injects a `/review` step between each user-defined step (each review opens a GitHub PR for human sign-off on the prior step's artifact), and guarantees that a `/required-fixes` step exists at the tail of the list (run when reviewers leave comments on a PR).

### 3a. Check whether an AI workflow already exists

Ask via `AskUserQuestion`:

> Do you already have an AI workflow set up for this repo (slash commands or skills like `/design`, `/plan`, `/execute`)?

Options:
1. **Yes, I have existing commands** — go to 3b.
2. **No, set one up from scratch** — go to 3c.

### 3b. Existing workflow — collect references to the user's commands

Ask via `AskUserQuestion` (free-text reply via "Other"):

> Paste your workflow as a space-separated list of slash commands, in the order they should run (e.g. `/design /plan /execute`).

Parse the reply into ordered step entries. For each `/foo`:
- `name` = `foo` (no leading slash)
- `command` = `/foo`
- `reference` = the relative path to the file that defines the command, resolved by checking, in order: `.claude/commands/foo.md`, then `.claude/skills/foo/SKILL.md`. If neither exists, set `reference = null` — the command is provided by a plugin or marketplace.

If `reference` came back `null`, surface it via `AskUserQuestion`:

> I couldn't find `/<name>` locally. Is it provided by a plugin (proceed with no local reference) or did you mean a different command?

Options: `It's a plugin command — proceed` / `Let me re-paste the list`. Loop back to the prompt above if the user wants to re-paste.

### 3c. New workflow — create starter command files

Ask via `AskUserQuestion` (free-text reply via "Other"):

> Which steps should your workflow include? Default: `design plan execute`. Reply `default` to accept, or paste a space-separated list of step names (no slashes).

Parse the reply into ordered step names. For each `<name>`, create `.claude/commands/<name>.md` if it does not already exist. Use this minimal scaffold:

```markdown
---
name: <name>
description: "<name> phase of the /process workflow."
---

# /<name>

<TODO: describe what the <name> step should do. The step sub-Claude will read `process.json` and any prior step's artifact before running.>
```

Record per step:
- `name` = `<name>`
- `command` = `/<name>`
- `reference` = `.claude/commands/<name>.md`

Tell the user via `AskUserQuestion` that the scaffolds are starters and they should fill them in before running `/process` for a real feature:

> I created starter command files at `.claude/commands/{design,plan,execute,...}.md`. They are stubs — flesh out the prompts before running `/process`. Confirm to proceed.

Options: `Got it, proceed` / `Let me edit them now` (the latter pauses; ask the user to reply when they're done).

### 3d. Inject a `/review` step between each user-defined step

Build the working step list by interleaving a `review` step after every user-defined step from 3b or 3c. The `/review` step bundles the prior step's artifact (markdown design/plan doc, or the code diff on the feature branch) into a GitHub PR for human review.

For each inserted review step:
- `name` = `review`
- `command` = `/review`
- `reference` = `.claude/commands/review.md`

If `.claude/commands/review.md` does not already exist, create it with this scaffold:

```markdown
---
name: review
description: "Locate the prior step's artifact (wherever it landed — worktree, main repo, or ~/.claude global), materialize it into the repo on the feature branch, open a GitHub PR, and wait for human review."
---

# /review

Bundle the artifact produced by the prior step into a GitHub PR for human review. The artifact's location is **not** guaranteed — plan-mode runs frequently save to `~/.claude/` instead of the project, and some steps may write to the main repo rather than the worktree. This step's job is to find it, bring it into the repo, and PR it.

## 1. Identify the prior step

Read `.claude/process.json` in the current worktree. The prior step is `steps[current_step_index - 1]` (skip back past any earlier `review` steps to reach the user-defined step that produced the artifact). Capture:

- `prior_step_name` (e.g. `design`, `plan`, `execute`)
- `feature` (the slug)
- `worktree` (absolute path)
- `branch` (e.g. `feature/<slug>`)

## 2. Classify the artifact

- **Spec-style steps** (`design`, `plan`, anything that produces a markdown doc rather than code): the artifact is a `.md` file. Go to step 3.
- **Code-producing steps** (`execute`, anything that edits source files): the artifact is the diff already on `feature/<slug>`. Skip step 3 and go directly to step 4.

If the prior step name is ambiguous, check `git diff main...HEAD` in the worktree: if it touches code files, treat it as code-producing; if it only touches `.md` files (or nothing at all), treat it as spec-style.

## 3. Locate and materialize the spec artifact

Search these locations in order, taking the **most recently modified** match:

1. `<worktree>/.claude/processes/<feature>/<prior_step_name>.md`
2. `<worktree>/.claude/processes/<feature>/*.md` (any file in the feature's process dir)
3. `<main-repo>/.claude/processes/<feature>/<prior_step_name>.md` (some skills write to the main repo by mistake)
4. `~/.claude/projects/**/processes/<feature>/<prior_step_name>.md` (plan mode and other globally-stored skills)
5. `~/.claude/**/<prior_step_name>*.md` modified in the last hour (plan-mode fallback — narrow by mtime to avoid grabbing an old file)

If none match, ask via `ask_on_slack`:

> I couldn't find the `<prior_step_name>` artifact in the worktree, the main repo, or `~/.claude/`. Paste the file path or content so I can include it in the PR.

Once found at `<source_path>`:

1. Copy (do not move — the user may want the original) to `<worktree>/.claude/processes/<feature>/<prior_step_name>.md`. Create the parent directory if missing.
2. `git add .claude/processes/<feature>/<prior_step_name>.md` in the worktree.
3. `git commit -m "<prior_step_name>: <one-line summary>"` (derive the summary from the artifact's first heading or first sentence).
4. `git push -u origin feature/<slug>` (or the configured branch pattern).

## 4. Open or update the PR

- Run `gh pr view feature/<slug>` to see if a PR already exists.
- **No existing PR**: `gh pr create --base main --head feature/<slug> --title "<feature>: <prior_step_name>" --body "$(cat <<'EOF' ... EOF)"`. Body should summarize what the step produced and link to the artifact path inside the repo.
- **Existing PR**: `gh pr edit <PR#> --body "$(cat <<'EOF' ... EOF)"` to refresh the body, then push the new commits (already done in step 3).

For code-producing steps where step 3 was skipped, the diff is already on the branch — just push (`git push`) and create/update the PR the same way. Title format: `<feature>: <prior_step_name>` (e.g. `add-login: execute`).

## 5. Hand off to the user

Post via `ask_on_slack`:

> PR ready for review: <PR-URL>. Reply `approve` to continue to the next step, or leave review comments on the PR and reply `comments` here when done.

- **`approve`**: report success to the workflow engine (status = approved); the daemon will advance to the next step.
- **`comments`**: report status = needs_fixes with the PR URL. The workflow engine will route to `/required-fixes` before resuming the next user-defined step.

Do not proceed past this point until the user replies.
```

After this pass the steps look like: `design -> review -> plan -> review -> execute -> review`.

### 3e. Ensure `/required-fixes` is present at the end

If the working step list does not already contain a step named `required-fixes`, append one. `/required-fixes` runs when a reviewer leaves comments on a PR opened by a `/review` step — it applies the requested fixes and re-requests review.

For the `required-fixes` step:
- `name` = `required-fixes`
- `command` = `/required-fixes`
- `reference` = `.claude/commands/required-fixes.md`

If `.claude/commands/required-fixes.md` does not already exist, create it with this scaffold:

```markdown
---
name: required-fixes
description: "Apply review-comment fixes from the open PR and re-request review."
---

# /required-fixes

Read the open PR's review comments via `gh pr view --json comments` (and `gh api` for line-level comments). For each actionable comment:

1. Edit the file the comment refers to.
2. Stage and commit the fix.
3. Reply to the comment with a short note about what changed.

Push the fix commits, then re-request review on the PR. Loop until the PR is approved or the user explicitly stops you via the Slack thread.
```

### 3f. Confirm the final step list

Confirm the parsed list back to the user via `AskUserQuestion`:

> I'll configure these steps in order: `<step1> -> review -> <step2> -> review -> ... -> required-fixes`. Confirm?

Options: `Yes, write it` / `Let me edit` (free-text — re-enter the list, then loop back to 3b/3c). Loop until confirmed.

---

## Step 4 — write `.claude/process-template.json`

Create `.claude/process-template.json` in `cwd` with this exact shape, populated from the user's confirmed list in Step 3f:

```json
{
  "version": 1,
  "branch_pattern": "feature/{slug}",
  "steps": [
    { "name": "design",         "command": "/design",         "reference": ".claude/commands/design.md" },
    { "name": "review",         "command": "/review",         "reference": ".claude/commands/review.md" },
    { "name": "plan",           "command": "/plan",           "reference": ".claude/commands/plan.md" },
    { "name": "review",         "command": "/review",         "reference": ".claude/commands/review.md" },
    { "name": "execute",        "command": "/execute",        "reference": ".claude/commands/execute.md" },
    { "name": "review",         "command": "/review",         "reference": ".claude/commands/review.md" },
    { "name": "required-fixes", "command": "/required-fixes", "reference": ".claude/commands/required-fixes.md" }
  ]
}
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
