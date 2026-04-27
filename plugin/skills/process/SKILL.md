---
name: process
description: "Clarification phase of the /process workflow. Acquires the task (via the optional list-tasks skill or by asking), runs a clarification loop in Slack, derives a feature slug, creates a git worktree, materializes <worktree>/.claude/process.json from .claude/process-template.json, writes the .claude/processes/active marker, then notifies the daemon over the Unix socket so the workflow engine can begin running steps. Use when the user posts /process in Slack and the daemon spawns this skill in a sub-Claude. Refuses if a process is already active."
---

# /process — clarification + handoff to the daemon

You are running the `/process` skill for the **claude-slack-bridge full-process plugin**. The daemon spawned you in a sub-Claude with:

- `cwd` = the **main repo root** of the consumer project (NOT a worktree — you create the worktree).
- `env["SLACK_THREAD_TS"]` = the ts of the user's `/process` message (this becomes the thread root for the entire feature).
- `env["SLACK_CHANNEL"]` = the channel id.

The broker reads `SLACK_THREAD_TS` automatically, so every `ask_on_slack` call you make lands as a reply in the existing thread.

Your job is **only the clarification phase**. You do not run any workflow steps. After you write `process.json` and tell the daemon to `START`, you exit. The daemon takes over and spawns each step sub-Claude.

---

## Critical contract — the state you write must match what the daemon expects

The daemon's workflow engine reads `process.json` and expects this exact shape after clarification:

