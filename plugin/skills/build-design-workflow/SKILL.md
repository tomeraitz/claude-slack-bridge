---
name: build-design-workflow
description: "Configure the design phase of the /process workflow. Asks whether the user wants a design step at all (skip-able), then asks whether they already have a design process (e.g. an existing /design command or skill). If they do, reads it and inspects whether it already saves the produced design into the repo and already commits/pushes/opens a PR; whatever is missing gets added. If they don't, scaffolds the whole flow from scratch. Either way the result is `.claude/skills/claude-slack-bridge_design/SKILL.md` — a wrapper skill that runs the user's design flow (`<@ref-design-flow>`), saves the output under `.design/` (creating the folder if missing), commits and pushes if the inner flow didn't, opens a GitHub PR, and sends a response back to the caller. Returns a status of `configured` (with the captured reference + repo save path) or `skipped` (with the literal label `design-workflow: skip`). Use as the design-workflow phase of /process-setup."
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
- `repo_design_dir` — the folder inside the repo where the design artifact will be saved (defaults to `.design/`).

Do not skip ahead to writing the helper skill until Step 3 has actually captured everything it needs.

---

## Step 1 — does the user want a design step at all?

Ask via `AskUserQuestion`:

> Do you want a design step in your /process workflow? (the design step produces a markdown design doc, saves it in the repo, opens a PR for review, and only then hands off to the next step)

Options:
1. **Yes, set up the design step** — continue to Step 2.
2. **No, skip it** — return immediately with `status: "skipped"` and `label: "design-workflow: skip"`. Do not write any files.

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

2. **No, I don't have one** — record `has_existing_design_process = false`. Help the user create one before continuing:

   **2a.** Ask via `AskUserQuestion` (free-text reply via "Other"):

   > What kind of design do you need this step to produce? Examples: a UX/UI design doc (user flows, wireframe descriptions), a system / architecture design (components, data flow, API contracts), a data-model design (schemas, migrations), an API design (endpoints, request/response shapes), or something else — describe it briefly.

   Record the reply as `design_kind` (free-text). It will be baked into the inline design prompt of the new `/design` slash command so the inner flow knows what to produce.

   **2b.** Tell the user exactly what is about to happen and wait for explicit approval before writing anything. Ask via `AskUserQuestion`:

   > Here's what I'll do to set up your design process:
   > 1. Create `.claude/commands/design.md` — a starter `/design` slash command tailored to producing a `{design_kind}` design doc. You can edit it later before running `/process` for a real feature.
   > 2. Set `existing_design_reference = /design` and `existing_design_path = .claude/commands/design.md` so Step 3 wraps it like any other existing design flow.
   >
   > Confirm to proceed?

   Options: `Yes, create it` / `No, cancel` (cancelling returns to the top of Step 2 so the user can pick again).

   On approval, write `.claude/commands/design.md` if it does not already exist (atomic write: `.design.md.tmp` → `os.replace`). Use this scaffold, substituting `{design_kind}` inline:

   ```markdown
   ---
   name: design
   description: "Produce a {design_kind} design doc for the current feature. Reads the feature description, writes a markdown design doc, and returns it for the wrapping claude-slack-bridge_design skill to save + PR."
   ---

   # /design

   Produce a **{design_kind}** design doc for the current feature.

   1. Read `.claude/process.json` to get the feature slug and description.
   2. Draft a markdown design doc covering the aspects relevant to a {design_kind} design (sections, headings, and depth are up to you — flesh this prompt out before running `/process` for a real feature).
   3. Return the markdown content. The wrapping `claude-slack-bridge_design` skill will save it under `.design/<feature>.md`, commit + push, open a PR, and post the PR URL back to the caller — do not duplicate any of that here.
   ```

   After writing, set `existing_design_reference = "/design"` and `existing_design_path = ".claude/commands/design.md"`, and flip `has_existing_design_process` to `true` (the rest of the flow now treats the freshly-created `/design` exactly like a pre-existing one). Continue to Step 3.

---

## Step 3 — write `.claude/skills/claude-slack-bridge_design/SKILL.md`

Always write the wrapper skill at `.claude/skills/claude-slack-bridge_design/SKILL.md`. The wrapper's job at runtime is:

1. **Run the design flow** — invoke `<@ref-design-flow>` (i.e. the user's existing design slash command via the Skill / slash-command machinery, or the scaffolded inline prompt if there isn't one).
2. **Save the design artifact in the repo** — write the produced markdown doc under `<repo_design_dir>/<feature>.md`. Create `<repo_design_dir>/` if it doesn't exist (default `.design/`).
3. **Commit and push if the inner flow didn't already** — `git add` the artifact, `git commit -m "design: <feature>"`, `git push -u origin <branch>`. Skip this if the inner flow already committed + pushed.
4. **Open a GitHub PR** — `gh pr create --base main --head <branch> --title "<feature>: design" --body "..."`. If a PR already exists for the branch, update it instead.
5. **Send a response back to the caller** — post via `mcp__claude-slack-bridge__ask_on_slack` with the PR URL and a short summary so the workflow engine can route to the next step.

