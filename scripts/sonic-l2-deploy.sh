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
# VLAN_MEMBER / NTP_SERVER) — no MAC, hostname or serial — so it is
# generic across units of the SAME hwsku. On `config reload` the device merges its own
# /etc/sonic/init_cfg.json, which restores the platform defaults golden
# omits (MGMT_PORT, FEATURE, NTP, CRM, KDUMP, FLEX_COUNTER_TABLE, syslog,
# auto-techsupport, SYSTEM_DEFAULTS).
#
# init_cfg.json does NOT carry DEVICE_METADATA.localhost.hwsku/mac, and
# `config reload` reads hwsku out of the file being loaded *before* that
# merge happens — a golden config with no DEVICE_METADATA aborts with
# "Could not get the HWSKU from config file". So this script splices the
# device's own live DEVICE_METADATA into golden on-box, which keeps the
# repo copy generic while giving the reload the per-unit mac/hwsku it
# needs. Tables deliberately left to die with the reload: BGP_NEIGHBOR,
# INTERFACE, LOOPBACK_INTERFACE (that is the point), plus LOGGER/SNMP/
# VERSIONS which are cosmetic on an L2 lab switch.
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
echo "    install golden config (splicing live DEVICE_METADATA into it)"
S python3 -c "
import json,sys
cur=json.load(open('/etc/sonic/config_db.json'))
gold=json.load(open('/tmp/config_db.golden.json'))
dm=cur.get('DEVICE_METADATA') or {}
lh=dm.get('localhost') or {}
if not lh.get('hwsku'):
    sys.stderr.write('DEVICE_METADATA.localhost.hwsku missing from live config; refusing\n')
    sys.exit(1)
gold['DEVICE_METADATA']=dm
json.dump(gold,open('/etc/sonic/config_db.json','w'),indent=2)
print('    spliced hwsku=%s mac=%s' % (lh.get('hwsku','?'), lh.get('mac','?')))
"
# -f skips the "SwSS container is not ready" precondition. Without it a
# switch whose dataplane is already down (e.g. a previous failed reload
# left swss stopped) can never be reloaded back into a good state — the
# check refuses to run precisely when you most need the reload.
echo "    config reload (detached, -f)"
S bash -c 'nohup config reload -y -f /etc/sonic/config_db.json >/tmp/sonic-l2-reload.log 2>&1 &'
# NB: do NOT use "Released lock" as the success signal — config reload
# prints it on the failure path too (it is lock teardown, not success).
# The only trustworthy signal is the intent actually landing in CONFIG_DB.
echo "    waiting for swss/syncd to settle"
ok=0
for i in $(seq 1 36); do
  sleep 5
  if grep -qE "Could not get the HWSKU|Traceback \(most recent call last\)" /tmp/sonic-l2-reload.log 2>/dev/null; then
    echo "    !! config reload aborted:" >&2
    tail -5 /tmp/sonic-l2-reload.log | sed 's/^/       /' >&2
    break
  fi
  sw=$(systemctl is-active swss 2>/dev/null || true)
  sy=$(systemctl is-active syncd 2>/dev/null || true)
  [ "$sw" = active ] && [ "$sy" = active ] || continue
  if [ "$(redis-cli -n 4 --scan --pattern 'VLAN|Vlan1000' 2>/dev/null | wc -l)" -ge 1 ]; then
    ok=1; echo "    reload complete after $((i*5))s"; break
  fi
done

if [ "$ok" != 1 ]; then
  # Critical: 'config save' here would serialise the *stock* running config
  # from redis back over the golden file we just installed, hiding the
  # failure and leaving no trace of what was attempted.
  echo "    !! golden config did NOT apply — skipping 'config save'" >&2
  echo "    !! restoring pre-deploy backup to /etc/sonic/config_db.json" >&2
  S cp "/home/$(whoami)/config_db.backup.$ts.json" /etc/sonic/config_db.json
  echo "    !! swss=$(systemctl is-active swss) syncd=$(systemctl is-active syncd)" >&2
  echo "    !! if the dataplane is down, recover with:" >&2
  echo "    !!   sudo config reload -y /etc/sonic/config_db.json" >&2
  exit 1
fi

