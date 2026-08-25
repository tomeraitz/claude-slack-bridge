# Install with Homebrew

An alternative to the `git clone` + `docker compose up -d` instructions in the
README. What it changes:

- **launchd (or systemd) supervises the daemon**, not Docker's own restart
  policy. `brew services start/stop/restart` is the control surface, and the
  container comes back after a reboot without Docker Desktop having to decide
  that for itself.
- **Config lives outside the source tree**, in `~/.config/claude-slack-bridge/`.
  An upgrade replaces the code and cannot touch your tokens or your channel
  mapping.

Docker is still what runs the daemon. This packages *how it is started*, not
what it is.

```
brew services  ──▶  claude-slack-bridge  ──▶  docker compose up --build
   (launchd)          (foreground wrapper)         (the daemon)
```

## Install

```bash
# any engine will do — OrbStack is the lightest
brew install --cask orbstack

brew install lexbrugman/tap/claude-slack-bridge
```

The formula deliberately does not declare a Docker dependency: OrbStack,
Colima and Docker Desktop all satisfy it, and the formula has no business
picking one for you.

## Configure

The config is not created for you — it holds two Slack tokens, and a file no
installer writes is a file no upgrade can overwrite.

```bash
mkdir -p ~/.config/claude-slack-bridge
cp "$(brew --prefix claude-slack-bridge)/libexec/config.env.default" \
   ~/.config/claude-slack-bridge/config.env
chmod 600 ~/.config/claude-slack-bridge/config.env
```

Then edit it. The template documents every setting; the three that must be
right before the first start:

| Setting | Why |
| --- | --- |
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | From your Slack app — see [slack-setup.md](slack-setup.md). |
| `PROJECTS_DIR` | The **parent** directory of your projects. Mounted at `/projects` in the container, so a project at `~/Workspace/api` is `/projects/api` in the mapping. |
| `SECURITY_ALLOWED_USERS` | The template ships denying everyone. Read on. |

### The access-control setting is not optional

The Slack → Claude direction runs `claude -p` against your files, on your
machine, as you. Anyone who can post in a channel the Slack app is installed in
can drive it. Upstream's default is permissive; the template here ships
`SECURITY_ENABLED=true` with `SECURITY_STRICT_MODE=true` and empty lists, which
denies **everyone including you** until you add your own Slack user ID (Slack
profile → ⋮ → Copy member ID). See [security.md](security.md).

If you only want the Claude → Slack direction, leave the lists empty. That is
the closed configuration, and `ask_on_slack` is unaffected by it.

Where the config is looked for, in order:

1. `~/.config/claude-slack-bridge/config.env` — yours
2. `$(brew --prefix)/etc/claude-slack-bridge/config.env` — machine-level, if you
   prefer one config for all users of the machine
3. the `.env` in the installed tree — only relevant when running from a checkout

## Start it

```bash
brew services start claude-slack-bridge
claude-slack-bridge logs
```

The first start builds the image and takes a few minutes. Later ones are a cache
hit and near-instant. Wait for the Socket Mode connection line in the log before
expecting Slack to answer.

If Docker is not running yet, the wrapper waits for it rather than exiting —
which is what you want at login, when launchd starts services before Docker
Desktop has finished coming up.

## The channel → project mapping

`~/.config/claude-slack-bridge/projects.json`, created empty (`{}`) on first
start. It is bind-mounted into the container, so an edit needs no rebuild:

```jsonc
{
  "#api-channel": "/projects/api",
  "#web-channel": { "path": "/projects/web", "plugin_dir": "/projects/web/.claude" }
}
```

Paths are **container** paths under `/projects`, not host paths. Apply an edit
live — the Slack connection stays up and in-flight runs are not killed:

```bash
claude-slack-bridge reload      # → "reloaded N channel(s)"
```

See [reloading-projects.md](reloading-projects.md).

## Per-project `.mcp.json`

Unchanged by this install method — the container name is the same, so the
snippet in the README works as written.

## Day to day

```bash
claude-slack-bridge logs        # follow the container log
claude-slack-bridge reload      # re-read projects.json, live
claude-slack-bridge config      # which config file is in effect
brew services restart claude-slack-bridge
```

### After an upgrade, restart it

Homebrew does not restart services on upgrade. The new code sits in the Cellar
while the old container keeps serving, and nothing says so:

```bash
brew upgrade claude-slack-bridge
brew services restart claude-slack-bridge
```

The restart rebuilds the image from the newly installed tree, which is why the
wrapper runs `up --build` rather than plain `up`.

### Following the dev branch

```bash
brew install --HEAD lexbrugman/tap/claude-slack-bridge
```

Installs the tip of `main` instead of a release. Nothing is compiled either way
— the daemon is built by `docker compose up --build` — so this is only a
different source. A plain `brew upgrade` does not move a `--HEAD` install
forward; `brew upgrade --fetch-HEAD` does.

## Notes

- **Do not use `sudo brew services`.** The container mounts your `~/.claude`; a
  system daemon has the wrong home directory for it.
- **The container's own restart policy is off** under `brew services`. The
  wrapper sets `RESTART_POLICY=no` so launchd is the only thing restarting the
  container — two supervisors racing over one container is how you get a daemon
  that flaps and a `brew services stop` that does not stop anything.
- **Logs** go to `$(brew --prefix)/var/log/claude-slack-bridge.log` (the
  wrapper's own output: waiting for Docker, compose's build and startup).
  `claude-slack-bridge logs` shows the daemon's log inside the container.
