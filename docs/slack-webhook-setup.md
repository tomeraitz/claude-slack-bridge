# Incoming Webhook — Letting Claude on the Web Message You

Add a webhook to the Slack app you already created in
[slack-setup.md](slack-setup.md), so **Claude on the web** can message you as
Claude — no Docker, no tunnel, no bot token in the cloud.

This picks up where that guide left off. You already have the `claude-bridge`
app installed, with `xoxb-`/`xapp-` tokens and a channel the bot is in. A
webhook is one more, much weaker credential on the same app.

## Why a webhook and not the bot token

|  | Incoming webhook | Bot token (`xoxb-`) |
|---|---|---|
| What it can do | post into **one** channel | post anywhere the bot is, read history, download files |
| What it can't | read *anything* | — |
| If it leaks | someone spams that one channel | they read your channels and files |

Claude's own Slack connector isn't an option here: it authenticates as **you**,
so its messages come from you and Slack doesn't notify you about your own
message. The webhook belongs to the app, so messages arrive as Claude and an
`<@you>` mention pings properly.

The cost is that a webhook is write-only. It cannot receive your reply — see
[Getting an answer back](#getting-an-answer-back).

## Step 1 — Turn on Incoming Webhooks

1. Go to https://api.slack.com/apps and select your app (`claude-bridge`, or
   whatever you named it in [Step 1](slack-setup.md#step-1--create-a-slack-app))
2. Left sidebar → **Features → Incoming Webhooks**
3. Toggle **Activate Incoming Webhooks** → **On**

> **Not in the sidebar?** It sits under **Features**, below *App Home* — the
> list is longer than the viewport, so scroll. If it's genuinely absent, add the
> scope instead: **OAuth & Permissions → Bot Token Scopes → Add an OAuth Scope**
> → `incoming-webhook`, then **Install App → Reinstall to Workspace**. Slack
> asks which channel to post to during the reinstall, and the section appears.

## Step 2 — Create the webhook

1. Scroll to the bottom → **Add New Webhook to Workspace**
2. Pick the channel — the same one from
   [Step 5](slack-setup.md#step-5--create-a-channel-per-project) is a good default
3. Click **Allow**
4. Copy the URL: `https://hooks.slack.com/services/T…/B…/…`

**One webhook is bolted to one channel, permanently.** For a second channel,
repeat this step; you'll get a second URL.

Treat the URL as the credential — anyone holding it can post in that channel.

Confirm it works from your own terminal, so a later failure is unambiguous:

```bash
curl -s -X POST -H 'Content-type: application/json' \
  --data '{"text":"webhook test"}' \
  https://hooks.slack.com/services/T…/B…/…
```

A healthy webhook prints a bare `ok` and the message lands in the channel.

## Step 3 — Let Claude's sandbox reach Slack

Claude on the web runs code in a sandbox with **no internet** until you allow it:

1. claude.ai → **Settings → Capabilities**
2. **Code execution and file creation** → on
3. **Allow network egress** → on
4. **Domain allowlist** → add `hooks.slack.com`

> Two traps here, both of which look like "the webhook is broken":
>
> - **`slack.com` does not cover `hooks.slack.com`.** They're separate hosts.
>   Use `*.slack.com` if wildcards are accepted, or list both.
> - **Network config is fixed when a chat starts.** Change the setting, then
>   **open a new chat** — editing it mid-conversation does nothing.
>
> A blocked request returns `403` with `x-deny-reason: blocked-by-allowlist`,
> and never reaches Slack.

## Step 4 — Configure and upload the skill

```bash
cp skills/slack-webhook/config.example.json skills/slack-webhook/config.json
```

```json
{
  "webhooks": {
    "default": "https://hooks.slack.com/services/T…/B…/…",
    "design": "https://hooks.slack.com/services/T…/B…/…"
  }
}
```

Name as many as you like; Claude picks one with `--to design`. If you'd rather
not store the URL at all, leave the file out and set `SLACK_WEBHOOK_URL` in the
sandbox instead — the script prefers the environment.

Build the bundle and upload it:

```bash
python skills/pack_skill.py slack-webhook      # → skills/slack-webhook.zip
```

claude.ai → **Settings → Skills → Add skill** → pick the zip.

Then **turn off the Slack connector** in claude.ai. While it's enabled Claude
will keep reaching for it and posting as you — the skill says not to, but a
missing tool is more reliable than an instruction.

## Step 5 — Use it

Ask Claude on the web for something long-running and tell it to report back:

```bash
python skills/slack-webhook/send.py "Migration finished — 412 rows, no errors."
python skills/slack-webhook/send.py --mention U0123ABCDEF "Need a decision to continue."
python skills/slack-webhook/send.py --to design "Mockups are in the PR."
python skills/slack-webhook/send.py --plain ":white_check_mark: build green"
```

`--mention` takes your Slack member ID (profile → ⋮ → *Copy member ID*) and is
what actually pushes a notification to your phone. Without it the message just
sits there unread.

Markdown is the default — headings, lists, tables, and fenced code all render.
`--plain` sends Slack's own mrkdwn dialect, which is better for one-liners and
raw `:emoji:`.

## Getting an answer back

A webhook can't read, so nothing here sees your reply. Options, cheapest first:

1. **Answer in the chat.** Claude sends the question, you read it on your phone,
   you reply in claude.ai. Fine when you're already at a machine.
2. **The `slack-as-claude` skill** — same idea, but with a bot token, so it can
   poll the thread and pick up your reply. Costs you a readable token in the
   bundle; use a dedicated app with only `chat:write` + `channels:history`.
3. **The MCP bridge** (`.mcp.json` + `session.py`) — the real `ask_on_slack`,
   which blocks on a socket until you answer and survives long runs. Local
   Claude Code only.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `403` / `blocked-by-allowlist` | `hooks.slack.com` isn't allowlisted, or you didn't start a new chat after changing it. |
| `cannot reach hooks.slack.com` | Network egress is off entirely. |
| `HTTP 404: no_service` | Webhook was deleted, or the URL is mistyped. |
| `HTTP 403: invalid_token` | App was uninstalled/reinstalled — the old URL died. Create a new webhook. |
| `HTTP 400: invalid_payload` | Malformed JSON body — shouldn't happen via the script; report it. |
| Message posted as you | Claude used the Slack connector. Disable it. |
| No notification | `--mention` missing, or the member ID is wrong. Check it renders as a blue chip. |
| Wrong channel | Webhooks are bound to a channel at creation. Make one per channel. |
