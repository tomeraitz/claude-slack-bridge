# Implementation Plan: add-to-cursor — Connect Cursor IDE to Claude-Slack-Bridge

## Overview

This plan implements support for Cursor IDE as a second MCP client in the Claude-Slack-Bridge. The work is split into four phases that can each be executed by an independent sub-agent with full context from disk.

**Design doc:** `.roadmap_features/add-to-cursor/design/feature_design.md`

### Phase map (execution order)

| Phase | Slug | Summary |
|---|---|---|
| 1 | `config-and-credentials` | Extend `Config` and `.env.example` to support dual-bot credentials via `CLIENT_ID` |
| 2 | `session-routing` | Update `session.py` to select the correct Slack bot credentials based on `CLIENT_ID` |
| 3 | `daemon-dual-bot` | Update `slack_daemon.py` to run two Socket Mode clients (one per Slack App) |
| 4 | `docs-and-readme` | Write `docs/cursor-setup.md` and update `README.md` |

Phases 1 and 4 are independent of each other. Phase 2 depends on Phase 1. Phase 3 depends on Phase 1. Phase 4 depends on none (documentation only, can be done in parallel with code phases).

---

## Phase 1 — config-and-credentials

### Goal

Add `CURSOR_SLACK_BOT_TOKEN`, `CURSOR_SLACK_APP_TOKEN`, and `CLIENT_ID` to the application config model and document them in `.env.example`. This is the foundation both `session.py` and `slack_daemon.py` changes build on.

### Inputs

- Design doc: `.roadmap_features/add-to-cursor/design/feature_design.md`
- `src/config.py` — current pydantic-settings Config class
- `.env.example` — current env var documentation

### Steps

1. **Modify `src/config.py`:**
   - Add `cursor_slack_bot_token: str = ""` field (optional; empty string default so it is not required when `CLIENT_ID != cursor`).
   - Add `cursor_slack_app_token: str = ""` field (same reasoning).
   - Add `client_id: str = "claude"` field — reads from `CLIENT_ID` env var, defaults to `"claude"`.
   - Keep all existing fields unchanged.

2. **Modify `.env.example`:**
   - After the existing `SLACK_APP_TOKEN` line, add a new section:
     ```
     # --- Cursor bot credentials (required only if you use Cursor IDE as an MCP client) ---
     # Create a second Slack App for cursor-bot (see docs/cursor-setup.md).
     CURSOR_SLACK_BOT_TOKEN=xoxb-...
     CURSOR_SLACK_APP_TOKEN=xapp-...
     ```
   - Do not remove or change any existing lines.

### Outputs

- `src/config.py` — updated with three new fields
- `.env.example` — updated with Cursor credential placeholders

### Acceptance

- `python -c "from config import Config; c = Config(); print(c.client_id)"` prints `claude` (default) when run in the container (or locally with the env vars set).
- `python -c "from config import Config; import os; os.environ['CLIENT_ID']='cursor'; os.environ['CURSOR_SLACK_BOT_TOKEN']='xoxb-test'; os.environ['CURSOR_SLACK_APP_TOKEN']='xapp-test'; c = Config(); print(c.client_id, c.cursor_slack_bot_token)"` prints `cursor xoxb-test`.
- Existing unit tests still pass: `pytest tests/` exits 0.

### Depends on

None.

---

## Phase 2 — session-routing

### Goal

Update `session.py` so that when `CLIENT_ID=cursor` the session uses `CURSOR_SLACK_BOT_TOKEN` (and a matching `AsyncApp`) to post messages, while the default (`CLIENT_ID=claude`) continues to use the existing `SLACK_BOT_TOKEN`. The daemon's Unix socket protocol is unchanged.

### Inputs

- Design doc: `.roadmap_features/add-to-cursor/design/feature_design.md`
- Phase 1 output: `src/config.py` (must have `client_id`, `cursor_slack_bot_token`, `cursor_slack_app_token` fields)
- `src/session.py` — current session entry point
- `src/session_broker.py` — for context (not modified)
- `src/mcp_server.py` — for context (not modified)

### Steps