# NTP_SERVER in the golden config gives chrony something to talk to, but
# that alone is not enough: the wedge has no RTC (timedatectl reports
# "RTC time: n/a"), so a cold boot starts from an arbitrary clock, and
# stock chrony.conf ships `makestep` commented out — chrony then slews
# only and will never close a multi-month gap, leaving a switch that is
# "NTP active" with a clock a year out. chrony.conf itself is templated
# by SONiC (/usr/share/sonic/templates/chrony.conf.j2) so editing it is
# pointless, but it carries `confdir /etc/chrony/conf.d` and drop-ins
# there survive both reboot and template regeneration.
#
# NB: write via /tmp then cp. Piping content into `sudo -S tee` makes the
# password stdin for BOTH sudo and tee, and tee writes the password into
# the file instead of the config.
echo "    install chrony makestep drop-in"
printf '%s\n' '# managed by sonic-l2-deploy.sh — see script for rationale' \
              'makestep 1.0 3' > /tmp/makestep.conf
S cp /tmp/makestep.conf /etc/chrony/conf.d/makestep.conf
S chmod 644 /etc/chrony/conf.d/makestep.conf
rm -f /tmp/makestep.conf
S systemctl restart chrony || echo "    !! chrony restart failed — check 'systemctl status chrony'" >&2

# hostcfgd repopulates the FEATURE table from init_cfg.json for a while
# after swss comes up. Disabling bgp before it has finished means our
# write is silently overwritten (and a 'config save' in that window
# serialises a FEATURE table that is not there yet). Wait for the table,
# then disable, then confirm it actually took.
echo "    waiting for FEATURE table to settle"
for i in $(seq 1 24); do
  sleep 5
  [ -n "$(redis-cli -n 4 hget 'FEATURE|bgp' state 2>/dev/null)" ] && break
done
echo "    disable bgp feature"
for i in $(seq 1 6); do
  S config feature state bgp disabled || true
  sleep 5
  [ "$(redis-cli -n 4 hget 'FEATURE|bgp' state 2>/dev/null)" = disabled ] && break
done
bgpstate="$(redis-cli -n 4 hget 'FEATURE|bgp' state 2>/dev/null)"
[ "$bgpstate" = disabled ] \
  && echo "    bgp feature disabled" \
  || echo "    !! bgp feature is '$bgpstate' (wanted disabled) — harmless with 0 neighbours, but check hostcfgd" >&2
echo "    save config"
S config save -y >/dev/null
REMOTE

# ---- verify ----------------------------------------------------------------
echo "==> verification"
# Config-level checks first: link state is meaningless when nothing is
# cabled, so a deploy onto an empty switch must still verify as good.
rsh 'bash -s' <<'REMOTE'
python3 -c "
import json
d=json.load(open('/etc/sonic/config_db.json'))
b=d.get('BREAKOUT_CFG',{})
print('    PORT entries       : %d' % len(d.get('PORT',{})))
print('    4x25G breakouts    : %d' % sum(1 for v in b.values() if '25G' in v.get('brkout_mode','')))
print('    VLANs              : %s' % list(d.get('VLAN',{})))
print('    VLAN members       : %d' % len(d.get('VLAN_MEMBER',{})))
print('    BGP neighbors      : %d' % len(d.get('BGP_NEIGHBOR',{})))
print('    L3 INTERFACE ents  : %d' % len(d.get('INTERFACE',{})))
"
echo "    bgp feature        : $(show feature status 2>/dev/null | awk '/^bgp/{print $2}')"
echo "    ntp sources        : $(chronyc sources 2>/dev/null | grep -c '^\^')  synced: $(timedatectl 2>/dev/null | awk -F': ' '/synchronized/{print $2}')"
echo "    swss / syncd       : $(systemctl is-active swss) / $(systemctl is-active syncd)"
echo "    mgmt eth0          : $(ip -o -4 addr show eth0 | awk '{print $4}')"
# $9 is the Admin column, $8 is Oper. Admin-up proves the subports were
# created; Oper-up only happens once something is cabled into them.
adm25=$(show interfaces status 2>/dev/null | awk '$3=="25G" && $9=="up"{c++} END{print c+0}')
oper25=$(show interfaces status 2>/dev/null | awk '$3=="25G" && $8=="up"{c++} END{print c+0}')
echo "    25G subports       : $adm25 admin-up, $oper25 oper-up (oper 0 is expected when nothing is cabled)"
REMOTE

echo "==> done. Backup of the pre-existing config is on the switch under /home/$USER/."
echo "    Note: 25G breakout subports have FEC unset (N/A). If a 25G link"
echo "    won't come up, set per-link FEC, e.g.: sudo config interface fec Ethernet16 rs"
