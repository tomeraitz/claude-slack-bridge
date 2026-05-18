---
name: build-plan-workflow
description: "Configure the plan phase of the /process workflow. Asks whether the user wants a plan step at all (skip-able), then asks whether they already have a planning process (e.g. an existing /plan command or skill). If they do, reads it and inspects whether it already saves the produced plan into the repo and already commits/pushes/opens a PR; whatever is missing gets added. If they don't, asks what kind of plan doc the step should produce and bakes that prompt directly into the wrapper. The ONLY file this skill writes is `.claude/skills/claude-slack-bridge_plan/SKILL.md` — a wrapper skill that runs the user's plan flow (`<@ref-plan-flow>`) or an inline plan prompt when none exists, saves the output under `.plan/` (creating the folder if missing), commits and pushes if the inner flow didn't, opens a GitHub PR, and sends a response back to the caller. Never scaffolds a separate `/plan` slash command. Returns a status of `configured` (with the captured reference + repo save path) or `skipped` (with the literal label `plan-workflow: skip`). Use as the plan-workflow phase of /process-setup."
---

# build-plan-workflow — configure the /plan phase and write the claude-slack-bridge_plan wrapper

You run **locally inside Claude Code** (not via the Slack daemon). All clarifications go through `AskUserQuestion`. Do not call `mcp__claude-slack-bridge__ask_on_slack` from this skill — Slack is only the runtime channel for `/process` itself, not for setup.

This skill is the plan-workflow phase of `/process-setup`. By the time it returns, either:
- The user opted in, the inputs were captured, and `.claude/skills/claude-slack-bridge_plan/SKILL.md` has been generated (status: `configured`); or
- The user opted out at Step 1 — no helper skill is written and the caller is told the literal label `plan-workflow: skip` (status: `skipped`).

Return values the caller needs (printed as a fenced JSON block at the end of the final reply):
- `status` — `configured` or `skipped`.
- `label` — `plan-workflow: configured` when configured, `plan-workflow: skip` when skipped.
- `has_existing_plan_process` — `true` / `false` (only present when configured).
- `existing_plan_reference` — slash command or path to the user's existing plan flow (only when `has_existing_plan_process == true`).
- `repo_plan_dir` — the folder inside the repo where the plan artifact will be saved (defaults to `.plan/`).

Do not skip ahead to writing the helper skill until Step 3 has actually captured everything it needs.

---

## Step 1 — does the user want a plan step at all?

Ask via `AskUserQuestion`:

> Do you want a plan step in your /process workflow? (the plan step produces a markdown plan doc, saves it in the repo, opens a PR for review, and only then hands off to the next step)

Options:
1. **Yes, set up the plan step** — continue to Step 2.
2. **No, help me create one** — the user has no existing planning process and wants one built inline. Record `has_existing_plan_process = false`, `existing_plan_reference = null`, `existing_plan_path = null`. **Skip Step 2 entirely** and jump straight to the `plan_kind` capture described below, then continue to Step 3.

   Ask via `AskUserQuestion` (free-text reply via "Other"):

   > What kind of plan do you need this step to produce? Examples: an implementation plan (step-by-step coding tasks, file-level changes), a project / milestone plan (phases, deliverables, owners), a migration plan (sequenced cutover steps, rollback), a testing / QA plan (test cases, coverage, gating), a rollout plan (flag ramps, canary stages), or something else — describe it briefly.

   Record the reply as `plan_kind` (free-text), then continue to Step 3.

3. **No, skip it** — return immediately with `status: "skipped"` and `label: "plan-workflow: skip"`. Do not write any files.

---

## Step 2 — does the user already have a planning process?

Capture the user's answer here so Step 3 knows which branch to take. Treat this as the working state of the skill — don't ask the same question twice in Step 3, just consume what you recorded here.

Ask via `AskUserQuestion`:

> Do you already have a planning process for this repo? (an existing `/plan` slash command, a `claude-slack-bridge_plan` style skill, or any other repeatable flow that produces a plan doc)

Options:
1. **Yes, I have one** — record `has_existing_plan_process = true`. Then ask via `AskUserQuestion` (free-text reply via "Other"):

   > Where is your planning process defined? Paste the slash command (e.g. `/plan`) or the relative path to the file (e.g. `.claude/commands/plan.md`, `.claude/skills/plan/SKILL.md`).

   Resolve the reply to a concrete file:
   - `/foo` → check `.claude/commands/foo.md`, then `.claude/skills/foo/SKILL.md`.
   - A path → use as-is.

   If neither file exists, ask once more whether the user meant a plugin command (in which case proceed with `existing_plan_reference` set to the slash command and a `null` file path) or wants to re-paste. Loop until resolved.

   Record `existing_plan_reference` (slash command form, e.g. `/plan`) and `existing_plan_path` (the resolved file path, or `null` for plugin commands). Continue to Step 3.

