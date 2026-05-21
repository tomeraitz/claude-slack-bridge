---
name: build-process-skill
description: "Generate the `/process` orchestrator command for this repo. Writes `.claude/commands/process.md` — the local entry point that reads `.claude/process-template.json` and walks through the configured workflow steps in order, dispatching to each step's slash command and routing to `/required-fixes` when a `/review` step surfaces reviewer comments. Returns a status of `configured` (with the path written) or `skipped` (when the file already exists and the user opts not to overwrite). Use as the process-command phase of /process-setup."
---

# build-process-skill — write the `/process` orchestrator command

You run **locally inside Claude Code** (not via the Slack daemon). All clarifications go through `AskUserQuestion`. Do not call `mcp__claude-slack-bridge__ask_on_slack` from this skill — Slack is only the runtime channel for `/process` itself, not for setup.

This skill's only job is to create `.claude/commands/process.md` in the current repo. By the time it returns, either:
- The file has been written (status: `configured`); or
- The file already existed and the user chose to keep theirs (status: `skipped`).

Return values the caller needs (printed as a fenced JSON block at the end of the final reply):
- `status` — `configured` or `skipped`.
- `path` — `.claude/commands/process.md` when configured; omitted otherwise.

---

## Step 1 — check whether `.claude/commands/process.md` already exists

If the file does not exist, continue to Step 2.

If it does exist, ask via `AskUserQuestion`:

> `.claude/commands/process.md` already exists. Overwrite it with the standard scaffold?

Options:
1. **No, keep mine** — return immediately with `status: "skipped"`. Do not touch the file.
2. **Yes, overwrite** — continue to Step 2.
3. **No, create it under a different name** — ask via `AskUserQuestion` (free-text via "Other"):

   > What filename should I use under `.claude/commands/`? (e.g. `process2.md`, `my-process.md` — must end in `.md`)

   Validate the reply: it must end in `.md`, contain no path separators, and not collide with an existing file in `.claude/commands/`. If invalid or colliding, re-ask. Once valid, record the chosen filename as `target_filename` and continue to Step 2 — substitute the chosen filename for `process.md` everywhere below, and update the frontmatter `name:` to match (filename without `.md`).

---

## Step 2 — write `.claude/commands/process.md`

Create `.claude/commands/` if missing, then write `process.md` (atomic write: `.process.md.tmp` → `os.replace`) with the following scaffold:

