---
name: slack-webhook
description: Send the user a Slack message as Claude — progress, a finished task, an alert, anything worth interrupting them for. Use whenever you want to reach the user outside this chat. Required instead of the Slack connector, which posts as the user and doesn't notify them.
---

# Send Slack messages as Claude

One command, one message, straight into the user's Slack.

```bash
python skills/slack-webhook/send.py "Migration finished — 412 rows moved, no errors."
```

## Use this, not the Slack connector

The built-in Slack tools authenticate as the **user**, so anything they post
looks like the user talking to themselves — and Slack doesn't notify anyone about
their own message. This script posts through a webhook owned by the Claude app,
so it arrives as Claude and can @-mention the user for a real notification.

**Never reach the user through the Slack connector.** If this script fails, say
so rather than quietly falling back to it.

## Options

```bash
# ping the user so their phone lights up
send.py --mention U0123ABCDEF "Need a decision before I continue."

# a different channel (each webhook is bound to one)
send.py --to design "New mockups are in the PR."

# short one-liners, Slack's own syntax, raw emoji
send.py --plain ":white_check_mark: build green"

# what's available
send.py --list
```

| Flag | Does |
|---|---|
| `--mention U…` | @-mentions that member — the thing that actually pushes a notification |
| `--to NAME` | picks a configured webhook; each one posts to a fixed channel |
| `--plain` | sends Slack mrkdwn instead of a markdown block |
| `--list` | prints the configured webhook names |

By default the message is a **markdown block**: headings, lists, tables, links,
and fenced code all render. Use `--plain` for one-liners and `:emoji:`.

## What it can't do

Read anything. A webhook is write-only, so there is **no way to receive the
user's reply** here. If you need an answer:

- ask the question, then stop and wait for the user in this chat, or
- use the `slack-as-claude` skill (needs a bot token), which polls the thread.

Don't claim you'll "wait for their Slack reply" — you won't see it.

## When to send

1. **Finishing** long work, so the user doesn't have to keep checking.
2. **Blocking** on a decision — send the question, then wait here.
3. **Failing** in a way they need to know about now.

Don't narrate. A message per step is noise; one per milestone is useful. Skip
`--mention` for anything that isn't worth a phone buzz.

## Setup (once)

`config.json`, next to the script:

```json
{
  "webhooks": {
    "default": "https://hooks.slack.com/services/T…/B…/…",
    "design": "https://hooks.slack.com/services/T…/B…/…"
  }
}
```

Get a URL from **api.slack.com/apps → your app → Incoming Webhooks → Add New
Webhook to Workspace**, then pick a channel. One webhook per channel.
`SLACK_WEBHOOK_URL` in the environment overrides `default`.

Claude on the web also needs egress to `hooks.slack.com`:
**Settings → Capabilities → Code execution → Allow network egress**, with
`hooks.slack.com` (or `*.slack.com`) on the domain allowlist. A `slack.com`
entry does **not** cover it. Settings apply to **new** chats only.
