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
   git worktree add ../<feature> -b <feature>
   ```

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
