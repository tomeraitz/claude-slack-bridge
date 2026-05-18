---
name: build-design-workflow
description: "Configure the design phase of the /process workflow. Asks whether the user wants a design step at all (skip-able), then asks whether they already have a design process (e.g. an existing /design command or skill). If they do, reads it and inspects whether it already commits/pushes/opens a PR; whatever is missing gets added. If they don't, asks what kind of design doc the step should produce and bakes that prompt directly into the wrapper. The ONLY file this skill writes is `.claude/skills/claude-slack-bridge_design/SKILL.md` — a wrapper skill that runs the user's design flow (`<@ref-design-flow>`) or an inline design prompt when none exists, saves the output to `.roadmap_features/<feature>/design/feature_design.md` (creating the folder if missing), commits and pushes if the inner flow didn't, opens a GitHub PR, and sends a response back to the caller. Never scaffolds a separate `/design` slash command. Returns a status of `configured` (with the captured reference) or `skipped` (with the literal label `design-workflow: skip`). Use as the design-workflow phase of /process-setup."
---

# build-design-workflow — configure the /design phase and write the claude-slack-bridge_design wrapper

You run **locally inside Claude Code** (not via the Slack daemon). All clarifications go through `AskUserQuestion`. Do not call `mcp__claude-slack-bridge__ask_on_slack` from this skill — Slack is only the runtime channel for `/process` itself, not for setup.

This skill is the design-workflow phase of `/process-setup`. By the time it returns, either:
- The user opted in, the inputs were captured, and `.claude/skills/claude-slack-bridge_design/SKILL.md` has been generated (status: `configured`); or
- The user opted out at Step 1 — no helper skill is written and the caller is told the literal label `design-workflow: skip` (status: `skipped`).

Return values the caller needs (printed as a fenced JSON block at the end of the final reply):
- `status` — `configured` or `skipped`.
- `label` — `design-workflow: configured` when configured, `design-workflow: skip` when skipped.
- `has_existing_design_process` — `true` / `false` (only present when configured).
- `existing_design_reference` — slash command or path to the user's existing design flow (only when `has_existing_design_process == true`).

The design artifact save path is **always** `.roadmap_features/<feature>/design/feature_design.md` (no longer configurable).

Do not skip ahead to writing the helper skill until Step 3 has actually captured everything it needs.

---

## Step 1 — does the user want a design step at all?

Ask via `AskUserQuestion`:

> Do you want a design step in your /process workflow? (the design step produces a markdown design doc, saves it in the repo, opens a PR for review, and only then hands off to the next step)

Options:
1. **Yes, set up the design step** — continue to Step 2.
2. **No, help me create one** — the user has no existing design process and wants one built inline. Record `has_existing_design_process = false`, `existing_design_reference = null`, `existing_design_path = null`. **Skip Step 2 entirely** and jump straight to the `design_kind` capture described below, then continue to Step 3.

   Ask via `AskUserQuestion` (free-text reply via "Other"):

   > What kind of design do you need this step to produce? Examples: a UX/UI design doc (user flows, wireframe descriptions), a system / architecture design (components, data flow, API contracts), a data-model design (schemas, migrations), an API design (endpoints, request/response shapes), or something else — describe it briefly.

   Record the reply as `design_kind` (free-text), then continue to Step 3.

3. **No, skip it** — return immediately with `status: "skipped"` and `label: "design-workflow: skip"`. Do not write any files.

---

## Step 2 — does the user already have a design process?

Capture the user's answer here so Step 3 knows which branch to take. Treat this as the working state of the skill — don't ask the same question twice in Step 3, just consume what you recorded here.

Ask via `AskUserQuestion`:

> Do you already have a design process for this repo? (an existing `/design` slash command, a `claude-slack-bridge_design` style skill, or any other repeatable flow that produces a design doc)

Options:
1. **Yes, I have one** — record `has_existing_design_process = true`. Then ask via `AskUserQuestion` (free-text reply via "Other"):

   > Where is your design process defined? Paste the slash command (e.g. `/design`) or the relative path to the file (e.g. `.claude/commands/design.md`, `.claude/skills/design/SKILL.md`).

   Resolve the reply to a concrete file:
   - `/foo` → check `.claude/commands/foo.md`, then `.claude/skills/foo/SKILL.md`.
   - A path → use as-is.

   If neither file exists, ask once more whether the user meant a plugin command (in which case proceed with `existing_design_reference` set to the slash command and a `null` file path) or wants to re-paste. Loop until resolved.

   Record `existing_design_reference` (slash command form, e.g. `/design`) and `existing_design_path` (the resolved file path, or `null` for plugin commands). Continue to Step 3.

