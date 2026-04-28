---
name: process-setup
description: "One-time per-repo configuration for the /process workflow. Verifies that mcp__claude-slack-bridge is installed in the repo, asks the user how their task manager is integrated (MCP server / CLI / plugin / direct API), generates a .claude/skills/list-tasks/SKILL.md helper from the user's answers, captures the workflow steps (asks whether an AI workflow already exists; if not, scaffolds starter command files for design/plan/execute), injects a /review step between each user-defined step, ensures a /required-fixes step is present at the tail, writes .claude/process-template.json (version 1) and a .claude/commands/process.md orchestrator, and appends .claude/worktrees/ and .claude/processes/ to .gitignore. Use when the user runs /process-setup or asks to set up / re-configure the /process workflow for this repository. Refuses to run while a feature is already in progress (.claude/processes/active exists)."
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

## Step 3 — task manager: how is it integrated, where do tasks live, and does it actually work

Walk the user through these substeps in order. Do not skip ahead to writing the helper skill until Step 3e has verified the integration end-to-end.

All four integration methods are valid for every manager — including GitHub. Do **not** assume `gh` for github; the user may prefer the GitHub MCP server, a custom plugin, or direct REST.

### 3a. Integration method

Ask via `AskUserQuestion`:

> How is `{task_manager_label}` integrated in this environment?

Options (single-select, in this order):
1. **MCP server** — there is an MCP server providing task tools (e.g. `mcp__linear__list_issues`, `mcp__github__list_issues`).
2. **CLI tool** — there is a CLI installed (e.g. `gh`, `linear-cli`, `jira-cli`).
3. **Plugin / slash command** — there is a Claude plugin or slash command that lists tasks.
4. **Direct API (curl)** — call the manager's HTTP API directly with credentials from env vars.

Record the choice as `integration_method` ∈ `{mcp, cli, plugin, api}`.

### 3b. Check whether the chosen integration is actually installed — offer to help install if not

Before asking for the concrete invocation, do a quick availability check based on `integration_method`. The point is to catch the "user picked Linear MCP but never installed the Linear MCP server" case early, so we can offer to help.

Run the matching check:
- **mcp** — read `.mcp.json` again and look for a server entry whose key plausibly matches `{task_manager_slug}` (e.g. `linear`, `jira`, `github`, `notion`). If none match, treat as not installed.
- **cli** — ask the user which CLI binary they intend to use (one short `AskUserQuestion`, free-text — e.g. `gh`, `linear`, `jira`). Then run `command -v <cli>` via Bash (or `where <cli>` on Windows). Non-zero exit ⇒ not installed.
- **plugin** — ask the user which plugin / slash command they intend to use, then check whether it appears in the available skills/commands list for this session. Absent ⇒ not installed.
- **api** — skip the install check; API integration only needs env vars, which Step 3c surfaces.

If the check says **installed**, continue to Step 3c.

If the check says **not installed**, ask via `AskUserQuestion`:

> `{task_manager_label}` ({integration_method}) doesn't appear to be installed in this repo. Want me to help you set it up?

Options:
1. **Yes, help me install it** — proceed with the install flow below.
2. **I'll install it myself, wait for me** — pause; ask the user to reply when they're done, then re-run the availability check.
3. **Skip task manager integration** — set `integration_method = "none"`, skip Step 3c–3f entirely, and continue at Step 4. The helper skill will not be written.

If the user picks **Yes, help me install it**, run the flow that matches `integration_method`:

- **mcp** — propose the canonical MCP server for `{task_manager_slug}` (Linear → `@modelcontextprotocol/linear` style entry, GitHub → `@modelcontextprotocol/github`, etc.; if you're not certain of the exact package, ask the user to confirm the package name rather than guessing). Show the user the proposed `.mcp.json` server entry, ask which env vars they need (API key, workspace id), and only after they confirm append the entry to `.mcp.json` (preserving existing servers — never rewrite the whole file). Do **not** write secrets into `.mcp.json`; reference them via env vars and tell the user where to set them. After writing, ask the user to reload the MCP server (usually by restarting Claude Code) and confirm before continuing.
- **cli** — detect the platform (`win32` on this user's machine, but check anyway). Propose the install command (`winget install …`, `scoop install …`, `brew install …`, `npm i -g …`, etc.) and ask the user to confirm before running. Run via Bash. After install, re-run `command -v <cli>` / `where <cli>` to verify.
- **plugin** — ask the user for the plugin or marketplace name. If it's a Claude Code plugin, point them at `/plugin` to install it; do not try to install plugins from inside this skill. Wait for the user to confirm the plugin is loaded, then re-check availability.

After install (or after the user says they've installed it themselves), re-run the availability check from the top of 3b. If it still fails, ask the user whether to retry, switch integration method (jump back to 3a), or skip (jump to Step 4 with `integration_method = "none"`). Do not loop more than 3 retries without offering to skip.

### 3c. Concrete invocation

Based on the chosen method, ask one targeted follow-up via `AskUserQuestion` (use the free-text "Other" channel — these answers are repo-specific):

- **mcp** → "Which MCP tool should `list-tasks` call to fetch open tasks? (e.g. `mcp__linear__list_my_issues`)"
- **cli** → "Which command should `list-tasks` run to fetch open tasks? Paste the full command including flags (e.g. `gh issue list --assignee @me --state open --limit 20 --json number,title,body`)."
- **plugin** → "Which slash command or skill should `list-tasks` invoke? (e.g. `/my-tasks` or skill name `my-team-tasks`)"
- **api** → "Which HTTP endpoint and auth env var(s) should `list-tasks` use? (e.g. `https://api.linear.app/graphql` with `LINEAR_API_KEY`)"

Record as `integration_invocation` (free-text from the user).

### 3d. Scope (project / team / workspace)

> Which project, team, or workspace holds the tasks for this repo, and how does `list-tasks` scope its query to it? (e.g. Linear team `ENG`, Jira project `PROJ`, GitHub repo `acme/web`, Notion DB id `abc123…`. Include the filter/parameter name if relevant — e.g. `team=ENG`, `repo=acme/web`.)

Record as `scope` (single free-text field — keep it open-ended; the user types whatever identifier their tool needs).

### 3e. Run the find-the-tasks flow together — verify before writing the skill

Do not write the helper skill yet. First, actually fetch tasks once using the values gathered in 3a–3d. The goal is to (1) prove the integration works and (2) discover any missing scope/filter/auth before it's baked into the skill.

Run the call that matches `integration_method`:

- **mcp** — invoke the MCP tool named in `integration_invocation`, passing arguments derived from `scope`. If you're unsure which argument shape the tool expects, call it with the obvious mapping and let the error message guide a retry.
- **cli** — run the exact command in `integration_invocation` via Bash. If `scope` includes a filter the command doesn't yet have (e.g. `team=ENG`), ask the user how to add it, then re-run.
- **plugin** — invoke the slash command or skill via the Skill tool, passing `scope` as an argument if applicable.
- **api** — issue the HTTP request via `curl` (or Python) using the env vars in `integration_invocation`. If a required env var is missing, surface it to the user before retrying.

Show the user a short preview of what came back (e.g. the first 3 task titles, or the raw response trimmed). Then ask via `AskUserQuestion`:

> I fetched `{N}` task(s) from `{task_manager_label}`. Does this look like the right list?

Options:
1. **Yes, that's my task list** — proceed to 3f.
2. **No, the scope/filter is wrong** — ask which field is wrong and loop back to 3c or 3d as appropriate, then re-run 3e.
3. **No, the call failed** — discuss the error with the user, fix the integration (may loop back to 3b for missing install, 3c for wrong invocation, or 3d for wrong scope), then re-run 3e.
4. **The list is empty but the call succeeded — write it anyway** — accept and proceed to 3f. (Useful when the user has no open tasks right now but the integration is wired correctly.)

Do not move to 3f until the user picks option 1 or 4. Cap the loop at ~5 retries; if it still doesn't work, offer to skip task manager integration (set `integration_method = "none"` and jump to Step 4).

### 3f. Confirm and write the helper skill

Now that the flow is verified, generate `.claude/skills/list-tasks/SKILL.md` from the plugin template.

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

## Step 4 — capture workflow steps

This step captures the ordered list of steps that `/process` will run. It also makes sure that every step has a backing slash-command file in `.claude/commands/` (so the workflow engine can spawn the step), injects a `/review` step between each user-defined step (each review opens a GitHub PR for human sign-off on the prior step's artifact), and guarantees that a `/required-fixes` step exists at the tail of the list (run when reviewers leave comments on a PR).

### 4a. Check whether an AI workflow already exists

Ask via `AskUserQuestion`:

> Do you already have an AI workflow set up for this repo (slash commands or skills like `/design`, `/plan`, `/execute`)?

Options:
1. **Yes, I have existing commands** — go to 4b.
2. **No, set one up from scratch** — go to 4c.

### 4b. Existing workflow — collect references to the user's commands

Ask via `AskUserQuestion` (free-text reply via "Other"):

> Paste your workflow as a space-separated list of slash commands, in the order they should run (e.g. `/design /plan /execute`).

Parse the reply into ordered step entries. For each `/foo`:
- `name` = `foo` (no leading slash)
- `command` = `/foo`
- `reference` = the relative path to the file that defines the command, resolved by checking, in order: `.claude/commands/foo.md`, then `.claude/skills/foo/SKILL.md`. If neither exists, set `reference = null` — the command is provided by a plugin or marketplace.

If `reference` came back `null`, surface it via `AskUserQuestion`:

> I couldn't find `/<name>` locally. Is it provided by a plugin (proceed with no local reference) or did you mean a different command?

Options: `It's a plugin command — proceed` / `Let me re-paste the list`. Loop back to the prompt above if the user wants to re-paste.

### 4c. New workflow — create starter command files

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

### 4d. Inject a `/review` step between each user-defined step

Build the working step list by interleaving a `review` step after every user-defined step from 4b or 4c. The `/review` step bundles the prior step's artifact (markdown design/plan doc, or the code diff on the feature branch) into a GitHub PR for human review.

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

### 4e. Ensure `/required-fixes` is present at the end

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

### 4f. Confirm the final step list

Confirm the parsed list back to the user via `AskUserQuestion`:

> I'll configure these steps in order: `<step1> -> review -> <step2> -> review -> ... -> required-fixes`. Confirm?

Options: `Yes, write it` / `Let me edit` (free-text — re-enter the list, then loop back to 4b/4c). Loop until confirmed.

---

## Step 5 — write `.claude/process-template.json`

Create `.claude/process-template.json` in `cwd` with this exact shape, populated from the user's confirmed list in Step 4f:

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

## Step 6 — create the `/process` orchestrator command

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

## Step 7 — append to `.gitignore`

Read `cwd/.gitignore` if it exists. If `.claude/worktrees/` is not present as its own line, append it (with a leading newline if the file doesn't end in one). Same for `.claude/processes/`. If `.gitignore` doesn't exist, create it with these two lines.

Do not rewrite or reorder existing entries.

---

## Step 8 — confirm

Print a one-line summary to stdout and exit zero:

```
process-setup complete (steps=N, task_manager=X, integration=Y)
```

Where `X` is the slug (or `none`) and `Y` is the integration method (or `none`).

---

## Failure handling

- Any unrecoverable error (e.g. unreadable plugin template, can't write `.claude/`, malformed user reply that doesn't recover after one retry) → print a short error describing what went wrong and exit non-zero. Do not leave a half-written `.claude/process-template.json` (use atomic write).
- Do not catch and ignore exceptions silently.
