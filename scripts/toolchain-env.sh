# shellcheck shell=bash
# toolchain-env.sh: SOURCE this to get reaper's pinned toolchain env (the env the
# `reaper` shim and `reaper shell` use). For tools that want an activate script, e.g.
# VS Code's Ansible extension (ansible.python.activationScript). By hand, use
# `reaper shell` instead: it gives a prompt marker and a way out.
#
# Venv: $REAPER_VENV, else your own ~/.venv-reaper-ansible (./setup), else the bang's
# shared /opt/flax/venv/reaper-ansible (reaper deploy <site> reaper_toolchain).
# Sets REAPER_TOOLCHAIN to the venv chosen, or empty when there is none.
REAPER_ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
export REAPER_ROOT
# Always decrypt with reaper's own per-site script; a caller's
# ANSIBLE_VAULT_PASSWORD_FILE (e.g. a ~/.bashrc export) must not override it.
export ANSIBLE_VAULT_PASSWORD_FILE="${REAPER_ROOT}/scripts/vault-password.sh"
# ...and reaper's own ansible.cfg, whatever directory it is run from.
export ANSIBLE_CONFIG="${REAPER_ROOT}/ansible.cfg"
REAPER_TOOLCHAIN=""
for REAPER_TOOLCHAIN in "${REAPER_VENV:-}" "${HOME}/.venv-reaper-ansible" /opt/flax/venv/reaper-ansible ""; do
  [[ -n "$REAPER_TOOLCHAIN" && -x "${REAPER_TOOLCHAIN}/bin/python" ]] && break
done
if [[ -n "$REAPER_TOOLCHAIN" ]]; then
  export VIRTUAL_ENV="$REAPER_TOOLCHAIN"
  export PATH="${REAPER_TOOLCHAIN}/bin:${PATH}"
  export ANSIBLE_COLLECTIONS_PATH="${REAPER_TOOLCHAIN}/collections"
fi