2. **No, I don't have one** — record `has_existing_design_process = false`. Before helping the user build one inline, confirm they actually want this flow. Ask via `AskUserQuestion`:

   > You don't have an existing design process. Do you want me to bake an inline design step into the `/process` workflow (it will produce a markdown design doc, save it in the repo, and open a PR for review)?

   Options:
   - **Yes, add it** — continue below to capture `design_kind`.
   - **No, skip the design step** — return immediately with `status: "skipped"` and `label: "design-workflow: skip"`. Do not write any files.

   If the user opted to continue, capture what kind of design the wrapper itself should produce inline. Do **not** write a `/design` command file or any other file here — the only file this skill ever writes is `.claude/skills/claude-slack-bridge_design/SKILL.md` in Step 3.

   Ask via `AskUserQuestion` (free-text reply via "Other"):

   > What kind of design do you need this step to produce? Examples: a UX/UI design doc (user flows, wireframe descriptions), a system / architecture design (components, data flow, API contracts), a data-model design (schemas, migrations), an API design (endpoints, request/response shapes), or something else — describe it briefly.

   Record the reply as `design_kind` (free-text). In Step 3 it will be baked directly into the wrapper skill's inline design prompt so the wrapper knows what to produce on its own — there is no separate inner flow.

   Leave `existing_design_reference` and `existing_design_path` as `null`. Continue to Step 3.

---

## Step 3 — write `.claude/skills/claude-slack-bridge_design/SKILL.md`

This is the only file this skill ever writes. The wrapper's job at runtime is:

1. **Produce the design doc** — either invoke the user's existing design flow (`{ref_design_flow}`), or, when there is no existing flow, run an inline `{design_kind}` design prompt baked directly into this same skill.
2. **Save the design artifact in the repo** — write the produced markdown doc to `.roadmap_features/<feature>/design/feature_design.md`, where `<feature>` is the **current git branch name** (obtain via `git rev-parse --abbrev-ref HEAD`, or read from `.claude/process.json`). Create `.roadmap_features/<feature>/design/` if it doesn't exist. This save path is **fixed** and applies in all cases — never reuse a different path the inner flow might write to; if the inner flow wrote elsewhere, the wrapper copies/moves the content here. Note: if the branch contains slashes (e.g. `feature/foo-bar`), they become nested directories in the path.
3. **Commit and push if the inner flow didn't already** — `git add` the artifact, `git commit -m "design: <feature>"`, `git push -u origin <branch>`. Skip this if the inner flow already committed + pushed.
4. **Open a GitHub PR** — `gh pr create --base main --head <branch> --title "<feature>: design" --body "..."`. If a PR already exists for the branch, update it instead.
5. **Send a response back to the caller** — post via `mcp__claude-slack-bridge__ask_on_slack` with the PR URL and a short summary so the workflow engine can route to the next step.

Inspect the existing flow (only when `has_existing_design_process == true`) to decide which extra steps the wrapper has to add:

- Does the inner flow already write its output to `.roadmap_features/<feature>/design/feature_design.md`? If not, the wrapper must do step 2 above (writing/copying the produced markdown to the fixed path).
- Does the inner flow already `git commit` + `git push`? If not, the wrapper must do step 3.
- Does the inner flow already `gh pr create` / open a PR? If not, the wrapper must do step 4.
- Does the inner flow already respond via `ask_on_slack`? If not, the wrapper must do step 5.

Whatever the inner flow already does, the wrapper should NOT duplicate — it just fills the gaps. Whatever it doesn't do, the wrapper must add. If `has_existing_design_process == false`, treat all four boolean facts as `false` — the wrapper does steps 2–5 itself, and step 1 runs an inline design prompt.

### Write the wrapper

Create `.claude/skills/claude-slack-bridge_design/` if missing, then write `SKILL.md` (atomic write: `.SKILL.md.tmp` → `os.replace`) using the template below. Substitute the placeholders inline (do not leave them literal in the output):

- `{step_1_body}` → see the two variants below, picked based on `has_existing_design_process`.
- `{inner_does_save}`, `{inner_does_commit_push}`, `{inner_does_pr}`, `{inner_does_respond}` → boolean facts captured during inspection (all `false` when there is no existing flow). Use them to phrase the wrapper steps as "skip if already done by the inner flow" vs. "always do".

The save path `.roadmap_features/<feature>/design/feature_design.md` is hardcoded into the wrapper — do not parameterize it.

**Variant A — `has_existing_design_process == true`** (substitute `{ref_design_flow}` with the slash command captured in Step 2, e.g. `/design`):

```
Invoke `{ref_design_flow}` to produce the design doc. Capture the resulting markdown — either the file path it wrote to, or the inline content if it returned text. <!-- inner_does_save = {inner_does_save} -->
```

