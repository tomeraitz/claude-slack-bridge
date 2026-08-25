#!/bin/sh
# Checks generate-formula.sh against a checksum file shaped exactly like the one
# packaging/archive.sh writes: "<hash> *<name>", not a bare hash. Getting that
# wrong produces a formula Homebrew rejects, and a fixture holding only a hash
# would not catch it.
set -eu
cd "$(dirname "$0")"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

version=2026.810.0
hash="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
name="claude-slack-bridge-${version}.tar.gz"
printf '%s *%s\n' "$hash" "$name" > "${work}/${name}.sha256"

formula="${work}/formula.rb"
./generate-formula.sh "$version" owner/repo "$work" > "$formula"

failed=0
fail() {
    echo "$1" >&2
    failed=1
}

# The formula is assembled by substituting into a template, so a broken edit
# yields a file that reads fine and only fails when someone runs `brew install`.
# This is the cheapest thing that would have caught it.
if command -v ruby > /dev/null; then
    ruby -c "$formula" > /dev/null || fail "the generated formula is not valid Ruby"
else
    echo "note: no ruby available, skipping the syntax check" >&2
fi

for value in $(grep -oE 'sha256 "[^"]*"' "$formula" | sed 's/sha256 "//; s/"//'); do
    printf '%s' "$value" | grep -qE '^[0-9a-f]{64}$' || fail "not a bare sha256: $value"
done
# Checks below that must not match the formula's own prose run against a
# comment-stripped copy: the caveats and the comments legitimately talk about
# `depends_on cask`, `post_install` and the placeholders, and a grep over the
# raw file cannot tell explaining something from doing it.
code="${work}/code.rb"
sed 's/#.*//' "$formula" > "$code"

# The lines that substitute a placeholder, and the ones that assert it is gone,
# both have to name it — excluded rather than the check weakened.
grep -vE 'gsub!|refute_match' "$code" | grep -q '@[A-Z_0-9]*@' && fail "unsubstituted placeholder left in the formula"

# The wrapper is unusable without both substitutions, and the formula is the
# only place they happen.
grep -q 's.gsub! "@LIBEXEC@", libexec' "$formula" || fail "the install step does not substitute @LIBEXEC@"
grep -q 's.gsub! "@ETC@", etc' "$formula" || fail "the install step does not substitute @ETC@"

# The release tarball is the Docker build context. Installing only the wrapper
# would pass every check up to first start.
grep -q 'libexec.install Dir\["\*"\]' "$formula" || fail "the install step does not install the tree"

# A Docker engine is a caveat, never a dependency: OrbStack, Colima and Docker
# Desktop all satisfy it, and `depends_on cask:` outside homebrew/core would
# both pick one and break `brew audit`.
grep -q 'depends_on cask' "$code" && fail "declares a cask dependency"
grep -q 'orbstack' "$formula" || fail "the caveats do not mention how to get Docker"

# The dev track has to point at this repo's default branch. A head spec naming
# the wrong one would quietly serve something else.
grep -q '^  head .*branch: "main"' "$formula" || fail "the head spec does not track main"
# A `head do` block holding only a url is what brew style rejects as redundant;
# the one-liner is the form that passes.
grep -q '^  head do' "$formula" && fail "head is a redundant block, not a one-liner"

# launchd needs a foreground process, which is the whole reason the wrapper
# exists; a service block that lost keep_alive would not come back after a
# reboot.
grep -q 'keep_alive true' "$formula" || fail "the service does not keep alive"
grep -q 'run \[opt_bin/"claude-slack-bridge", "start"\]' "$formula" || fail "the service does not run the wrapper"

# No config may be created by the formula: it holds Slack tokens, and anything
# the formula writes there is something an upgrade can overwrite.
grep -q 'post_install' "$code" && fail "has a post_install that could touch the config"
grep -qE '^\s*etc\.install|\(etc/' "$code" && fail "installs into etc, which Homebrew freezes at first install"

if [ "$failed" -ne 0 ]; then
    echo "FAILED" >&2
    exit 1
fi
echo "ok"
