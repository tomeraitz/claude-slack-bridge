# Native CLI — No Docker, No Daemon

Run the Claude → Slack half of the bridge as a plain command on your machine.
There is no container and no background process. Claude Code starts the CLI when
it needs it, and the CLI talks to Slack directly with the bot token.

| | Native CLI | Docker daemon |
|---|---|---|
| `ask_on_slack` / `notify_on_slack` in Claude Code | ✅ | ✅ |
| `send` / `ask` from your shell, scripts, hooks | ✅ | — |
| Tag `@bot` in Slack to run Claude (Slack → Claude) | — | ✅ |
| Needs | Python 3.10+, `SLACK_BOT_TOKEN` | Docker, `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN` |
| How a reply is picked up | the CLI checks the thread every few seconds | pushed instantly over Socket Mode |

You can use both: the CLI and the daemon can run side by side with the same Slack app.

---

## Install

You need the Slack app and bot token from [slack-setup.md](slack-setup.md). The
CLI only uses the `xoxb-` bot token. You don't need Socket Mode or the `xapp-` token.

```bash
git clone https://github.com/your-username/claude-slack-bridge.git
cd claude-slack-bridge
./install.sh                 # creates .venv, installs deps, links ~/.local/bin/claude-slack-bridge
cp .env.example .env         # set SLACK_BOT_TOKEN (and optionally a default SLACK_CHANNEL)
```

`install.sh` is safe to re-run. To link somewhere else, run `BIN_DIR=/usr/local/bin ./install.sh`.

Check that it works by inviting the bot to a channel (`/invite @your-bot`), then running:

```bash
claude-slack-bridge send --channel "#my-channel" "hello from the CLI"
```

---

## Use it from Claude Code

Register the MCP server once per project:

```bash
claude mcp add claude-slack-bridge -e SLACK_CHANNEL="#my-channel" -- claude-slack-bridge mcp
```

- **Scope:** the default `local` scope means this project only. Add `-s user` to use it
  in every project (they then all post to that one channel).
- **Tokens:** they stay in the bridge's `.env`, which the CLI reads no matter which
  folder Claude Code starts it from. Only the channel goes in the registration.
- **Check:** run `claude mcp list`, or `/mcp` inside a session. You should see `ask_on_slack`
  and `notify_on_slack`.

If you'd rather write `.mcp.json` by hand:

```json
{
  "mcpServers": {
    "claude-slack-bridge": {
      "command": "claude-slack-bridge",
      "args": ["mcp"],
      "env": { "SLACK_CHANNEL": "#my-channel" }
    }
  }
}
```

If `claude-slack-bridge` isn't on the `PATH` that Claude Code sees, use the
absolute path to `bin/claude-slack-bridge`.

The tools behave as they do with Docker. Each Claude session gets its own
thread, and follow-up questions stay in that thread.

---

## Use it from the shell

```bash
claude-slack-bridge send "Build finished ✅"                # prints the thread ts
claude-slack-bridge ask "Deploy to prod?"                   # waits, prints your reply
claude-slack-bridge ask --timeout 600 "Merge now?"          # exit code 2 if no reply in 10 min
make test 2>&1 | tail -20 | claude-slack-bridge send -      # message from stdin
```

Keep a conversation in one thread:

```bash
ts=$(claude-slack-bridge send "Starting the migration")
answer=$(claude-slack-bridge ask --thread "$ts" "Step 2 drops a column — continue?")
[ "$answer" = "yes" ] && ./migrate.sh step2
```

| Flag | Applies to | Meaning |
|---|---|---|
| `--channel` | all | Channel name or ID. Default: `SLACK_CHANNEL`. |
| `--thread TS` | `send`, `ask` | Post into an existing thread. |
| `--timeout N` | `ask` | Stop waiting after N seconds (exit code 2). Default: wait forever. |
| `--env-file PATH` | all | Use this env file instead of the bridge's `.env`. |
| `-v` | all | Log progress to stderr. |

Exit codes: `0` ok, `1` error (bad token, bot not in channel, …), `2` `ask` timed out.

---

## Configuration

Values are read from the environment first, then from an env file. The env file is
the first of these that applies: `--env-file`, then `$CLAUDE_SLACK_BRIDGE_ENV`, then
`.env` at the bridge repo root. The CLI never reads `.env` from the current directory.

| Variable | Required | Default | Description |
|---|---|---|---|
| `SLACK_BOT_TOKEN` | Yes | — | Bot token (`xoxb-…`) |
| `SLACK_CHANNEL` | No* | — | Default channel. *Required if you don't pass `--channel`. |
| `SLACK_POLL_INTERVAL` | No | `3` | Seconds between checks for a reply while `ask` waits |

---

## How replies are detected

After posting, the CLI reads the thread with `conversations.replies` every
`SLACK_POLL_INTERVAL` seconds. The first message that meets all of these is
returned as your answer:

- it was posted by a human (no `bot_id`),
- it is a normal message (not a join or other system event),
- it is newer than the question.

If the message has attachments, their file names are appended. When Slack
rate-limits a request, the CLI waits as long as Slack asks (`Retry-After`) and
then keeps checking.

Reply **in the thread**, not in the channel. A message posted directly to the
channel is not seen.

The CLI uses the history scopes from [slack-setup.md](slack-setup.md):
`channels:history`, `groups:history` for private channels, and `im:history` for DMs.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `error: not_in_channel` / `channel_not_found` | Invite the bot to the channel (`/invite @your-bot`). |
| `error: missing_scope` while waiting | Add the `*:history` scope for that channel type and reinstall the app. |
| `error: SLACK_BOT_TOKEN is not set` | Fill in `.env` at the bridge repo root, or pass `--env-file`. |
| `no virtualenv … run install.sh first` | Run `./install.sh` in the bridge repo. |
| Claude Code says the server failed to start | Use the absolute path to `bin/claude-slack-bridge` in `claude mcp add`. |

Windows: the CLI doesn't use Unix sockets, so it runs natively. The `bin/`
wrapper is a POSIX shell script, though, so on Windows either use WSL or call
`.venv\Scripts\python src\cli.py` directly.
