#!/usr/bin/env bash
# Install reaper's rule-zero git hooks (pre-commit + pre-push -> scripts/check-secrets)
# into a clone (default: the one this script sits in; `reaper setup` passes the clone it
# runs from). Idempotent. Worktrees share the clone's hooks, and each hook runs the
# checker of the worktree it fires in.
#   scripts/install-git-hooks.sh [clone-dir]
#
# Hooks live in the clone's own hooks dir, not core.hooksPath: setup also keeps its
# post-merge/post-checkout ssh-modes hooks there, and core.hooksPath would silence them.
# A hook that is not ours is left alone (and reported).
set -euo pipefail
REPO="$(cd "${1:-$(dirname "${BASH_SOURCE[0]}")/..}" && pwd)"
hooks="$(cd "$REPO" && git rev-parse --git-path hooks)"   # worktree-safe; git >= 2.5
[[ "$hooks" == /* ]] || hooks="$REPO/$hooks"
mkdir -p "$hooks"
if [[ -n "$(git -C "$REPO" config --get core.hooksPath || true)" ]]; then
    echo "install-git-hooks: core.hooksPath is set; git ignores $hooks. Unset it:" \
         "git -C $REPO config --unset core.hooksPath" >&2
fi
rc=0
for h in pre-commit pre-push; do
    f="$hooks/$h"
    if [[ -e "$f" ]] && ! grep -q 'reaper-secrets-check' "$f"; then
        echo "install-git-hooks: $f exists and is not reaper's; left alone (add a call to" \
             "scripts/check-secrets $h yourself)" >&2
        rc=1
        continue
    fi
    printf '%s\n' '#!/bin/sh' \
        "# reaper-secrets-check (written by scripts/install-git-hooks.sh): rule zero, no binaries" \
        'c="$(git rev-parse --show-toplevel)/scripts/check-secrets"' \
        '[ -x "$c" ] || exit 0      # a branch from before the checker existed' \
        'command -v python3.12 >/dev/null 2>&1 || { echo "check-secrets needs python3.12 (run ./setup; on a bang: reaper deploy <site> reaper_toolchain)" >&2; exit 1; }' \
        "exec \"\$c\" $h" > "$f"
    chmod 0755 "$f"
done
exit $rc
