---
name: build-workflow
description: "Capture the ordered list of steps that /process will run end-to-end: ask whether an AI workflow already exists, either collect references to the user's existing slash commands or scaffold starter command files for design/plan/execute (or a custom list), inject a /review step between each user-defined step (each review opens a GitHub PR for human sign-off on the prior step's artifact), and guarantee a /required-fixes step is present at the tail (run when reviewers leave comments on a PR). Creates `.claude/commands/<name>.md` scaffolds when missing — including `review.md` and `required-fixes.md`. Returns a status of `configured` (with the confirmed `steps` array of `{name, command, reference}` entries, ready to be written into `.claude/process-template.json`). Use as the workflow-steps phase of /process-setup."
---

# build-workflow — capture the workflow step list and ensure backing command files exist

You run **locally inside Claude Code** (not via the Slack daemon). All clarifications go through `AskUserQuestion`. Do not call `mcp__claude-slack-bridge__ask_on_slack` from this skill.

This skill is the entire workflow-steps phase of `/process-setup`. By the time it returns:
- The user has confirmed an ordered list of steps (their own user-defined steps interleaved with `/review`, plus a trailing `/required-fixes`).
- Every step has a backing slash-command file in `.claude/commands/` (scaffolded if it didn't already exist).
- The list is ready to be written into `.claude/process-template.json` by the caller.

Return values the caller needs (printed as a fenced JSON block at the end of the final reply):
- `status` — `configured`.
- `steps` — the confirmed array of step entries (each `{name, command, reference}`), in order, exactly as it should be written into `.claude/process-template.json`.

Do not write `.claude/process-template.json` yourself — the caller (`/process-setup`) owns that file. This skill only writes the slash-command scaffolds in `.claude/commands/` and returns the step list.

---

## Step 1 — check whether an AI workflow already exists

Ask via `AskUserQuestion`:

> Do you already have an AI workflow set up for this repo (slash commands or skills like `/design`, `/plan`, `/execute`)?

Options:
1. **Yes, I have existing commands** — go to Step 2.
2. **No, set one up from scratch** — go to Step 3.

---

## Step 2 — existing workflow: collect references to the user's commands

Ask via `AskUserQuestion` (free-text reply via "Other"):

> Paste your workflow as a space-separated list of slash commands, in the order they should run (e.g. `/design /plan /execute`).

Parse the reply into ordered step entries. For each `/foo`:
- `name` = `foo` (no leading slash)
- `command` = `/foo`
- `reference` = the relative path to the file that defines the command, resolved by checking, in order: `.claude/commands/foo.md`, then `.claude/skills/foo/SKILL.md`. If neither exists, set `reference = null` — the command is provided by a plugin or marketplace.

If `reference` came back `null`, surface it via `AskUserQuestion`:

> I couldn't find `/<name>` locally. Is it provided by a plugin (proceed with no local reference) or did you mean a different command?

Options: `It's a plugin command — proceed` / `Let me re-paste the list`. Loop back to the prompt above if the user wants to re-paste.

Once parsed, continue to Step 4.

---

## Step 3 — new workflow: create starter command files

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

Continue to Step 4.

---

## Step 4 — inject a `/review` step between each user-defined step

Build the working step list by interleaving a `review` step after every user-defined step from Step 2 or Step 3. The `/review` step bundles the prior step's artifact (markdown design/plan doc, or the code diff on the feature branch) into a GitHub PR for human review.

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

---

## Step 5 — ensure `/required-fixes` is present at the end

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

---

## Step 6 — confirm the final step list

Confirm the parsed list back to the user via `AskUserQuestion`:

> I'll configure these steps in order: `<step1> -> review -> <step2> -> review -> ... -> required-fixes`. Confirm?

Options: `Yes, write it` / `Let me edit` (free-text — re-enter the list, then loop back to Step 2 or Step 3 depending on whether they have existing commands). Loop until confirmed.

---

## Return shape

End your final reply with a single fenced JSON block so the caller can parse the result.

```json
{
  "status": "configured",
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

The `reference` for plugin-provided commands may be `null`. Preserve the order exactly — the caller writes this array verbatim into `.claude/process-template.json`.