- `phase` = `"ready_for_next_step"` (NOT `"running_step"` — the daemon flips it to `running_step` only after it successfully spawns step 0; if it crashes between your `START` and a successful spawn, the file must remain in a recoverable state).
- All `steps[].status` = `"not started"` — **including step 0**. Do not pre-mark step 0 `in progress`; the daemon does that.
- `current_step_index` = `0`.
- `current_step_pid` = `null`.
- `pending_user_input` = `[]`.
- `pr_link` = `null`.
- `slack_channel` = `os.environ["SLACK_CHANNEL"]`.
- `slack_thread_ts` = `os.environ["SLACK_THREAD_TS"]`.
- `feature` = your derived slug.
- `branch` = `branch_pattern.replace("{slug}", feature)` (the template's `branch_pattern` defaults to `feature/{slug}`).
- `worktree` = `.claude/worktrees/<feature>` (relative path, what the template stores; absolute path is what you send to the daemon).
- `task_source` = a string like `"linear:ENG-482"`, `"github:#42"`, `"manual"`, or `"none"` depending on how you acquired the task.
- `task_description` = the user's clarified description (one or two short paragraphs — what the steps will read).
- `steps` = copied verbatim from `.claude/process-template.json["steps"]`, with each step augmented to `{ "name": ..., "command": ..., "status": "not started", "rejection_reason": null }`.

The template's `version: 1` is checked by the daemon on every spawn. If `process-template.json` has a different version, abort and tell the user to re-run `/process-setup` or upgrade the bridge.

---

## Step 1 — check for an existing active feature

Before anything else:

```python
import os
if os.path.exists(".claude/processes/active"):
    # Post via ask_on_slack and exit. Do not proceed.
    ...
```

If `.claude/processes/active` exists, send via `ask_on_slack`:

> A process is active — finish it or post `/clean-process`.

Then exit cleanly. (The daemon also enforces this, but defending in depth means a user editing files by hand can't trick you into double-starting.)

Also check that `.claude/process-template.json` exists. If it doesn't, send via `ask_on_slack`:

> No `.claude/process-template.json` found. Run `/process-setup` first.

And exit.

---

## Step 2 — acquire the task

Check whether `.claude/skills/list-tasks/SKILL.md` exists in `cwd`. If yes:

1. Invoke that skill via the Skill tool. It returns a list of open tasks from the configured task manager.
2. Format the list (numbered) and send via `ask_on_slack`:
   > Pick a task by number, or reply `manual` to describe one yourself: `1. <title>`, `2. <title>`, ...

3. If the user replies with a number, set `task_source` to `<manager>:<task-id>` (e.g. `linear:ENG-482`) and `task_description` to that task's title + body.
4. If the user replies `manual`, fall through to the no-task-manager path.

If `list-tasks` does not exist OR the user chose `manual`, send via `ask_on_slack`:

> What is the task? Describe it in one paragraph.

Treat the user's reply as `task_description` and set `task_source` to `"manual"`.

---

## Step 3 — clarification loop

Now ask follow-up questions via `ask_on_slack` until you have enough context for the workflow steps to proceed without further questions. Examples of what to ask: target file/module, expected behavior, edge cases, what "done" looks like, whether tests are required.

Do not over-ask — 1 to 3 follow-ups is typical. Stop as soon as the user signals readiness (e.g. replies `ready`, `go`, `that's enough`, or you have a self-contained `task_description`).

Update `task_description` to incorporate the answers (rewrite it as a coherent paragraph or bulleted brief — this is what every step sub-Claude will read first).

---

## Step 4 — derive a feature slug and confirm

Derive a kebab-case slug from the task title:

- Lowercase.
- Replace non-alphanumerics with `-`.
- Collapse multiple `-` into one.
- Strip leading/trailing `-`.
- Truncate to ~40 chars at a word boundary.

Send via `ask_on_slack`:

> I'll use the feature slug `<slug>` (branch: `feature/<slug>`, worktree: `.claude/worktrees/<slug>`). Reply `yes` to confirm or send a different slug.

If the user sends a different slug, sanitize it the same way and confirm again. Do not loop forever — after 2 corrections, accept whatever the user sends (sanitized).

---

## Step 5 — create the worktree

Run:

```bash
git worktree add .claude/worktrees/<slug> -b feature/<slug>
```

Use a subprocess call from `cwd`. If the branch already exists, fall back to `git worktree add .claude/worktrees/<slug> feature/<slug>` (without `-b`). If the worktree path already exists, abort with a Slack message asking the user to pick a different slug.

After success, capture the **absolute** path of the new worktree (you'll need it both for `.claude/processes/active` and for the `START` socket message). Use `os.path.abspath`.

Create `<worktree>/.claude/` (the worktree inherits the main repo's tree, so `.claude/` may already exist there — `os.makedirs(..., exist_ok=True)` is fine).

---

## Step 6 — materialize `process.json`

Read `.claude/process-template.json` from `cwd` (the main repo). Verify `version == 1` (or fail with the upgrade message above).

Build the `process.json` payload:

```python
import json, os

with open(".claude/process-template.json") as f:
    tmpl = json.load(f)

if tmpl.get("version") != 1:
    # Post upgrade message via ask_on_slack and exit non-zero.
    ...

slug = "<your derived slug>"
branch = tmpl["branch_pattern"].replace("{slug}", slug)

steps = [
    {
        "name": s["name"],
        "command": s["command"],
        "status": "not started",
        "rejection_reason": None,
    }
    for s in tmpl["steps"]
]

state = {
    "feature": slug,
    "branch": branch,
    "worktree": f".claude/worktrees/{slug}",
    "task_source": task_source,
    "task_description": task_description,
    "slack_channel": os.environ["SLACK_CHANNEL"],
    "slack_thread_ts": os.environ["SLACK_THREAD_TS"],
    "phase": "ready_for_next_step",
    "current_step_index": 0,
    "current_step_pid": None,
    "pending_user_input": [],
    "pr_link": None,
    "steps": steps,
}

worktree_abs = os.path.abspath(f".claude/worktrees/{slug}")
target = os.path.join(worktree_abs, ".claude", "process.json")
os.makedirs(os.path.dirname(target), exist_ok=True)

tmp = target + ".tmp"
with open(tmp, "w") as f:
    json.dump(state, f, indent=2)
os.replace(tmp, target)
```

Atomic write via tmp + `os.replace` is mandatory — the daemon may read this file as soon as it sees `START`.

---

## Step 7 — write `.claude/processes/active` (atomic)

In the **main repo** (`cwd`, NOT the worktree), write `.claude/processes/active` containing the absolute worktree path. Atomic create so a concurrent `/process` post can't double-write:

```python
import os, tempfile

main_active_dir = ".claude/processes"
os.makedirs(main_active_dir, exist_ok=True)
active_path = os.path.join(main_active_dir, "active")

# Atomic: write to a uniquely-named tmp in the same dir, then os.replace.
fd, tmp = tempfile.mkstemp(dir=main_active_dir, prefix=".active.", suffix=".tmp")
try:
    with os.fdopen(fd, "w") as f:
        f.write(worktree_abs + "\n")
    os.replace(tmp, active_path)
except Exception:
    if os.path.exists(tmp):
        os.unlink(tmp)
    raise
```

If `os.replace` is blocked because the file already exists (it shouldn't, given Step 1 — but in a race, the daemon's own `O_CREAT | O_EXCL` admission check would have caught it first), abort with a Slack message and exit.

---

## Step 8 — notify the daemon over the Unix socket

The daemon listens on `/tmp/slack-bridge.sock` for line-oriented control messages (verified in `src/session_broker.py` — same socket the broker uses for `REGISTER`). Send a single line `START <absolute-worktree-path>\n` and close the connection. Python:

```python
import socket

SOCK = "/tmp/slack-bridge.sock"
msg = f"START {worktree_abs}\n".encode()

with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
    s.connect(SOCK)
    s.sendall(msg)
    # No reply expected; close immediately.
```

Or in bash:

```bash
printf 'START %s\n' "$WORKTREE_ABS" | nc -U /tmp/slack-bridge.sock
```

If the connection fails (socket missing, daemon down), post via `ask_on_slack`:

> Couldn't reach the bridge daemon at `/tmp/slack-bridge.sock`. The state files are written, but the workflow won't start until the daemon is back. Restart the bridge and post `/clean-process` then `/process` again.

And exit non-zero. **Do NOT delete `.claude/processes/active` yourself in this case** — the user needs `/clean-process` to do it cleanly.

---

## Step 9 — exit with a short summary

Send a final Slack message via `ask_on_slack` (or just post a notice — either works):

> Clarification done. Starting the workflow for `<slug>` (branch `feature/<slug>`). I'll post here when the first step needs your review.

Print a one-line stdout summary and exit zero. Example: `clarification complete; feature=<slug>; worktree=<abs-path>; START sent`.

---

## Communication rules (project CLAUDE.md)

Once you call `ask_on_slack` for the first time, ALL further communication with the user goes through Slack. No terminal prompts, no `AskUserQuestion`. Stay in Slack for the rest of the skill.

## Things you must NOT do

- Do not run `/design`, `/plan`, or any workflow step. The daemon spawns those in separate sub-Claudes.
- Do not write to `process.json` after Step 6. The daemon owns it once `START` is sent.
- Do not start a background loop watching for the next step. Exit cleanly so the daemon can spawn step 0 fresh.
- Do not pre-mark step 0 as `in progress`. The daemon flips it after a successful spawn (see §6 of the design doc — "Phase handoff from clarification").
- Do not send anything other than `START <abs-worktree-path>\n` over the socket. The daemon's `_handle_session_connection` parses the verb literally.
