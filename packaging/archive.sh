#!/bin/sh
# Package the source tree as the release archive.
#
#   archive.sh <version> <out-dir> [<git-ref>]
#
# Writes <out-dir>/claude-slack-bridge-<version>.tar.gz and a .sha256 beside
# it — what publish-release.yml uploads and what generate-formula.sh later
# reads the hash out of.
#
# There is nothing compiled to ship: the release IS the source, because the
# Homebrew formula installs it into libexec and the daemon is built from it by
# `docker compose up --build` on first start. So this is `git archive` rather
# than a build, which also means only tracked files can end up in it and the
# export-ignore rules in .gitattributes decide what is left out.
#
# The files land at the archive root with no wrapping directory, matching what
# the formula's `libexec.install Dir["*"]` expects. GitHub's own auto-generated
# tarballs wrap everything in one, which is the main reason we publish our own.
set -eu

if [ "$#" -lt 2 ]; then
    echo "usage: archive.sh <version> <out-dir> [<git-ref>]" >&2
    exit 1
fi

version="$1"
out="$2"
ref="${3:-HEAD}"

archive="claude-slack-bridge-${version}.tar.gz"

mkdir -p "$out"
out_abs="$(cd "$out" && pwd)"

git archive --format=tar.gz --output="${out_abs}/${archive}" "$ref"

# The formula builds a Docker image out of this, so a tarball missing the build
# inputs installs cleanly and only fails at first start, in launchd's log where
# nobody is looking. Check here instead.
for required in Dockerfile docker-compose.yml entrypoint.sh requirements.txt \
                bin/claude-slack-bridge config.env.default src/main.py; do
    if ! tar -tzf "${out_abs}/${archive}" | grep -qx "$required"; then
        echo "the archive is missing $required" >&2
        exit 1
    fi
done

# ...and nothing that .gitattributes says to leave out. export-ignore on a
# directory works through the directory entry, so a pattern that looks right can
# quietly stop pruning — which would ship the test suite and the release
# machinery to every user, visible only to whoever unpacks a tarball and looks.
for excluded in tests packaging plans .github .roadmap_features pytest.ini; do
    if tar -tzf "${out_abs}/${archive}" | grep -qE "^${excluded}(/|\$)"; then
        echo "the archive contains $excluded, which .gitattributes excludes" >&2
        exit 1
    fi
done

# "<hash> *<name>" from either platform's tool — the shape generate-formula.sh
# parses and `sha256sum -c` accepts. GitHub's Linux runners have sha256sum and
# its macOS ones only shasum, and the two agree on this format.
(
    cd "$out_abs"
    if command -v sha256sum > /dev/null; then
        sha256sum -b "$archive" > "${archive}.sha256"
    else
        shasum -a 256 -b "$archive" > "${archive}.sha256"
    fi
)

cat "${out_abs}/${archive}.sha256"