2. **No, I don't have one** — record `has_existing_plan_process = false`. Before helping the user build one inline, confirm they actually want this flow. Ask via `AskUserQuestion`:

   > You don't have an existing planning process. Do you want me to bake an inline plan step into the `/process` workflow (it will produce a markdown plan doc, save it in the repo, and open a PR for review)?

   Options:
   - **Yes, add it** — continue below to capture `plan_kind`.
   - **No, skip the plan step** — return immediately with `status: "skipped"` and `label: "plan-workflow: skip"`. Do not write any files.

   If the user opted to continue, capture what kind of plan the wrapper itself should produce inline. Do **not** write a `/plan` command file or any other file here — the only file this skill ever writes is `.claude/skills/claude-slack-bridge_plan/SKILL.md` in Step 3.

   Ask via `AskUserQuestion` (free-text reply via "Other"):

   > What kind of plan do you need this step to produce? Examples: an implementation plan (step-by-step coding tasks, file-level changes), a project / milestone plan (phases, deliverables, owners), a migration plan (sequenced cutover steps, rollback), a testing / QA plan (test cases, coverage, gating), a rollout plan (flag ramps, canary stages), or something else — describe it briefly.

   Record the reply as `plan_kind` (free-text). In Step 3 it will be baked directly into the wrapper skill's inline plan prompt so the wrapper knows what to produce on its own — there is no separate inner flow.

   Leave `existing_plan_reference` and `existing_plan_path` as `null`. Continue to Step 3.

---

## Step 3 — write `.claude/skills/claude-slack-bridge_plan/SKILL.md`

This is the only file this skill ever writes. The wrapper's job at runtime is:

1. **Produce the plan doc** — either invoke the user's existing plan flow (`{ref_plan_flow}`), or, when there is no existing flow, run an inline `{plan_kind}` plan prompt baked directly into this same skill.
2. **Save the plan artifact in the repo** — write the produced markdown doc under `<repo_plan_dir>/<feature>.md`. Create `<repo_plan_dir>/` if it doesn't exist (default `.plan/`).
3. **Commit and push if the inner flow didn't already** — `git add` the artifact, `git commit -m "plan: <feature>"`, `git push -u origin <branch>`. Skip this if the inner flow already committed + pushed.
4. **Open a GitHub PR** — `gh pr create --base main --head <branch> --title "<feature>: plan" --body "..."`. If a PR already exists for the branch, update it instead.
5. **Send a response back to the caller** — post via `mcp__claude-slack-bridge__ask_on_slack` with the PR URL and a short summary so the workflow engine can route to the next step.

Pick `repo_plan_dir`:

- If `has_existing_plan_process == true` and `existing_plan_path` is not null, read the file. Search for an obvious save target — a literal path the inner flow writes to (e.g. `.plan/`, `docs/plan/`, `plans/`). If found, set `repo_plan_dir` to that folder. Otherwise default to `.plan/` and the wrapper will add the save step on top of the inner flow.
- If `has_existing_plan_process == false`, default `repo_plan_dir = ".plan/"`.

Inspect the existing flow (only when `has_existing_plan_process == true`) to decide which extra steps the wrapper has to add:

- Does the inner flow already write its output to a folder in the repo? If not, the wrapper must do step 2 above.
- Does the inner flow already `git commit` + `git push`? If not, the wrapper must do step 3.
- Does the inner flow already `gh pr create` / open a PR? If not, the wrapper must do step 4.
- Does the inner flow already respond via `ask_on_slack`? If not, the wrapper must do step 5.

Whatever the inner flow already does, the wrapper should NOT duplicate — it just fills the gaps. Whatever it doesn't do, the wrapper must add. If `has_existing_plan_process == false`, treat all four boolean facts as `false` — the wrapper does steps 2–5 itself, and step 1 runs an inline plan prompt.

### Write the wrapper

Create `.claude/skills/claude-slack-bridge_plan/` if missing, then write `SKILL.md` (atomic write: `.SKILL.md.tmp` → `os.replace`) using the template below. Substitute the placeholders inline (do not leave them literal in the output):

- `{repo_plan_dir}` → the folder chosen above (e.g. `.plan/`).
- `{step_1_body}` → see the two variants below, picked based on `has_existing_plan_process`.
- `{inner_does_save}`, `{inner_does_commit_push}`, `{inner_does_pr}`, `{inner_does_respond}` → boolean facts captured during inspection (all `false` when there is no existing flow). Use them to phrase the wrapper steps as "skip if already done by the inner flow" vs. "always do".

**Variant A — `has_existing_plan_process == true`** (substitute `{ref_plan_flow}` with the slash command captured in Step 2, e.g. `/plan`):

