# Project Instructions

## Communication

Work in the terminal normally. Only reach out via Slack (using `mcp__claude-slack-bridge__ask_on_slack`) when you hit a roadblock — e.g. you need clarification, approval for a risky action, or are blocked on a decision. Day-to-day output and progress stays in the terminal. Do not use `AskUserQuestion` once you have sent your first Slack message in a conversation.

**Exception — setup/configuration skills:** The following skills run locally inside Claude Code as part of `/process-setup` and must use `AskUserQuestion` (not Slack), even if `ask_on_slack` was already used earlier in the session:

- `build-design-workflow`
- `build-plan-workflow`
- `build-run-plan-flow`
- `build-process-skill`

While executing any of these skills, follow the skill's own instructions for clarifications (local `AskUserQuestion`). Resume the normal communication rule above once the skill returns.

## Adding a channel → folder route

When asked to route a Slack channel to a project folder, edit `projects.json`
(repo root) and add one entry:

```jsonc
"#channel-name": "/abs/path/to/project"          // simple
"#channel-name": { "path": "/abs/path", "plugin_dir": "/abs/plugin/dir" }  // with a plugin dir
```

Rules:

- **The path must be an existing, absolute directory** — verify with
  `ls <path>` first. A path that doesn't exist is the #1 cause of "the project
  directory doesn't exist" replies. If it runs in Docker, the path must exist
  **inside the container** (a mounted volume), not just on the host.
- The channel key may be `#name`, a raw channel ID (`C…`), or a DM ID (`D…`).
- Don't add comment/placeholder keys (e.g. `"_README"`) — every key is treated
  as a channel and an unknown one just logs a warning.

Apply the change without restarting the daemon:

```bash
./reload-mapping.sh        # prints "reloaded N channel(s)"; warns about any missing dirs
```

Reloads take effect for new threads immediately; an existing thread whose
directory is unchanged keeps it, and a thread whose directory went missing
re-resolves on its next message. See `docs/reloading-projects.md`.
