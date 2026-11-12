#!/usr/bin/env bash
# toolchain-venv.sh <venv-dir>: build or refresh reaper's pinned toolchain venv
# (spec 2026-11-07 reaper-cli §3.1): python 3.12.13 + requirements-ansible.txt, and the
# requirements.yml collections inside <venv-dir>/collections (never ~/.ansible).
#
#   dev machine:  ./setup                        -> ~/.venv-reaper-ansible
#   bangs:        reaper deploy all reaper_toolchain -> /opt/flax/venv/reaper-ansible
#
# Idempotent: a stamp of the two pin files is written only after a full, successful
# build; an unchanged stamp exits at once ("up to date"), so the role reports ok.
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
VENV="${1:?usage: toolchain-venv.sh <venv-dir>}"
PY="${REAPER_TOOLCHAIN_PYTHON:-python3.12}"
PYVER=3.12.13      # one interpreter everywhere: = flax-control image = bangs' /usr/bin/python3.12 = test venv
STAMP="${VENV}/.reaper-pins"
REQ="${ROOT}/requirements-ansible.txt"
COLL="${ROOT}/requirements.yml"

want="$({ echo "python $PYVER"; cat "$REQ" "$COLL"; } | sha256sum | cut -d' ' -f1)"
# a venv (pyvenv.cfg: only venvs have one, so this never removes anything else) on
# another interpreter -- built by us or not -- is rebuilt from scratch, never patched
if [[ -f "${VENV}/pyvenv.cfg" && "$("${VENV}/bin/python" -c 'import platform; print(platform.python_version())' 2>/dev/null)" != "$PYVER" ]]; then
  echo "toolchain: ${VENV} is not python ${PYVER}; rebuilding"
  rm -rf "$VENV"
fi
if [[ -x "${VENV}/bin/ansible" && -f "$STAMP" && "$(cat "$STAMP")" == "$want" ]]; then
  echo "toolchain: up to date (${VENV})"
  exit 0
fi

echo "toolchain: building ${VENV}"
export PIP_ROOT_USER_ACTION=ignore PIP_DISABLE_PIP_VERSION_CHECK=1
if command -v "$PY" >/dev/null 2>&1; then
  [[ -x "${VENV}/bin/python" ]] || "$PY" -m venv "$VENV"
  "${VENV}/bin/python" -m pip install --quiet -r "$REQ"
elif command -v uv >/dev/null 2>&1; then
  # a dev machine without python3.12: uv fetches the exact PYVER
  [[ -x "${VENV}/bin/python" ]] || uv venv --quiet --python "$PYVER" "$VENV"
  uv pip install --quiet --python "${VENV}/bin/python" -r "$REQ"
else
  echo "toolchain: need ${PY} or uv to build ${VENV}" >&2
  exit 1
fi
# --force: we only get here when a pin changed, and galaxy will not move an installed
# collection to another version without it
ANSIBLE_COLLECTIONS_PATH="${VENV}/collections" "${VENV}/bin/ansible-galaxy" collection install \
  --force -r "$COLL" -p "${VENV}/collections" --timeout 60 >/dev/null
echo "$want" > "$STAMP"
"${VENV}/bin/ansible" --version | head -n1
echo "toolchain: built ${VENV}"