```
Invoke `{ref_plan_flow}` to produce the plan doc. Capture the resulting markdown — either the file path it wrote to, or the inline content if it returned text. <!-- inner_does_save = {inner_does_save} -->
```

**Variant B — `has_existing_plan_process == false`** (substitute `{plan_kind}` with the free-text answer from Step 2):

```
Produce a **{plan_kind}** plan doc for the current feature inline (there is no separate `/plan` command — this wrapper is the whole flow):

1. Read `.claude/process.json` to get the feature slug and description.
2. Draft a markdown plan doc covering the aspects relevant to a {plan_kind} plan (sections, headings, and depth are up to you — refine this prompt before running `/process` for a real feature).
3. Hold the markdown content in memory; the next steps save it, commit it, open a PR, and respond.
```

Now write the wrapper file using this template:

```markdown
---
name: claude-slack-bridge_plan
description: "Produce a plan doc for the current feature, save it into the repo under {repo_plan_dir}, commit + push it, open a GitHub PR for review, and post the PR URL back to the caller via mcp__claude-slack-bridge__ask_on_slack. Either wraps an existing plan flow (filling in whatever steps it doesn't already perform) or runs an inline plan prompt baked into this skill."
---

# claude-slack-bridge_plan — plan phase wrapper

This skill is invoked by the `/process` workflow engine as the plan step. It is the single entry point the engine calls; everything the plan phase needs to do at runtime is encoded here.

## 1. Produce the plan doc

{step_1_body}

## 2. Save the plan in the repo

Target path: `{repo_plan_dir}/<feature>.md` (where `<feature>` is the slug from `.claude/process.json`). Create `{repo_plan_dir}/` if it does not exist.

If the inner flow already wrote the doc to this path, skip this step. Otherwise copy / write the captured content to the target path.

## 3. Commit and push

If the inner flow already committed and pushed the plan doc, skip this step. Otherwise:

```
git add {repo_plan_dir}/<feature>.md
git commit -m "plan: <feature>"
git push -u origin <branch>
```

Use the branch from `.claude/process.json` (`feature/<slug>` by default).

## 4. Open a GitHub PR

If the inner flow already opened a PR, skip and capture the PR URL it returned. Otherwise:

- Run `gh pr view <branch>` to check for an existing PR.
- If none: `gh pr create --base main --head <branch> --title "<feature>: plan" --body "<short summary derived from the plan doc's first heading or first paragraph>"`.
- If one exists: `gh pr edit <PR#> --body "<refreshed summary>"` and let the push from step 3 surface the new commit.

Capture the resulting PR URL.

## 5. Respond to the caller

Post the result via `mcp__claude-slack-bridge__ask_on_slack` so the workflow engine can route to the next step:

> Plan ready for review: <PR-URL>. Reply `approve` to continue to the next step, or leave review comments on the PR and reply `comments` here when done.

- `approve` → return `status = approved`; the engine advances to the next step.
- `comments` → return `status = needs_fixes` with the PR URL; the engine routes to `/required-fixes` before resuming.

Do not return until the user has replied.
```

After writing the file, briefly confirm to the user via `AskUserQuestion`:

> I wrote `.claude/skills/claude-slack-bridge_plan/SKILL.md` (saves plan under `{repo_plan_dir}`, {wraps `{ref_plan_flow}` | runs an inline `{plan_kind}` prompt}, fills in the missing commit/push/PR/respond steps). Look right?

Options:
1. **Yes** — return with `status: "configured"`.
2. **No, fix it** — ask what to change (different `repo_plan_dir`? wrong `existing_plan_reference`? inner-flow inspection got it wrong? different `plan_kind`?), update the captured values, re-write the file, ask again. Cap retries at ~3.
3. **Skip** — delete the half-written file and return with `status: "skipped"` + `label: "plan-workflow: skip"`.

---

## Return shape

End your final reply with a single fenced JSON block so the caller can parse the result.

For the configured case (user had an existing process, or the wrapper now contains an inline plan prompt):

```json
{
  "status": "configured",
  "label": "plan-workflow: configured",
  "has_existing_plan_process": true,
  "existing_plan_reference": "/plan",
  "repo_plan_dir": ".plan/"
}
```

When `has_existing_plan_process == false`, `existing_plan_reference` is `null` and `plan_kind` is included instead:

```json
{
  "status": "configured",
  "label": "plan-workflow: configured",
  "has_existing_plan_process": false,
  "existing_plan_reference": null,
  "repo_plan_dir": ".plan/",
  "plan_kind": "implementation plan"
}
```

For the skipped case (Step 1 returned No):

```json
{
  "status": "skipped",
  "label": "plan-workflow: skip"
}
```
