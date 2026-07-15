#!/usr/bin/env bash
#
# sonic-l2-deploy.sh — push a golden L2 config_db.json onto a freshly
# installed SONiC switch and activate it.
#
# Turns a stock SONiC switch (default L3/BGP fabric config) into a flat
# layer-2 switch: no BGP, no per-port IPs, all front-panel ports untagged
# in one VLAN and admin-up, with the model's breakout layout applied.
#
# The golden config carries ONLY intent (PORT / BREAKOUT_CFG / VLAN /
# VLAN_MEMBER) — no MAC, hostname or serial — so it is generic across
# units of the SAME hwsku. On `config reload` the device merges its own
# /etc/sonic/init_cfg.json for all platform defaults (incl. its real MAC).
#
# Usage:
#   scripts/sonic-l2-deploy.sh <mgmt-ip> [ssh-user]
#
# Environment:
#   SONIC_PASS   password for <ssh-user> (also used for sudo on the box).
#                If unset, SSH key / agent auth is used and sudo must be
#                passwordless.
#   SSH_JUMP     optional ProxyJump for reaching the switch, e.g.
#                "dbahi@bang-gouda" (the eindhoven lab reaches vid-26 OOB
#                switches through bang-gouda; a customer with direct
#                reachability omits this).
#   GOLDEN       path to the golden config_db.json
#                (default: files/configs/golden/wedge100s-l2/config_db.json).
#   EXPECT_HWSKU hwsku the golden config is built for
#                (default: Accton-WEDGE100S-32X). The script refuses to
#                apply to a different hwsku unless FORCE=1.
#   FORCE=1      skip the hwsku match guard.
#
# Examples:
#   # eindhoven lab, through the bang-gouda jump host:
#   SONIC_PASS='YourPaSsWoRd' SSH_JUMP='dbahi@bang-gouda' \
#     scripts/sonic-l2-deploy.sh 172.26.0.51
#
#   # customer site, direct reachability, key auth:
#   scripts/sonic-l2-deploy.sh 10.0.9.20 admin
#
set -euo pipefail

TARGET="${1:-}"
USER="${2:-admin}"
[[ -n "$TARGET" ]] || { echo "usage: $0 <mgmt-ip> [ssh-user]" >&2; exit 2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GOLDEN="${GOLDEN:-$REPO_ROOT/files/configs/golden/wedge100s-l2/config_db.json}"
EXPECT_HWSKU="${EXPECT_HWSKU:-Accton-WEDGE100S-32X}"
PASS="${SONIC_PASS:-}"

[[ -f "$GOLDEN" ]] || { echo "golden config not found: $GOLDEN" >&2; exit 2; }
python3 -c "import json,sys; json.load(open('$GOLDEN'))" \
  || { echo "golden config is not valid JSON: $GOLDEN" >&2; exit 2; }

# ---- transport helpers (sshpass + optional ProxyJump) ----------------------
SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=10)
[[ -n "${SSH_JUMP:-}" ]] && SSH_OPTS+=(-o "ProxyJump=$SSH_JUMP")
if [[ -n "$PASS" ]]; then
  command -v sshpass >/dev/null || { echo "SONIC_PASS set but sshpass not installed" >&2; exit 2; }
  SSH=(sshpass -p "$PASS" ssh "${SSH_OPTS[@]}")
  SCP=(sshpass -p "$PASS" scp "${SSH_OPTS[@]}")
else
  SSH=(ssh "${SSH_OPTS[@]}")
  SCP=(scp "${SSH_OPTS[@]}")
fi
rsh() { "${SSH[@]}" "$USER@$TARGET" "$@"; }

echo "==> target       : $USER@$TARGET ${SSH_JUMP:+(via $SSH_JUMP)}"
echo "==> golden config: $GOLDEN"

# ---- preflight: reachable, is SONiC, hwsku matches ------------------------
echo "==> preflight"
HWSKU="$(rsh 'show platform summary 2>/dev/null | awk -F: "/HwSKU/{gsub(/ /,\"\",\$2);print \$2}"')" \
  || { echo "cannot reach / not a SONiC device" >&2; exit 1; }
echo "    hwsku: ${HWSKU:-<unknown>}"
if [[ "$HWSKU" != "$EXPECT_HWSKU" && "${FORCE:-0}" != "1" ]]; then
  echo "    hwsku mismatch (expected $EXPECT_HWSKU). Re-run with FORCE=1 to override." >&2
  exit 1
fi

# ---- push golden config ----------------------------------------------------
echo "==> copying golden config to switch:/tmp/config_db.golden.json"
"${SCP[@]}" "$GOLDEN" "$USER@$TARGET:/tmp/config_db.golden.json"

# ---- apply on-box (backup -> reload -> disable bgp -> save -> verify) ------
# The remote work is streamed as one script so a single sudo session drives
# it. config reload is kicked detached so it survives any mgmt-plane blip,
# then we wait for swss/syncd to settle before verifying.
echo "==> applying (this includes a config reload; ~1-2 min)"
rsh "SUDO_PW='${PASS}' bash -s" <<'REMOTE'
set -euo pipefail
S(){ if [ -n "${SUDO_PW:-}" ]; then echo "$SUDO_PW" | sudo -S "$@"; else sudo "$@"; fi; }
ts="$(date -u +%Y%m%d-%H%M%S)"
echo "    backup current config -> /home/$(whoami)/config_db.backup.$ts.json"
S cp /etc/sonic/config_db.json "/home/$(whoami)/config_db.backup.$ts.json" || true
echo "    install golden config"
S cp /tmp/config_db.golden.json /etc/sonic/config_db.json
echo "    config reload (detached)"
S bash -c 'nohup config reload -y /etc/sonic/config_db.json >/tmp/sonic-l2-reload.log 2>&1 &'
echo "    waiting for swss/syncd to settle"
for i in $(seq 1 30); do
  sleep 5
  sw=$(systemctl is-active swss 2>/dev/null || true)
  sy=$(systemctl is-active syncd 2>/dev/null || true)
  if [ "$sw" = active ] && [ "$sy" = active ] && grep -q "Released lock" /tmp/sonic-l2-reload.log 2>/dev/null; then
    echo "    reload complete after $((i*5))s"; break
  fi
done
echo "    disable bgp feature"
S config feature state bgp disabled
sleep 5
echo "    save config"
S config save -y >/dev/null
REMOTE

# ---- verify ----------------------------------------------------------------
echo "==> verification"
rsh 'bash -s' <<'REMOTE'
p25=$(show interfaces status 2>/dev/null | awk '$3=="25G" && $9=="up"{c++} END{print c+0}')
p100=$(show interfaces status 2>/dev/null | awk '$3=="100G" && $9=="up"{c++} END{print c+0}')
members=$(show vlan brief 2>/dev/null | grep -cE "Ethernet")
bgp=$(show feature status 2>/dev/null | awk '/^bgp/{print $2}')
l3=$(show ip interfaces 2>/dev/null | grep -cE "Ethernet|Loopback")
echo "    25G ports admin-up : $p25"
echo "    100G ports admin-up: $p100"
echo "    Vlan members       : $members"
echo "    bgp feature        : $bgp"
echo "    L3 interface IPs   : $l3"
echo "    mgmt eth0          : $(ip -o -4 addr show eth0 | awk '{print $4}')"
REMOTE

echo "==> done. Backup of the pre-existing config is on the switch under /home/$USER/."
echo "    Note: 25G breakout subports have FEC unset (N/A). If a 25G link"
echo "    won't come up, set per-link FEC, e.g.: sudo config interface fec Ethernet16 rs"
