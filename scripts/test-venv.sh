#!/usr/bin/env bash
# Build (or confirm) the test venv: the flax service image's runtime, python 3.12 +
# requirements.txt (flax_control/Dockerfile), plus the test-only pins in
# requirements-test.txt. scripts/run-tests.sh calls this, so it is normally automatic.
#
#   scripts/test-venv.sh [venv-dir]          default ~/.venv-reaper-test
#
# Rebuilds only when a requirements file changes (stamp = their sha256). Uses python3.12
# when installed, else uv fetches one. Separate from the ansible toolchain venv
# (requirements-ansible.txt only) and from any personal dev venv.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${1:-${REAPER_TEST_VENV:-$HOME/.venv-reaper-test}}"
PY="${REAPER_TEST_PYTHON:-python3.12}"
PYVER=3.12.13      # = flax-control image (python:3.12-slim at 0.9.209) = bangs' /usr/bin/python3.12
REQS=(requirements-test.txt requirements-dev.txt requirements.txt requirements-ansible.txt)
STAMP="${VENV}/.reaper-test-stamp"

want="$(cd "$ROOT" && { echo "python $PYVER"; cat "${REQS[@]}"; } | sha256sum | cut -d' ' -f1)"
if [[ -x "${VENV}/bin/python" && -f "$STAMP" && "$(cat "$STAMP")" == "$want" ]]; then
  exit 0
fi
# a venv (pyvenv.cfg: only venvs have one, so this never removes anything else) on
# another interpreter -- built by us or not -- is rebuilt from scratch, never patched
if [[ -f "${VENV}/pyvenv.cfg" && "$("${VENV}/bin/python" -c 'import platform; print(platform.python_version())' 2>/dev/null)" != "$PYVER" ]]; then
  rm -rf "$VENV"
fi
echo "test-venv: building ${VENV}" >&2
export PIP_ROOT_USER_ACTION=ignore PIP_DISABLE_PIP_VERSION_CHECK=1
if command -v uv >/dev/null 2>&1; then
  [[ -x "${VENV}/bin/python" ]] || uv venv --quiet --python "$PYVER" "$VENV"
  uv pip install --quiet --python "${VENV}/bin/python" -r "$ROOT/requirements-test.txt"
elif command -v "$PY" >/dev/null 2>&1; then
  have="$("$PY" -c 'import platform; print(platform.python_version())')"
  [[ "$have" == "$PYVER" ]] || echo "test-venv: warning: $PY is $have, the image runs $PYVER" >&2
  [[ -x "${VENV}/bin/python" ]] || "$PY" -m venv "$VENV"
  "${VENV}/bin/python" -m pip install --quiet -r "$ROOT/requirements-test.txt"
else
  echo "test-venv: need uv or ${PY} to build ${VENV}" >&2
  exit 1
fi
echo "$want" > "$STAMP"
echo "test-venv: built ${VENV} ($("${VENV}/bin/python" --version))" >&2
