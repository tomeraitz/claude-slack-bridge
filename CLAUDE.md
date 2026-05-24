# Project Instructions

## Communication

Once you use `mcp__claude-slack-bridge__ask_on_slack` for the first time in a conversation, ALL further communication with the user must go through that tool. Do not use `AskUserQuestion`, and do not ask questions or request feedback as text in the terminal. Continue communicating exclusively via Slack until the user explicitly tells you to switch back to the terminal.

**Exception — setup/configuration skills:** The following skills run locally inside Claude Code as part of `/process-setup` and must use `AskUserQuestion` (not Slack), even if `ask_on_slack` was already used earlier in the session:

- `build-design-workflow`
- `build-plan-workflow`
- `build-run-plan-flow`
- `build-process-skill`

While executing any of these skills, follow the skill's own instructions for clarifications (local `AskUserQuestion`). Resume the Slack-only rule once the skill returns.