1. **Modify `src/session.py`** — update the `run(config)` function:
   - After reading `config`, determine which bot token to use:
     ```python
     if config.client_id == "cursor":
         bot_token = config.cursor_slack_bot_token
         if not bot_token:
             raise RuntimeError(
                 "CLIENT_ID=cursor requires CURSOR_SLACK_BOT_TOKEN to be set."
             )
     else:
         bot_token = config.slack_bot_token
     ```
   - Replace the hardcoded `AsyncApp(token=config.slack_bot_token)` call with `AsyncApp(token=bot_token)`.
   - No other changes; the rest of `run()` is token-agnostic.
   - Update the module docstring to mention `CLIENT_ID` and the dual-bot model.

### Outputs

- `src/session.py` — updated to select bot token based on `config.client_id`

### Acceptance

- Running `docker exec -e CLIENT_ID=cursor -e CURSOR_SLACK_BOT_TOKEN=<valid_token> ... python session.py` uses the cursor-bot token (observable in Slack: message appears under the cursor-bot name).
- Running `docker exec ... python session.py` (no `CLIENT_ID`) still uses the claude-bot token.
- `pytest tests/` exits 0.

### Depends on

Phase 1.

---

## Phase 3 — daemon-dual-bot

### Goal

Update `slack_daemon.py` and its startup in `main.py` to listen for Socket Mode events from *both* Slack Apps (claude-bot and cursor-bot) concurrently. Each app gets its own `AsyncApp` + `AsyncSocketModeHandler`; both share the same `_pending` dict (keyed by `thread_ts`) and Unix socket server, so replies are routed correctly regardless of which bot posted.

### Inputs

- Design doc: `.roadmap_features/add-to-cursor/design/feature_design.md`
- Phase 1 output: `src/config.py` (must have `cursor_slack_bot_token`, `cursor_slack_app_token` fields)
- `src/slack_daemon.py` — current daemon implementation
- `src/main.py` — daemon entry point
- `src/claude_handler.py` — for context (not modified)

### Steps

1. **Modify `src/slack_daemon.py`:**
   - Rename the constructor signature:
     - Keep `__init__(self, bot_token: str, app_token: str)` as the primary (claude-bot) identity.
     - Add optional `cursor_bot_token: str = ""` and `cursor_app_token: str = ""` parameters.
   - Inside `__init__`:
     - Keep existing `self._app` / `self._handler` (claude-bot).
     - If `cursor_bot_token` and `cursor_app_token` are non-empty, create:
       ```python
       self._cursor_app = AsyncApp(token=cursor_bot_token)
       self._cursor_handler = AsyncSocketModeHandler(self._cursor_app, cursor_app_token)
       ```
       Register the **same** event handlers (`_handle_slack_message`, `_handle_app_mention`) on `self._cursor_app`.
     - Otherwise set `self._cursor_handler = None`.
   - In `start()`:
     - Add the cursor handler to the `asyncio.gather(...)` call if it is not `None`:
       ```python
       handlers = [server.serve_forever(), self._handler.start_async()]
       if self._cursor_handler is not None:
           handlers.append(self._cursor_handler.start_async())
       await asyncio.gather(*handlers)
       ```
   - No changes to `_handle_session_connection`, `_pending`, `_lock`, or any business logic.

2. **Modify `src/main.py`:**
   - In `run(config)`, pass cursor credentials to `SlackDaemon`:
     ```python
     daemon = SlackDaemon(
         bot_token=config.slack_bot_token,
         app_token=config.slack_app_token,
         cursor_bot_token=config.cursor_slack_bot_token,
         cursor_app_token=config.cursor_slack_app_token,
     )
     ```

### Outputs

- `src/slack_daemon.py` — updated to optionally run a second Socket Mode client
- `src/main.py` — updated to pass cursor credentials to `SlackDaemon`

### Acceptance

- When only `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` are set, the daemon starts normally (single bot mode, backward compatible).
- When both sets of tokens are set, the daemon starts two Socket Mode connections. Replying in a Cursor-bot thread routes the reply back to the waiting Cursor session correctly.
- `pytest tests/` exits 0.

### Depends on

Phase 1.

---

## Phase 4 — docs-and-readme

