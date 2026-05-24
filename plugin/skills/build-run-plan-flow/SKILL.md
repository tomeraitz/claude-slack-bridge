---
name: build-run-plan-flow
description: "Configure the run-plan (implementation) phase of the /process workflow. Asks whether the user wants a run-plan step at all (skip-able), then asks whether they already have a run-plan process (e.g. an existing /run-plan command or skill that takes a plan doc and implements it). If they do, reads it and inspects whether it already commits/pushes/opens a PR; whatever is missing gets added. If they don't, bakes a simple inline implementation prompt directly into the wrapper. The ONLY file this skill writes is `.claude/skills/claude-slack-bridge_run-plan/SKILL.md` — a wrapper skill that runs the user's run-plan flow (`<@ref-run-plan-flow>`) or an inline implementation prompt when none exists, commits and pushes if the inner flow didn't, opens a GitHub PR, and sends a response back to the caller. Never scaffolds a separate `/run-plan` slash command. Returns a status of `configured` (with the captured reference) or `skipped` (with the literal label `run-plan-flow: skip`). Use as the run-plan-flow phase of /process-setup."
---

# build-run-plan-flow — configure the /run-plan phase and write the claude-slack-bridge_run-plan wrapper

You run **locally inside Claude Code** (not via the Slack daemon). All clarifications go through `AskUserQuestion`. Do not call `mcp__claude-slack-bridge__ask_on_slack` from this skill — Slack is only the runtime channel for `/process` itself, not for setup.

This skill is the run-plan-flow phase of `/process-setup`. By the time it returns, either:
- The user opted in, the inputs were captured, and `.claude/skills/claude-slack-bridge_run-plan/SKILL.md` has been generated (status: `configured`); or
- The user opted out at Step 1 — no helper skill is written and the caller is told the literal label `run-plan-flow: skip` (status: `skipped`).

Return values the caller needs (printed as a fenced JSON block at the end of the final reply):
- `status` — `configured` or `skipped`.
- `label` — `run-plan-flow: configured` when configured, `run-plan-flow: skip` when skipped.
- `has_existing_run_plan_process` — `true` / `false` (only present when configured).
- `existing_run_plan_reference` — slash command or path to the user's existing run-plan flow (only when `has_existing_run_plan_process == true`).

Do not skip ahead to writing the helper skill until Step 3 has actually captured everything it needs.

---

## Step 1 — does the user want a run-plan step at all?

Ask via `AskUserQuestion`:

> Do you want a run-plan step in your /process workflow? (the run-plan step takes the approved plan doc, implements it in code, commits + pushes the changes, opens a PR for review, and only then hands off to the next step)

Options:
1. **Yes, set up the run-plan step** — continue to Step 2.
2. **No, skip it** — return immediately with `status: "skipped"` and `label: "run-plan-flow: skip"`. Do not write any files.

---

## Step 2 — does the user already have a run-plan process?

Capture the user's answer here so Step 3 knows which branch to take. Treat this as the working state of the skill — don't ask the same question twice in Step 3, just consume what you recorded here.

Ask via `AskUserQuestion`:

> Do you already have a run-plan / implementation process for this repo? (an existing `/run-plan` slash command, a `claude-slack-bridge_run-plan` style skill, or any other repeatable flow that takes a plan doc and implements it in code)

Options:
1. **Yes, I have one** — record `has_existing_run_plan_process = true`. Then ask via `AskUserQuestion` (free-text reply via "Other"):

   > Where is your run-plan process defined? Paste the slash command (e.g. `/run-plan`) or the relative path to the file (e.g. `.claude/commands/run-plan.md`, `.claude/skills/run-plan/SKILL.md`).

   Resolve the reply to a concrete file:
   - `/foo` → check `.claude/commands/foo.md`, then `.claude/skills/foo/SKILL.md`.
   - A path → use as-is.

   If neither file exists, ask once more whether the user meant a plugin command (in which case proceed with `existing_run_plan_reference` set to the slash command and a `null` file path) or wants to re-paste. Loop until resolved.

   Record `existing_run_plan_reference` (slash command form, e.g. `/run-plan`) and `existing_run_plan_path` (the resolved file path, or `null` for plugin commands). Continue to Step 3.

2. **No, I don't have one** — record `has_existing_run_plan_process = false`. The wrapper will bake in a simple inline implementation prompt that reads the plan doc and implements it. No further inputs needed here. Leave `existing_run_plan_reference` and `existing_run_plan_path` as `null`. Continue to Step 3.

---

## Step 3 — write `.claude/skills/claude-slack-bridge_run-plan/SKILL.md`

This is the only file this skill ever writes. The wrapper's job at runtime is:

1. **Implement the plan** — either invoke the user's existing run-plan flow (`{ref_run_plan_flow}`), or, when there is no existing flow, run a simple inline implementation prompt baked directly into this same skill.
2. **Commit and push if the inner flow didn't already** — `git add -A`, `git commit -m "run-plan: <feature>"`, `git push -u origin <branch>`. Skip this if the inner flow already committed + pushed.
3. **Open a GitHub PR** — `gh pr create --base main --head <branch> --title "<feature>: implementation" --body "..."`. If a PR already exists for the branch, update it instead.
4. **Send a response back to the caller** — post via `mcp__claude-slack-bridge__ask_on_slack` with the PR URL and a short summary so the workflow engine can route to the next step.

Inspect the existing flow (only when `has_existing_run_plan_process == true`) to decide which extra steps the wrapper has to add:

- Does the inner flow already `git commit` + `git push`? If not, the wrapper must do step 2.
- Does the inner flow already `gh pr create` / open a PR? If not, the wrapper must do step 3.
- Does the inner flow already respond via `ask_on_slack`? If not, the wrapper must do step 4.

Whatever the inner flow already does, the wrapper should NOT duplicate — it just fills the gaps. Whatever it doesn't do, the wrapper must add. If `has_existing_run_plan_process == false`, treat all three boolean facts as `false` — the wrapper does steps 2–4 itself, and step 1 runs an inline implementation prompt.

### Write the wrapper

Create `.claude/skills/claude-slack-bridge_run-plan/` if missing, then write `SKILL.md` (atomic write: `.SKILL.md.tmp` → `os.replace`) using the template below. Substitute the placeholders inline (do not leave them literal in the output):

- `{step_1_body}` → see the two variants below, picked based on `has_existing_run_plan_process`.
- `{inner_does_commit_push}`, `{inner_does_pr}`, `{inner_does_respond}` → boolean facts captured during inspection (all `false` when there is no existing flow). Use them to phrase the wrapper steps as "skip if already done by the inner flow" vs. "always do".

**Variant A — `has_existing_run_plan_process == true`** (substitute `{ref_run_plan_flow}` with the slash command captured in Step 2, e.g. `/run-plan`):

```
Invoke `{ref_run_plan_flow}` to implement the approved plan. The plan doc lives under `.plan/<feature>.md` (path from `.claude/process.json`). Pass the plan path to the inner flow and let it apply the code changes.
```

**Variant B — `has_existing_run_plan_process == false`** (simple inline implementation prompt, no extra config from the user):

```
Implement the approved plan inline (there is no separate `/run-plan` command — this wrapper is the whole flow):

1. Read `.claude/process.json` to get the feature slug, branch, and plan path (defaults to `.plan/<feature>.md`).
2. Read the plan doc end-to-end so you have the full task list and any constraints in mind.
3. Apply the code changes the plan describes, one section at a time. Run tests / type checks / linters as they would normally run in this repo after meaningful edits.
4. Stop when every item in the plan is implemented. The next steps commit, push, open a PR, and respond.
```

Now write the wrapper file using this template:

```markdown
---
name: claude-slack-bridge_run-plan
description: "Implement the approved plan for the current feature, commit + push the code changes, open a GitHub PR for review, and post the PR URL back to the caller via mcp__claude-slack-bridge__ask_on_slack. Either wraps an existing run-plan flow (filling in whatever steps it doesn't already perform) or runs an inline implementation prompt baked into this skill."
---

# claude-slack-bridge_run-plan — run-plan phase wrapper

This skill is invoked by the `/process` workflow engine as the run-plan step. It is the single entry point the engine calls; everything the run-plan phase needs to do at runtime is encoded here.

## 1. Implement the plan

{step_1_body}

## 2. Commit and push

If the inner flow already committed and pushed the changes, skip this step. Otherwise:

```
git add -A
git commit -m "run-plan: <feature>"
git push -u origin <branch>
```

Use the branch from `.claude/process.json` (`feature/<slug>` by default).

## 3. Open a GitHub PR

If the inner flow already opened a PR, skip and capture the PR URL it returned. Otherwise:

- Run `gh pr view <branch>` to check for an existing PR.
- If none: `gh pr create --base main --head <branch> --title "<feature>: implementation" --body "<short summary of what was implemented, derived from the plan doc>"`.
- If one exists: `gh pr edit <PR#> --body "<refreshed summary>"` and let the push from step 2 surface the new commits.

Capture the resulting PR URL.

## 4. Respond to the caller

Post the result via `mcp__claude-slack-bridge__ask_on_slack` so the workflow engine can route to the next step:

> Implementation ready for review: <PR-URL>. Reply `approve` to continue to the next step, or leave review comments on the PR and reply `comments` here when done.

- `approve` → return `status = approved`; the engine advances to the next step.
- `comments` → return `status = needs_fixes` with the PR URL; the engine routes to `/required-fixes` before resuming.

Do not return until the user has replied.
```

After writing the file, briefly confirm to the user via `AskUserQuestion`:

> I wrote `.claude/skills/claude-slack-bridge_run-plan/SKILL.md` ({wraps `{ref_run_plan_flow}` | runs an inline implementation prompt}, fills in the missing commit/push/PR/respond steps). Look right?

Options:
1. **Yes** — return with `status: "configured"`.
2. **No, fix it** — ask what to change (wrong `existing_run_plan_reference`? inner-flow inspection got it wrong? switch to the inline prompt?), update the captured values, re-write the file, ask again. Cap retries at ~3.
3. **Skip** — delete the half-written file and return with `status: "skipped"` + `label: "run-plan-flow: skip"`.

---

## Return shape

End your final reply with a single fenced JSON block so the caller can parse the result.

For the configured case (user had an existing process, or the wrapper now contains an inline implementation prompt):

```json
{
  "status": "configured",
  "label": "run-plan-flow: configured",
  "has_existing_run_plan_process": true,
  "existing_run_plan_reference": "/run-plan"
}
```

When `has_existing_run_plan_process == false`, `existing_run_plan_reference` is `null`:

```json
{
  "status": "configured",
  "label": "run-plan-flow: configured",
  "has_existing_run_plan_process": false,
  "existing_run_plan_reference": null
}
```

For the skipped case (Step 1 returned No):

```json
{
  "status": "skipped",
  "label": "run-plan-flow: skip"
}
```
