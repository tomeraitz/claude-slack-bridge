#!/bin/sh
# install.sh — set up the native claude-slack-bridge CLI (no Docker).
#
#   ./install.sh                 # venv + deps, link into ~/.local/bin
#   BIN_DIR=/usr/local/bin ./install.sh
#
# Safe to re-run: it reuses the venv and refreshes the link.
set -e
cd "$(dirname "$0")"
repo="$(pwd)"
bin_dir="${BIN_DIR:-$HOME/.local/bin}"

if [ ! -x .venv/bin/python ]; then
    echo "Creating virtualenv in $repo/.venv"
    python3 -m venv .venv
fi
echo "Installing dependencies"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

chmod +x bin/claude-slack-bridge
mkdir -p "$bin_dir"
ln -sf "$repo/bin/claude-slack-bridge" "$bin_dir/claude-slack-bridge"
echo "Linked $bin_dir/claude-slack-bridge"

if [ ! -f .env ]; then
    echo "Next: cp .env.example .env and set SLACK_BOT_TOKEN (and SLACK_CHANNEL)."
fi
case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) echo "Note: $bin_dir is not on your PATH — add it to your shell profile." ;;
esac