### Goal

Write `docs/cursor-setup.md` (the user-facing setup guide for Cursor IDE) and update `README.md` to mention Cursor as a supported client.

### Inputs

- Design doc: `.roadmap_features/add-to-cursor/design/feature_design.md`
- `docs/mcp-client-setup.md` — existing Claude Code guide (use as structural template)
- `README.md` — current README (to find the right insertion points)
- `.env.example` — to accurately document required env vars

### Steps

1. **Create `docs/cursor-setup.md`:**
   - Title: `# Cursor IDE MCP Setup — Connecting Cursor to the Bridge`
   - Sections:
     - **Prerequisites** — daemon running, two Slack Apps created (link to `docs/slack-setup.md` for App creation; note that a *second* App is needed for cursor-bot), `CURSOR_SLACK_BOT_TOKEN` and `CURSOR_SLACK_APP_TOKEN` set in `.env` and container restarted.
     - **How It Works** — same daemon+session model as Claude Code; `CLIENT_ID=cursor` selects the cursor-bot identity; messages appear under the cursor-bot name/avatar in Slack.
     - **Step 1 — Create a Second Slack App for cursor-bot** — brief checklist (create App, enable Socket Mode, add `chat:write`+`channels:history` scopes, invite to channel, copy `xoxb-` and `xapp-` tokens).
     - **Step 2 — Add cursor credentials to `.env`** — show the two new env var lines; remind user to `docker compose up -d --build` to reload.
     - **Step 3 — Add `.cursor/mcp.json` to your project** — JSON snippet using `docker exec` with `CLIENT_ID=cursor`, `SLACK_CHANNEL`, and `TIMEOUT_LIMIT_MINUTES`:
       ```json
       {
         "mcpServers": {
           "claude-slack-bridge": {
             "command": "docker",
             "args": [
               "exec", "-i",
               "-e", "SLACK_CHANNEL",
               "-e", "TIMEOUT_LIMIT_MINUTES",
               "-e", "CLIENT_ID",
               "claude-slack-bridge",
               "python", "session.py"
             ],
             "env": {
               "SLACK_CHANNEL": "#your-project-channel",
               "TIMEOUT_LIMIT_MINUTES": "5",
               "CLIENT_ID": "cursor"
             }
           }
         }
       }
       ```
       - Note: project-level path is `.cursor/mcp.json`; global path is `~/.cursor/mcp.json`.
       - Note: add `.cursor/mcp.json` to `.gitignore` (contains channel name).
     - **Step 4 — Verify the setup** — checklist: daemon running, open project in Cursor, check MCP panel for `ask_on_slack` tool, trigger it, confirm cursor-bot posts in Slack.
     - **Environment Variables Reference** — table listing `CURSOR_SLACK_BOT_TOKEN`, `CURSOR_SLACK_APP_TOKEN` (set in `.env`); and `SLACK_CHANNEL`, `CLIENT_ID`, `TIMEOUT_LIMIT_MINUTES` (set in `.cursor/mcp.json`).
     - **Limitations** — copy the relevant limitations from the design doc (no Slack→Cursor, MCP roots may not work, Docker must be local, two Slack Apps required).

2. **Modify `README.md`:**
   - In the **Requirements** section, change `- Claude Code (or any MCP-compatible client)` to `- Claude Code or Cursor IDE (or any MCP-compatible client)`.
   - In the **Next steps** table, add a row: `| Use Cursor IDE as an MCP client | [docs/cursor-setup.md](docs/cursor-setup.md) |`.
   - Optionally: update the "What It Does" opening paragraph to mention Cursor alongside Claude Code.

### Outputs

- `docs/cursor-setup.md` — new file, complete setup guide
- `README.md` — updated with Cursor references and link to setup guide

### Acceptance

- `docs/cursor-setup.md` exists and is valid Markdown (render with any Markdown viewer).
- `README.md` contains a link to `docs/cursor-setup.md`.
- The JSON snippet in `docs/cursor-setup.md` includes `"CLIENT_ID": "cursor"` in the `env` block.
- No existing docs are modified beyond `README.md`.

### Depends on

None (documentation is independent of code phases).
