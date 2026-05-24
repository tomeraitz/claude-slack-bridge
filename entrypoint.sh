#!/bin/sh
set -e

if [ -n "$GITHUB_TOKEN" ] || [ -n "$GH_TOKEN" ]; then
    gh auth setup-git 2>&1 || echo "warning: gh auth setup-git failed; git push to github.com over HTTPS may not authenticate"
fi

exec "$@"
