#!/usr/bin/env bash
# Run reaper's test suite from this checkout (main clone, branch or worktree alike).
#
#   scripts/run-tests.sh [pytest opts]            whole suite
#   scripts/run-tests.sh tests/test_x.py [opts]   just those files
#
# Each pytest process builds its own database from this tree's schema/ and drops it
# afterwards (conftest.py), so parallel runs from different worktrees do not collide.
# That only happens on a cluster marked as a disposable test cluster (marker database
# flax_test_cluster); anywhere else the DB-backed tests skip. Never point PGHOST at a
# bang's live postgres: the 2026-09-20 incident deleted live work-record history
# through exactly that. conftest also sets every test DSN (superuser, flax_control,
# flax_post writer), so this wrapper only adds the venv and the destructive-retention
# opt-in (FLAX_RETENTION_DESTRUCTIVE_OK, safe on the per-run database).
#
# Python: the test venv (scripts/test-venv.sh: python 3.12 + the service image's pins +
# test-only pins), built or refreshed on demand. REAPER_TEST_PY overrides it.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${REAPER_TEST_PY:-}" ]]; then
  PY="$REAPER_TEST_PY"
else
  "$ROOT/scripts/test-venv.sh" || exit 2
  PY="${REAPER_TEST_VENV:-$HOME/.venv-reaper-test}/bin/python"
fi
cd "$ROOT"
args=("$@")
have_path=0
for a in "$@"; do [[ -e "${a%%::*}" ]] && have_path=1; done
(( have_path )) || args=(tests "${args[@]}")
exec env FLAX_RETENTION_DESTRUCTIVE_OK=1 "$PY" -m pytest "${args[@]}"
