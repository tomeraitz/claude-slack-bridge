# Slack Setup — Getting Your Tokens

## Step 1 — Create a Slack App

1. Go to https://api.slack.com/apps
2. Click **Create New App** → **From scratch**
3. Name it (e.g. `claude-bridge`)
4. Select your workspace
5. Click **Create App**

---

## Step 2 — Get the Bot Token (`xoxb-...`)

1. In your app's left sidebar, go to **OAuth & Permissions**
2. Scroll down to **Bot Token Scopes** and add these scopes:

   | Scope | Purpose |
   |---|---|
   | `chat:write` | Post messages |
   | `channels:history` | Read replies in public channels |
   | `groups:history` | Read replies in private channels |
   | `im:history` | Read replies in DMs |
   | `im:write` | Open DM conversations |
   | `reactions:read` | Detect the 🛑 stop reaction |
   | `reactions:write` | Add/remove the 🛑 reaction on running tasks |

   > **Note:** `reactions:read` also unlocks the `reaction_added` bot event in
   > Step 4. Slack hides that event until this scope is added, so add it here
   > first.

3. Scroll back up and click **Install to Workspace**
4. Click **Allow**
5. Copy the **Bot User OAuth Token** — it starts with `xoxb-...`

---

## Step 3 — Enable Socket Mode & Get the App Token (`xapp-...`)

1. In the left sidebar, go to **Socket Mode**
2. Toggle **Enable Socket Mode** → ON
3. It will prompt you to create an App-Level Token — click **Generate Token and Scopes** (or go to **Settings → Basic Information** → scroll to **App-Level Tokens**)
4. Name the token (e.g. `socket-mode`)
5. Add scope: `connections:write`
6. Click **Generate**
7. Copy the token — it starts with `xapp-...`

---

## Step 4 — Enable Event Subscriptions

1. In the left sidebar, go to **Event Subscriptions**
2. Toggle **Enable Events** → ON
3. Under **Subscribe to bot events** (the section with the **Add Bot User
   Event** button — *not* "Subscribe to events on behalf of users"), add:
   - `message.channels` — messages in public channels
   - `message.groups` — messages in private channels
   - `message.im` — messages in DMs
   - `reaction_added` — detect the 🛑 reaction used to stop a running task
4. Click **Save Changes**
5. Reinstall the app if prompted (**OAuth & Permissions** → **Reinstall to Workspace**)

> **`reaction_added` not in the list?** It only appears under **Subscribe to
> bot events** once the `reactions:read` scope from Step 2 is added. If you only
> see it under "Subscribe to events on behalf of users", that's the *user*
> events section — ignore it; add the `reactions:read` bot scope and `reaction_added`
> will show up under bot events. Tip: type just `reaction` to filter the list.

---

## Step 5 — Create a Channel per Project

1. In Slack, click **+** next to Channels → **Create channel**
2. Name it after the project (e.g. `vibki`, `resume-fitter`)
3. Click **Create**
4. Use the channel name as `SLACK_CHANNEL` (e.g. `#vibki`)

---

## Step 6 — Invite the Bot to Your Channel

In each project channel:
1. Click the channel name at the top to open its settings
2. Go to the chat apps to this channel
3. Search for your app and click **Add**

---

## Changing the App Name

The app name and the bot's display name are separate settings.

### Change the App Name
1. Go to https://api.slack.com/apps and select your app
2. In the left sidebar, go to **Settings → Basic Information**
3. Update the **App Name** field at the top
4. Click **Save Changes**

### Change the Bot's Display Name (what Slack shows)
1. In the left sidebar, go to **App Home**
2. Under **Your App's Presence in Slack**, click **Edit** next to the display name
3. Update it and save

### Apply the Changes
After renaming, reinstall the app:
1. Go to **OAuth & Permissions**
2. Click **Reinstall to Workspace**
3. Click **Allow**

---

## Summary — What You Have Now

| Variable | Value | Where to set |
|---|---|---|
| `SLACK_BOT_TOKEN` | `xoxb-...` | Docker container (shared) |
| `SLACK_APP_TOKEN` | `xapp-...` | Docker container (shared) |
| `SLACK_CHANNEL` | `#channel-name` or `U0123456789` | Project MCP config (per project) |
