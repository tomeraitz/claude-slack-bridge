---
name: build-task-manager
description: "Set up the task manager for this repo end-to-end: pick a manager (Linear / Jira / GitHub Issues / Notion / None), pick an integration method (MCP / CLI / plugin / API), verify the chosen integration is installed (offering to help install, wait for the user, or skip), capture the concrete invocation and scope, smoke-test the fetch, and write `.claude/skills/claude-slack-bridge_list-tasks/SKILL.md` from the plugin template. Returns a status of `configured` (with task_manager_label, task_manager_slug, integration_method) or `skipped`. Use as the task-manager phase of /process-setup."
---

# build-task-manager — set up the task manager and write the claude-slack-bridge_list-tasks helper

You run **locally inside Claude Code** (not via the Slack daemon). All clarifications go through `AskUserQuestion`. Do not call `mcp__claude-slack-bridge__ask_on_slack` from this skill.

This skill is the entire task-manager phase of `/process-setup`. By the time it returns, either:
- The user picked a manager, the integration is verified, the smoke-test succeeded, and `.claude/skills/claude-slack-bridge_list-tasks/SKILL.md` has been written from the plugin template (status: `configured`); or
- The user skipped (or aborted the install flow after retries), no helper skill was written (status: `skipped`).

Return values the caller needs (printed as a fenced JSON block at the end of the final reply):
- `status` — `configured` or `skipped`.
- `task_manager_label`, `task_manager_slug`, `integration_method` — populated when `status == "configured"`; omitted otherwise.

Do not skip ahead to writing the helper skill until Step 5 has verified the integration end-to-end.

---

## Step 1 — pick a task manager

Ask via `AskUserQuestion`:

> Which task manager do you use for this repo?

Options: `Linear`, `Jira`, `GitHub Issues`, `Notion`, `None / skip`.

If the answer is **None / skip**, return immediately with `status: "skipped"`.

Record:
- `task_manager_label` — the human label (e.g. `Linear`, `GitHub Issues`).
- `task_manager_slug` — lowercase slug (`linear`, `jira`, `github`, `notion`).

---

## Step 2 — pick the integration method and verify it's installed

### 2a. Integration method

Ask via `AskUserQuestion`:

> How is `{task_manager_label}` integrated in this environment?

All four integration methods are valid for every manager — including GitHub. Do **not** assume `gh` for github; the user may prefer the GitHub MCP server, a custom plugin, or direct REST.

Options (single-select, in this order):
1. **MCP server** — there is an MCP server providing task tools (e.g. `mcp__linear__list_issues`, `mcp__github__list_issues`).
2. **CLI tool** — there is a CLI installed (e.g. `gh`, `linear-cli`, `jira-cli`).
3. **Plugin / slash command** — there is a Claude plugin or slash command that lists tasks.
4. **Direct API (curl)** — call the manager's HTTP API directly with credentials from env vars.

Record the choice as `integration_method` ∈ `{mcp, cli, plugin, api}`.

### 2b. Availability check (and offer to install)

Run the matching availability check based on `integration_method`. The point is to catch the "user picked Linear MCP but never installed the Linear MCP server" case early, so we can offer to help.

- **mcp** — read `cwd/.mcp.json` and look for a server entry whose key plausibly matches `{task_manager_slug}` (e.g. `linear`, `jira`, `github`, `notion`). If none match, treat as not installed.
- **cli** — ask the user which CLI binary they intend to use (one short `AskUserQuestion`, free-text — e.g. `gh`, `linear`, `jira`). Then run `command -v <cli>` via Bash (or `where <cli>` on Windows). Non-zero exit ⇒ not installed.
- **plugin** — ask the user which plugin / slash command they intend to use, then check whether it appears in the available skills/commands list for this session. Absent ⇒ not installed.
- **api** — skip the install check; API integration only needs env vars, which Step 3 will surface.

If the check says **installed** (or `integration_method` is `api`), continue to Step 3.

If the check says **not installed**, ask via `AskUserQuestion`:

> `{task_manager_label}` ({integration_method}) doesn't appear to be installed in this repo. Want me to help you set it up?

Options:
1. **Yes, help me install it** — proceed with the install flow below.
2. **I'll install it myself, wait for me** — pause; ask the user to reply when they're done, then re-run 2b.
3. **Skip task manager integration** — return with `status: "skipped"`.

If the user picks **Yes, help me install it**, run the flow that matches `integration_method`:

- **mcp** — propose the canonical MCP server for `{task_manager_slug}` (Linear → `@modelcontextprotocol/linear` style entry, GitHub → `@modelcontextprotocol/github`, etc.; if you're not certain of the exact package, ask the user to confirm the package name rather than guessing). Show the user the proposed `.mcp.json` server entry, ask which env vars they need (API key, workspace id), and only after they confirm append the entry to `.mcp.json` (preserving existing servers — never rewrite the whole file). Do **not** write secrets into `.mcp.json`; reference them via env vars and tell the user where to set them. After writing, ask the user to reload the MCP server (usually by restarting Claude Code) and confirm before continuing.
- **cli** — detect the platform (`win32` on this user's machine, but check anyway). Propose the install command (`winget install …`, `scoop install …`, `brew install …`, `npm i -g …`, etc.) and ask the user to confirm before running. Run via Bash. After install, re-run `command -v <cli>` / `where <cli>` to verify.
- **plugin** — ask the user for the plugin or marketplace name. If it's a Claude Code plugin, point them at `/plugin` to install it; do not try to install plugins from inside this skill. Wait for the user to confirm the plugin is loaded, then re-check availability.

After install (or after the user says they've installed it themselves), re-run the availability check from the top of 2b. If it still fails, ask the user whether to retry, switch integration method (jump back to 2a), or skip (return with `status: "skipped"`). Do not loop more than 3 retries without offering to skip.

---

## Step 3 — concrete invocation

Based on the chosen method, ask one targeted follow-up via `AskUserQuestion` (use the free-text "Other" channel — these answers are repo-specific):

- **mcp** → "Which MCP tool should `claude-slack-bridge_list-tasks` call to fetch open tasks? (e.g. `mcp__linear__list_my_issues`)"
- **cli** → "Which command should `claude-slack-bridge_list-tasks` run to fetch open tasks? Paste the full command including flags (e.g. `gh issue list --assignee @me --state open --limit 20 --json number,title,body`)."
- **plugin** → "Which slash command or skill should `claude-slack-bridge_list-tasks` invoke? (e.g. `/my-tasks` or skill name `my-team-tasks`)"
- **api** → "Which HTTP endpoint and auth env var(s) should `claude-slack-bridge_list-tasks` use? (e.g. `https://api.linear.app/graphql` with `LINEAR_API_KEY`)"

Record as `integration_invocation` (free-text from the user).

---

## Step 4 — scope (project / team / workspace)

Ask via `AskUserQuestion`:

> Which project, team, or workspace holds the tasks for this repo, and how does `claude-slack-bridge_list-tasks` scope its query to it? (e.g. Linear team `ENG`, Jira project `PROJ`, GitHub repo `acme/web`, Notion DB id `abc123…`. Include the filter/parameter name if relevant — e.g. `team=ENG`, `repo=acme/web`.)

Record as `scope` (single free-text field — keep it open-ended; the user types whatever identifier their tool needs).

---

## Step 5 — run the find-the-tasks flow together (verify before writing the skill)

Do not write the helper skill yet. First, actually fetch tasks once using the values gathered so far (`integration_method` from Step 2, `integration_invocation` from Step 3, `scope` from Step 4). The goal is to (1) prove the integration works and (2) discover any missing scope/filter/auth before it's baked into the skill.

Run the call that matches `integration_method`:

- **mcp** — invoke the MCP tool named in `integration_invocation`, passing arguments derived from `scope`. If you're unsure which argument shape the tool expects, call it with the obvious mapping and let the error message guide a retry.
- **cli** — run the exact command in `integration_invocation` via Bash. If `scope` includes a filter the command doesn't yet have (e.g. `team=ENG`), ask the user how to add it, then re-run.
- **plugin** — invoke the slash command or skill via the Skill tool, passing `scope` as an argument if applicable.
- **api** — issue the HTTP request via `curl` (or Python) using the env vars in `integration_invocation`. If a required env var is missing, surface it to the user before retrying.

Show the user a short preview of what came back (e.g. the first 3 task titles, or the raw response trimmed). Then ask via `AskUserQuestion`:

> I fetched `{N}` task(s) from `{task_manager_label}`. Does this look like the right list?

Options:
1. **Yes, that's my task list** — proceed to Step 6.
2. **No, the scope/filter is wrong** — ask which field is wrong and loop back to Step 3 or Step 4 as appropriate, then re-run Step 5.
3. **No, the call failed** — discuss the error with the user, fix the integration (may loop back to Step 2b for missing install, Step 3 for wrong invocation, or Step 4 for wrong scope), then re-run Step 5.
4. **The list is empty but the call succeeded — write it anyway** — accept and proceed to Step 6. (Useful when the user has no open tasks right now but the integration is wired correctly.)

Do not move to Step 6 until the user picks option 1 or 4. Cap the loop at ~5 retries; if it still doesn't work, offer to skip task manager integration (return with `status: "skipped"`).

---

## Step 6 — confirm and write the helper skill

Now that the flow is verified, generate `.claude/skills/claude-slack-bridge_list-tasks/SKILL.md` from the plugin template.

Read the plugin template at `<plugin-root>/templates/task-manager.md.tmpl` (use `${CLAUDE_PLUGIN_ROOT}` if set, otherwise resolve by searching upward from this skill's directory until you find `plugin.json`).

Substitute:
- `{{TASK_MANAGER}}` → `task_manager_label`
- `{{TASK_MANAGER_SLUG}}` → `task_manager_slug`
- `{{INTEGRATION_METHOD}}` → `integration_method` (one of `mcp`, `cli`, `plugin`, `api`)
- `{{INTEGRATION_INVOCATION}}` → `integration_invocation` (verbatim user reply)
- `{{SCOPE}}` → `scope` (verbatim user reply)

Create `.claude/skills/claude-slack-bridge_list-tasks/` if missing and write the substituted text to `.claude/skills/claude-slack-bridge_list-tasks/SKILL.md`. Use atomic write (`.SKILL.md.tmp` → `os.replace`).

The generated `claude-slack-bridge_list-tasks` skill is invoked by the `/process` clarification skill via the Skill tool. The frontmatter `name` must be `claude-slack-bridge_list-tasks`.

After writing, return with `status: "configured"`.

---

## Return shape

End your final reply with a single fenced JSON block so the caller can parse the result.

For the configured case:

```json
{
  "status": "configured",
  "task_manager_label": "Linear",
  "task_manager_slug": "linear",
  "integration_method": "mcp"
}
```

For the skipped case:

```json
{
  "status": "skipped"
}
```