```markdown
---
name: process
description: "Orchestrate the configured workflow: read .claude/process-template.json and run each step in order, with /review between steps and /required-fixes when reviewers leave comments."
---

# /process

## Accepted invocations

The user can invoke this command in either of two forms:

- **`/process`** — no argument. Treat as the default invocation.
- **`/process <argument>`** — with a single free-text argument (e.g. `/process start`, `/process resume`, `/process status`, or anything else the user types).

When invoked, read whatever argument the user passed (if any) and interpret the user's intent from it. The argument is free-text — do not require an exact match against a fixed list. Infer the intent from natural-language meaning (e.g. `start`, `begin`, `go` all mean "begin the workflow"; `resume`, `continue` mean "pick up where we left off"; `status`, `where` mean "report current state"). If the argument is ambiguous or doesn't map to a recognizable intent, ask the user to clarify what they want before doing anything.

If no argument was passed, fall back to the default behavior (defined below).

## Step 1 — start the process

This step runs when the user's argument means "start the process" — e.g. `/process start`, `/process start process`, `/process begin`, or any other natural-language phrasing that maps to that intent.

1. **Confirm with the user (via Slack).** Post via `mcp__claude-slack-bridge__ask_on_slack`:

   > You're about to start a new process. Confirm to begin?

   Wait for the user's reply. Only proceed if they confirm (`yes`, `start`, `go`, etc.). If they decline or ask to do something else, stop here and respond accordingly.

2. **Show the user their open tasks.** Invoke the `claude-slack-bridge_list-tasks` skill **in a separate Agent context** (so its fetch chatter does not pollute this command's context). Then post the returned task list to the user via `mcp__claude-slack-bridge__ask_on_slack` and wait for the user to pick one.

   Suggested Agent call:
   - `description`: "List open tasks for /process start"
   - `subagent_type`: `general-purpose`
   - `prompt`: "Invoke the `claude-slack-bridge_list-tasks` skill end-to-end and return its result verbatim (task names only, one per line)."

   Record the user's chosen task as `<feature>` — its name becomes both the branch name and the feature slug for the rest of the workflow. Normalize the name into a git-safe slug (lowercase, spaces → `-`, drop characters git refuses) before using it as a branch name.

3. **Create the worktree.** Create a new git worktree with the branch name set to `<feature>`. Run via Bash:

   ```
   git worktree add --relative-paths ../<feature> -b <feature>
   ```

   The `--relative-paths` flag is important: it makes git store relative paths in the worktree's pointer files (`<worktree>/.git` and `.git/worktrees/<feature>/gitdir`) instead of absolute ones. This keeps the worktree usable when the same files are read from different absolute paths — e.g. when `/process` runs inside the Slack daemon's Docker container (which sees the repo at `/projects/<repo>`) but the user opens the worktree from the host (e.g. `C:/.../<repo>`). With absolute paths, the host's git can't find the metadata the container wrote, and the worktree shows up as `prunable`. Requires git ≥ 2.48.

   (Adjust the worktree path if the project's setup uses `.claude/worktrees/<feature>` or similar — pick the location that matches the repo's existing convention.)

4. **Copy gitignored files into the new worktree.** Read `.gitignore` from the repo root. For each entry that exists in the current working tree (files or directories — e.g. `.mcp.json`, `.env`, `.claude/`, `projects.json`), copy it into the new worktree folder so the worktree has the same local-only files as the source checkout. Run via Bash (use `cp -r` for directories, `cp` for files). Skip entries that don't exist locally. Do not copy build/cache patterns like `__pycache__/`, `*.pyc`, `.pytest_cache/` — those are regeneratable and would just bloat the worktree.

5. **Create the feature folder and state file.** Inside the new worktree, create `.roadmap_features/<feature>/` and write `.roadmap_features/<feature>/process.json` with:

json
   {
     "step": "<design or plan>",
     "status": "started"
   }

   Set `step` to `"design"` if the skill `claude-slack-bridge_design` exists in this repo (check `.claude/skills/claude-slack-bridge_design/SKILL.md`); otherwise set it to `"plan"`.

6. **Hand off to the new worktree session via Slack.** Post via `mcp__claude-slack-bridge__ask_on_slack`:

   > `@claude-bot [<worktree-path>] /process start first step`

   Substitute `<worktree-path>` with whatever tag/path the Slack daemon uses to route to this worktree (typically the worktree's absolute path or the feature slug, matching the bridge's existing project-tag convention). The daemon picks up this message, spawns a fresh Claude session inside the worktree, and that session runs `/process start first step` — which reads `.roadmap_features/<feature>/process.json` and dispatches to the first configured step (design or plan).

   After posting, end your turn. The current session does not wait for or supervise the worktree session — state lives in `process.json`, and Slack drives the next move.

## Step 2 — run a step from `process.json`

This step runs when the user's argument means "run the step that's currently configured in `process.json`". Two variants share this branch:

- **`/process start first step`** — run the step currently recorded in `process.json` as-is. Used right after `/process start` hands off into the new worktree, when the step was just initialized by Step 1.
- **`/process next step`** — first advance `process.json` to the next step in the flow, then run it.

A third related argument is handled here for symmetry:

- **`/process not approved`** — the previous step's PR was rejected. Do **not** advance `process.json`; re-run the current step so the skill can address the feedback.

### Step flow

The configured workflow is:

1. **`design`** — *optional*. Only present if the `claude-slack-bridge_design` skill exists in this repo (check `.claude/skills/claude-slack-bridge_design/SKILL.md`). Otherwise the flow starts at `plan`.
2. **`plan`** — *mandatory*.
3. **`run-plan`** — *mandatory*.

Each step name maps to a skill:

| `process.json` step | Skill to invoke                |
| ------------------- | ------------------------------ |
| `design`            | `claude-slack-bridge_design`   |
| `plan`              | `claude-slack-bridge_plan`     |
| `run-plan`          | `claude-slack-bridge_run-plan` |

### Behavior

1. **Identify the feature and read state.** Determine `<feature>` from the current worktree (worktree folder name or current git branch). Read `.roadmap_features/<feature>/process.json` to get the current `step`.

2. **Adjust `process.json` based on the argument.**
   - If the argument was **`next step`**: pick the next step in the flow above (`design` → `plan`, `plan` → `run-plan`). If the current step is already `run-plan`, there is no next step — post via Slack that the workflow is complete and stop. Otherwise write the new step back to `process.json` (set `status: "started"`).
   - If the argument was **`start first step`**: do not change `process.json` — Step 1 already initialized it.
   - If the argument was **`not approved`**: do not change `process.json`. Optionally post a brief Slack note acknowledging the rejection before continuing to step 3.

3. **If the argument was `not approved`, gather PR feedback and the skill's re-run shape — all in separate contexts.** Skip this sub-step on `start first step` / `next step`.

   3a. **Fetch the PR and all its comments (separate Agent context).** Identify the PR URL for the current step's work — prefer the URL posted by the previous `/process` step in Slack; otherwise derive it via `gh pr list --head <feature> --json url,number`. Then spawn an Agent (`subagent_type: general-purpose`) whose only job is to collect every reviewer-visible comment on the PR and return them as a structured feedback bundle. Suggested prompt:

   > "For PR `<pr-url>`, collect every reviewer comment: top-level review summaries (`gh pr view <pr-url> --json reviews,comments`), inline review comments (`gh api repos/<owner>/<repo>/pulls/<n>/comments`), and issue-style comments (`gh api repos/<owner>/<repo>/issues/<n>/comments`). Return them grouped by file/line where applicable, with author and body, as a single bundle. Do not edit anything."

   3b. **Read the step's skill and decide the re-run shape (separate Agent context).** Spawn another Agent (`subagent_type: general-purpose`) to read `.claude/skills/<skill-name>/SKILL.md` for the current step and report back how it expects to be re-run against reviewer feedback. Specifically: does the skill itself spawn an inner subagent / inner process for addressing feedback, or is it meant to be driven directly with the feedback in the prompt? Suggested prompt:

   > "Read `.claude/skills/<skill-name>/SKILL.md` end-to-end and report: (1) how this skill is normally invoked for a re-run after reviewer feedback, (2) whether it expects to spawn its own subagent or inner process for the fix-up pass, or whether the caller should drive it directly, and (3) what inputs it needs (feedback format, paths, etc.). Do not invoke the skill — just describe it."

   Keep the outputs of 3a and 3b small and structured — they are inputs to sub-step 4, not material that should sit in the orchestrator's context beyond that handoff.

4. **Invoke the step's skill in a separate Agent context.** Use the Agent tool with `subagent_type: general-purpose` so the skill's chatter does not pollute this command's context. Pass the worktree path, feature name, and `process.json` path so the skill knows where it is operating.

   Suggested Agent call:
   - `description`: `"Run <step> step for <feature>"`
   - `subagent_type`: `general-purpose`
   - `prompt`: `"Invoke the <skill-name> skill end-to-end for feature <feature> in worktree <worktree-path>. When complete, return the GitHub PR URL it opened along with any details the user needs to approve or reject the work."`

   Map `<step>` → `<skill-name>` using the table above.

   **When the argument was `not approved`:** extend the prompt with the PR feedback bundle from 3a and follow the re-run shape surfaced by 3b — i.e. if 3b says the skill spawns its own inner subagent for fix-ups, invoke it that way; if it expects to be driven directly with the feedback, pass the bundle inline. Make clear in the prompt that this is a fix-up pass on the existing PR, not a fresh run.

5. **Relay any mid-skill questions to Slack in the same thread.** If the spawned skill needs user input, surface its question via `mcp__claude-slack-bridge__ask_on_slack` posted to **the same Slack thread** that triggered this `/process` run (so the conversation stays linear). Forward the user's answer back to the spawned skill and continue.

6. **When the skill finishes, post the PR link to Slack and hand back control.** Post via `mcp__claude-slack-bridge__ask_on_slack`:

   > Step `<step>` finished. PR: `<pr-url>`
   >
   > • If you **approve**, reply in a **new thread**: `@claude-bot [<worktree-path>] /process next step`
   > • If you do **not** approve, reply in a **new thread**: `@claude-bot [<worktree-path>] /process not approved`

   Substitute `<step>`, `<pr-url>`, and `<worktree-path>` with the real values. After posting, end your turn — the current session does not wait. The next move is driven by the user's reply in a new Slack thread, which the daemon routes back into this same worktree to run `/process next step` or `/process not approved`.

<!-- rest of body to be filled in -->
```

---

## Step 3 — confirm

Briefly confirm to the user via `AskUserQuestion`:

> I wrote `.claude/commands/process.md`. Look right?

Options:
1. **Yes** — return with `status: "configured"`.
2. **No, fix it** — ask what to change, edit the file, ask again. Cap retries at ~3.

---

## Return shape

End your final reply with a single fenced JSON block so the caller can parse the result.

For the configured case:

```json
{
  "status": "configured",
  "path": ".claude/commands/process.md"
}
```

For the skipped case:

```json
{
  "status": "skipped"
}
```
