#!/usr/bin/env bash
# Reload projects.json (the Slack channel → project mapping) in the running
# daemon WITHOUT restarting it — no dropped Slack connection, no killed runs.
#
# Usage: ./reload-mapping.sh   (run after editing projects.json)
#
# Prints "reloaded N channel(s)" on success. Alternative trigger with no
# confirmation line: pm2 sendSignal SIGHUP claude-slack-bridge
set -euo pipefail
cd "$(dirname "$0")"
exec ./.venv/bin/python src/reloadctl.py "$@"
