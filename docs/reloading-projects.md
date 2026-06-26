# Reloading `projects.json` without a restart

The daemon reads [`projects.json`](../projects.json) — the Slack channel → project
mapping — **once at startup**, then resolves each channel name to its Slack ID
and caches the result in memory. Editing the file therefore has no effect until
the daemon re-reads it.

Restarting the daemon would re-read it, but at a cost: it drops the Slack Socket
Mode connection and kills any in-flight Claude runs. To avoid that, the daemon
can reload the mapping **live**.

## How to reload

Two triggers, same effect — pick by whether you want a confirmation line:

| Trigger | Command | Output |
| --- | --- | --- |
| CLI (socket verb) | `./reload-mapping.sh` | `reloaded N channel(s)` |
| Signal (ops idiom) | `kill -HUP <daemon-pid>` | none (check logs) |

`SIGHUP` is the no-dependency trigger — it needs neither the venv nor the
socket. Process managers just wrap it: `pm2 sendSignal SIGHUP claude-slack-bridge`
and `docker kill --signal=HUP claude-slack-bridge` both send the same `SIGHUP`.

The CLI also accepts a non-default socket path:

```bash
./reload-mapping.sh --socket /tmp/other.sock
```

In Docker, run the CLI inside the container (and mount `projects.json` so your
edits reach it):

```bash
docker exec claude-slack-bridge python src/reloadctl.py
```

## What a reload does

1. Re-reads `projects.json` from disk.
2. Re-queries Slack (`conversations_list`) to map channel names → IDs.
3. Builds the new channel→project table in a local dict and **swaps it in
   atomically**.

## Guarantees

- **No restart** — the Slack WebSocket and any running Claude runs are
  untouched; only the in-memory mapping is rebuilt.
- **Atomic** — a message arriving mid-reload sees either the old or the new
  mapping, never a half-built one.
- **Fail-safe** — if the Slack re-query fails, the previous mapping is kept.
- **New threads only** — a live conversation keeps the directory it started in
  (cached per-thread when the thread began); the new mapping applies to threads
  started after the reload.

## How it works

`reloadctl.py` opens the daemon's Unix control socket (`/tmp/slack-bridge.sock`,
the same socket sessions use to register for replies) and sends a single
`RELOAD` line. The daemon's socket handler recognises the verb, calls
`ClaudeHandler.reload_projects()`, and writes back the channel count. The SIGHUP
handler calls the same `reload_projects()` coroutine.

> First-time note: a daemon built before this feature does not understand the
> `RELOAD` verb and will close the connection without replying — `reload-mapping.sh`
> reports this explicitly. Restart the daemon once to load the feature; after
> that, reloads are live.