Pick `repo_design_dir`:

- If `has_existing_design_process == true` and `existing_design_path` is not null, read the file. Search for an obvious save target — a literal path the inner flow writes to (e.g. `.design/`, `docs/design/`, `design/`). If found, set `repo_design_dir` to that folder. Otherwise default to `.design/` and the wrapper will add the save step on top of the inner flow.
- If `has_existing_design_process == false`, default `repo_design_dir = ".design/"`.

Inspect the existing flow (when present) to decide which extra steps the wrapper has to add:

- Does the inner flow already write its output to a folder in the repo? If not, the wrapper must do step 2 above.
- Does the inner flow already `git commit` + `git push`? If not, the wrapper must do step 3.
- Does the inner flow already `gh pr create` / open a PR? If not, the wrapper must do step 4.
- Does the inner flow already respond via `ask_on_slack`? If not, the wrapper must do step 5.

Whatever the inner flow already does, the wrapper should NOT duplicate — it just fills the gaps. Whatever it doesn't do, the wrapper must add. If `has_existing_design_process == false`, the wrapper does all four extra steps itself.

### Write the wrapper

Create `.claude/skills/claude-slack-bridge_design/` if missing, then write `SKILL.md` (atomic write: `.SKILL.md.tmp` → `os.replace`) using the template below. Substitute the placeholders inline (do not leave them literal in the output):

- `{ref_design_flow}` → the user's slash command from Step 2 (e.g. `/design`) when `has_existing_design_process == true`; otherwise the literal placeholder `<@ref-design-flow>` and a TODO note that the user has to flesh out the inline design prompt.
- `{repo_design_dir}` → the folder chosen above (e.g. `.design/`).
- `{inner_does_save}`, `{inner_does_commit_push}`, `{inner_does_pr}`, `{inner_does_respond}` → boolean facts captured during inspection. Use them to phrase the wrapper steps as "skip if already done by the inner flow" vs. "always do".

```markdown
---
name: claude-slack-bridge_design
description: "Run the configured design flow, save the produced design doc into the repo under {repo_design_dir}, commit + push it, open a GitHub PR for review, and post the PR URL back to the caller via mcp__claude-slack-bridge__ask_on_slack. Wraps the user's existing design process (if any) and adds whatever steps it doesn't already perform."
---

# claude-slack-bridge_design — design phase wrapper

This skill is invoked by the `/process` workflow engine as the design step. It is the single entry point the engine calls; everything the design phase needs to do at runtime is encoded here.

## 1. Run the design flow

Invoke `{ref_design_flow}` to produce the design doc. Capture the resulting markdown — either the file path it wrote to, or the inline content if it returned text. <!-- inner_does_save = {inner_does_save} -->

## 2. Save the design in the repo

Target path: `{repo_design_dir}/<feature>.md` (where `<feature>` is the slug from `.claude/process.json`). Create `{repo_design_dir}/` if it does not exist.

If the inner flow already wrote the doc to this path, skip this step. Otherwise copy / write the captured content to the target path.

## 3. Commit and push

If the inner flow already committed and pushed the design doc, skip this step. Otherwise:

```
git add {repo_design_dir}/<feature>.md
git commit -m "design: <feature>"
git push -u origin <branch>
```

Use the branch from `.claude/process.json` (`feature/<slug>` by default).

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

> I wrote `.claude/skills/claude-slack-bridge_design/SKILL.md` (saves design under `{repo_design_dir}`, wraps `{ref_design_flow}`, fills in the missing commit/push/PR/respond steps). Look right?

Options:
1. **Yes** — return with `status: "configured"`.
2. **No, fix it** — ask what to change (different `repo_design_dir`? wrong `existing_design_reference`? inner-flow inspection got it wrong?), update the captured values, re-write the file, ask again. Cap retries at ~3.
3. **Skip** — delete the half-written file and return with `status: "skipped"` + `label: "design-workflow: skip"`.

---

## Return shape

End your final reply with a single fenced JSON block so the caller can parse the result.

For the configured case (user had an existing process, or one was just scaffolded for them in Step 2):

```json
{
  "status": "configured",
  "label": "design-workflow: configured",
  "has_existing_design_process": true,
  "existing_design_reference": "/design",
  "repo_design_dir": ".design/",
  "design_kind": "system / architecture design"
}
```

`design_kind` is only present when Step 2 scaffolded a fresh `/design` command (i.e. the user originally said "No, I don't have one"). Omit it when wrapping a pre-existing flow.

For the skipped case (Step 1 returned No):

```json
{
  "status": "skipped",
  "label": "design-workflow: skip"
}
```