**Variant B — `has_existing_design_process == false`** (substitute `{design_kind}` with the free-text answer from Step 2):

```
Produce a **{design_kind}** design doc for the current feature inline (there is no separate `/design` command — this wrapper is the whole flow):

1. Read `.claude/process.json` to get the feature slug and description.
2. Draft a markdown design doc covering the aspects relevant to a {design_kind} design (sections, headings, and depth are up to you — refine this prompt before running `/process` for a real feature).
3. Hold the markdown content in memory; the next steps save it, commit it, open a PR, and respond.
```

Now write the wrapper file using this template:

```markdown
---
name: claude-slack-bridge_design
description: "Produce a design doc for the current feature, save it to `.roadmap_features/<feature>/design/feature_design.md`, commit + push it, open a GitHub PR for review, and post the PR URL back to the caller via mcp__claude-slack-bridge__ask_on_slack. Either wraps an existing design flow (filling in whatever steps it doesn't already perform) or runs an inline design prompt baked into this skill."
---

# claude-slack-bridge_design — design phase wrapper

This skill is invoked by the `/process` workflow engine as the design step. It is the single entry point the engine calls; everything the design phase needs to do at runtime is encoded here.

## 1. Produce the design doc

{step_1_body}

## 2. Save the design in the repo

Target path: `.roadmap_features/<feature>/design/feature_design.md`, where `<feature>` is the **current git branch name** (use `git rev-parse --abbrev-ref HEAD`, or read it from `.claude/process.json`). Create `.roadmap_features/<feature>/design/` if it does not exist. If the branch contains slashes (e.g. `feature/foo-bar`), they become nested directories — that is intentional.

This path is fixed. Even if the inner flow already wrote a design doc somewhere else, copy or move its content here — the rest of the pipeline (commit, PR body, downstream steps) assumes this exact path.

## 3. Commit and push

If the inner flow already committed and pushed the design doc at this exact path, skip this step. Otherwise (note: `<feature>` and `<branch>` resolve to the same value — the current git branch):

```
git add .roadmap_features/<feature>/design/feature_design.md
git commit -m "design: <feature>"
git push -u origin <branch>
```

## 4. Open a GitHub PR

If the inner flow already opened a PR, skip and capture the PR URL it returned. Otherwise:

- Run `gh pr view <branch>` to check for an existing PR.
- If none: `gh pr create --base main --head <branch> --title "<feature>: design" --body "<short summary derived from the design doc's first heading or first paragraph>"`.
- If one exists: `gh pr edit <PR#> --body "<refreshed summary>"` and let the push from step 3 surface the new commit.

Capture the resulting PR URL.

## 5. Respond to the caller

Post the result via `mcp__claude-slack-bridge__ask_on_slack` so the workflow engine can route to the next step:

> Design ready for review: <PR-URL>. Reply `approve` to continue to the next step, or leave review comments on the PR and reply `comments` here when done.

- `approve` → return `status = approved`; the engine advances to the next step.
- `comments` → return `status = needs_fixes` with the PR URL; the engine routes to `/required-fixes` before resuming.

Do not return until the user has replied.
```

After writing the file, briefly confirm to the user via `AskUserQuestion`:

> I wrote `.claude/skills/claude-slack-bridge_design/SKILL.md` (saves design to `.roadmap_features/<feature>/design/feature_design.md`, {wraps `{ref_design_flow}` | runs an inline `{design_kind}` prompt}, fills in the missing commit/push/PR/respond steps). Look right?

Options:
1. **Yes** — return with `status: "configured"`.
2. **No, fix it** — ask what to change (wrong `existing_design_reference`? inner-flow inspection got it wrong? different `design_kind`?), update the captured values, re-write the file, ask again. The save path is fixed and not adjustable. Cap retries at ~3.
3. **Skip** — delete the half-written file and return with `status: "skipped"` + `label: "design-workflow: skip"`.

---

## Return shape

End your final reply with a single fenced JSON block so the caller can parse the result.

For the configured case (user had an existing process, or the wrapper now contains an inline design prompt):

```json
{
  "status": "configured",
  "label": "design-workflow: configured",
  "has_existing_design_process": true,
  "existing_design_reference": "/design"
}
```

When `has_existing_design_process == false`, `existing_design_reference` is `null` and `design_kind` is included instead:

```json
{
  "status": "configured",
  "label": "design-workflow: configured",
  "has_existing_design_process": false,
  "existing_design_reference": null,
  "design_kind": "system / architecture design"
}
```

The design artifact is always saved to `.roadmap_features/<feature>/design/feature_design.md`; this path is not part of the return shape because it is not configurable.

For the skipped case (Step 1 returned No):

```json
{
  "status": "skipped",
  "label": "design-workflow: skip"
}
```
