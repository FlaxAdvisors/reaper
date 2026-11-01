#!/usr/bin/env bash
#
# sonic-l2-deploy-n3248te.sh — turn a freshly installed community-SONiC Dell
# N3248TE-ON into a flat L2 mgmt switch using the golden config.
#
# Derived from reaper's scripts/sonic-l2-deploy.sh (wedge100s), with:
#   - FIX: the on-box steps run from a file copied to the switch, not from
#     an ssh heredoc on stdin. With `bash -s`, any command that reads stdin
#     eats the rest of the script; `redis-cli --scan` does exactly that
#     (verified 2026-10-26), so the chrony drop-in, bgp disable and
#     `config save` silently never ran in the wedge script.
#   - FIX: "reload complete" waits for the `config reload` process itself
#     to exit. Before, an already-populated VLAN plus a not-yet-stopped swss
#     declared success after 5 s, mid-reload.
#   - the two platform fixes for this model, applied on-box before the
#     reload and idempotent (each keeps a one-time .orig-* backup):
#       1. autoneg 1/0 -> on/off in the hwsku port_config.ini, so anything
#          that regenerates config from platform defaults (first boot,
#          `sonic-cfggen -H`, config-setup) passes the 202511 YANG check;
#          the golden's PORT table is normalised the same way on apply.
#       2. stable_size 0x5500000 (85 MiB) -> 0x8000000 (128 MiB) in the
#          hwsku .config.bcm, so syncd can initialise the ASIC.
#   - preflight accepts a first-boot HwSKU of "None" when the platform is
#     the N3248TE (the stock autoneg 1/0 config aborts first boot; the
#     golden reload replaces that config anyway).
#   - Dell-shaped verification (port counts by speed, VLAN members, MACs
#     learned) and sudo for `show interfaces status` (non-root tracebacks
#     reading the EEPROM).
#
# Usage:
#   scripts/sonic-l2-deploy-n3248te.sh <mgmt-ip> [ssh-user]
#
# Environment:
#   SONIC_PASS   password for <ssh-user> (also used for sudo on the switch).
#                Unset = ssh key/agent auth and passwordless sudo.
#   SSH_JUMP     optional ProxyJump (not needed when run on bang-gouda).
#   GOLDEN       golden config (default: files/configs/golden/n3248te-l2/config_db.json
#                in this repo).
#   FORCE=1      skip the platform/hwsku guard.
#
set -euo pipefail

TARGET="${1:-}"
USER="${2:-admin}"
[[ -n "$TARGET" ]] || { echo "usage: $0 <mgmt-ip> [ssh-user]" >&2; exit 2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GOLDEN="${GOLDEN:-$REPO_ROOT/files/configs/golden/n3248te-l2/config_db.json}"
EXPECT_PLATFORM="x86_64-dellemc_n3248te_c3338-r0"
EXPECT_HWSKU="DellEMC-N3248TE"
PASS="${SONIC_PASS:-}"

[[ -f "$GOLDEN" ]] || { echo "golden config not found: $GOLDEN" >&2; exit 2; }
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$GOLDEN" \
  || { echo "golden config is not valid JSON: $GOLDEN" >&2; exit 2; }

SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=error -o ConnectTimeout=10)
[[ -n "${SSH_JUMP:-}" ]] && SSH_OPTS+=(-o "ProxyJump=$SSH_JUMP")
if [[ -n "$PASS" ]]; then
  command -v sshpass >/dev/null || { echo "SONIC_PASS set but sshpass not installed" >&2; exit 2; }
  export SSHPASS="$PASS"     # sshpass -e reads it; the password never appears in argv
  SSH=(sshpass -e ssh "${SSH_OPTS[@]}")
  SCP=(sshpass -e scp "${SSH_OPTS[@]}")
else
  SSH=(ssh "${SSH_OPTS[@]}")
  SCP=(scp "${SSH_OPTS[@]}")
fi
rsh() { "${SSH[@]}" "$USER@$TARGET" "$@"; }

echo "==> target       : $USER@$TARGET ${SSH_JUMP:+(via $SSH_JUMP)}"
echo "==> golden config: $GOLDEN"

# ---- preflight --------------------------------------------------------------
echo "==> preflight"
SUMMARY="$(rsh 'show platform summary 2>/dev/null')" \
  || { echo "cannot reach / not a SONiC device" >&2; exit 1; }
PLATFORM="$(awk -F': *' '/^Platform/{print $2}' <<<"$SUMMARY")"
HWSKU="$(awk -F': *' '/^HwSKU/{print $2}' <<<"$SUMMARY")"
echo "    platform: ${PLATFORM:-<unknown>}  hwsku: ${HWSKU:-<unknown>}"
if [[ "${FORCE:-0}" != "1" ]]; then
  [[ "$PLATFORM" == "$EXPECT_PLATFORM" ]] \
    || { echo "    platform mismatch (expected $EXPECT_PLATFORM); FORCE=1 to override" >&2; exit 1; }
  if [[ "$HWSKU" != "$EXPECT_HWSKU" ]]; then
    [[ "$HWSKU" == "None" ]] \
      || { echo "    hwsku mismatch (expected $EXPECT_HWSKU); FORCE=1 to override" >&2; exit 1; }
    echo "    hwsku None = first boot aborted on the stock autoneg 1/0 config; the golden reload replaces it"
  fi
fi

echo "==> copying golden config to switch:/tmp/config_db.golden.json"
"${SCP[@]}" "$GOLDEN" "$USER@$TARGET:/tmp/config_db.golden.json"

# ---- apply on-box -------------------------------------------------------------
# The on-box steps go to the switch as files and run from there. Nothing
# below may be fed through ssh stdin: see the redis-cli note at the top.
TMPD="$(mktemp -d)"; trap 'rm -rf "$TMPD"' EXIT
cat >"$TMPD/apply.sh" <<'REMOTE'
set -euo pipefail
S(){ if [ -n "${SUDO_PW:-}" ]; then echo "$SUDO_PW" | sudo -S -p '' "$@"; else sudo "$@"; fi; }
ts="$(date -u +%Y%m%d-%H%M%S)"

# Platform scache fix. SDK 6.5.34 needs more than the platform's 85 MiB
# warm-boot scache; without this syncd fails "Scache out of space" in
# flowtracker init and swss never starts. syncd re-reads the file on the
# config reload below.
HWSKU_DIR=/usr/share/sonic/device/x86_64-dellemc_n3248te_c3338-r0/DellEMC-N3248TE

# Platform fix 1: port_config.ini says autoneg 1/0, the 202511 YANG model
# only takes on/off, and config-setup aborts the whole first-boot config
# on it (CONFIG_DB empty, HwSKU None, swss/syncd/pmon never start).
# autoneg is the last column; comment lines are left alone.
PCI="$HWSKU_DIR/port_config.ini"
if S awk '!/^#/ && NF && ($NF=="1" || $NF=="0"){f=1} END{exit !f}' "$PCI"; then
  echo "    platform fix: port_config.ini autoneg 1/0 -> on/off (backup $PCI.orig-autoneg)"
  S cp -n "$PCI" "$PCI.orig-autoneg"
  S awk '!/^#/ && NF{ if($NF=="1") $NF="on"; else if($NF=="0") $NF="off" } {print}' "$PCI" > /tmp/port_config.ini.new
  S cp /tmp/port_config.ini.new "$PCI"
  rm -f /tmp/port_config.ini.new
else
  echo "    platform fix: port_config.ini autoneg already on/off"
fi

# Platform fix 2: SDK 6.5.34 needs more than the platform's 85 MiB scache.
BCM=$HWSKU_DIR/hx5-n3248te-48x1G+4x10G.config.bcm
if S grep -q '^stable_size=0x5500000$' "$BCM"; then
  echo "    platform fix: stable_size 0x5500000 -> 0x8000000 (backup $BCM.orig-stable_size)"
  S cp -n "$BCM" "$BCM.orig-stable_size"
  S sed -i 's/^stable_size=0x5500000$/stable_size=0x8000000/' "$BCM"
else
  echo "    platform fix: stable_size already $(S grep '^stable_size=' "$BCM" | cut -d= -f2)"
fi

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
fixed=0
for v in gold.get('PORT',{}).values():
    a=v.get('autoneg')
    if a in ('1','0'):
        v['autoneg']={'1':'on','0':'off'}[a]; fixed+=1
if fixed: print('    normalised autoneg 1/0 -> on/off on %d golden PORT entries' % fixed)
json.dump(gold,open('/etc/sonic/config_db.json','w'),indent=2)
print('    spliced hwsku=%s mac=%s' % (lh.get('hwsku','?'), lh.get('mac','?')))
"
# </dev/null is the fix: a backgrounded child otherwise inherits this
# script's stdin (the ssh heredoc) and swallows every line after it.
echo "    config reload (detached, -f)"
S rm -f /tmp/sonic-l2-reload.log
pid="$(S bash -c 'nohup config reload -y -f /etc/sonic/config_db.json </dev/null >/tmp/sonic-l2-reload.log 2>&1 & echo $!')"
echo "    waiting for config reload (pid $pid) to finish"
for i in $(seq 1 60); do
  [ -d "/proc/$pid" ] || break
  sleep 5
done
[ -d "/proc/$pid" ] && echo "    !! config reload still running after 300 s" >&2
echo "    waiting for swss/syncd to settle"
ok=0
for i in $(seq 1 48); do
  sleep 5
  if grep -qE "Could not get the HWSKU|Traceback \(most recent call last\)" /tmp/sonic-l2-reload.log 2>/dev/null; then
    echo "    !! config reload aborted:" >&2
    tail -5 /tmp/sonic-l2-reload.log | sed 's/^/       /' >&2
    break
  fi
  [ "$(systemctl is-active swss 2>/dev/null || true)" = active ] || continue
  [ "$(systemctl is-active syncd 2>/dev/null || true)" = active ] || continue
  # VLAN in CONFIG_DB only proves the config loaded; PortInitDone in
  # APPL_DB (cleared by swss on stop) proves the ports were created.
  [ "$(redis-cli -n 0 exists PORT_TABLE:PortInitDone </dev/null 2>/dev/null)" = 1 ] || continue
  if [ "$(redis-cli -n 4 --scan --pattern 'VLAN|Vlan1000' </dev/null 2>/dev/null | wc -l)" -ge 1 ]; then
    ok=1; echo "    reload complete; dataplane up after a further $((i*5))s"; break
  fi
done
if [ "$ok" != 1 ]; then
  echo "    !! golden config did NOT apply; skipping 'config save'" >&2
  echo "    !! restoring pre-deploy backup to /etc/sonic/config_db.json" >&2
  S cp "/home/$(whoami)/config_db.backup.$ts.json" /etc/sonic/config_db.json
  echo "    !! swss=$(systemctl is-active swss) syncd=$(systemctl is-active syncd)" >&2
  exit 1
fi

# No RTC sync on first boot + stock chrony slews only, so a clock that is
# far off never converges. Drop-ins in conf.d survive template regeneration.
echo "    install chrony makestep drop-in"
printf '%s\n' '# managed by n3248te-l2-deploy.sh' 'makestep 1.0 3' > /tmp/makestep.conf
S cp /tmp/makestep.conf /etc/chrony/conf.d/makestep.conf
S chmod 644 /etc/chrony/conf.d/makestep.conf
rm -f /tmp/makestep.conf
S systemctl restart chrony || echo "    !! chrony restart failed" >&2

# The golden carries FEATURE bgp state=disabled; this is a belt-and-braces
# check in case hostcfgd rewrote the table after the reload.
for i in $(seq 1 24); do
  [ -n "$(redis-cli -n 4 hget 'FEATURE|bgp' state </dev/null 2>/dev/null)" ] && break
  sleep 5
done
for i in $(seq 1 6); do
  [ "$(redis-cli -n 4 hget 'FEATURE|bgp' state </dev/null 2>/dev/null)" = disabled ] && break
  S config feature state bgp disabled || true
  sleep 5
done
echo "    bgp feature: $(redis-cli -n 4 hget 'FEATURE|bgp' state </dev/null 2>/dev/null)"
echo "    save config"
S config save -y >/dev/null
echo "    REMOTE-DONE"
REMOTE

cat >"$TMPD/verify.sh" <<'REMOTE'
S(){ if [ -n "${SUDO_PW:-}" ]; then echo "$SUDO_PW" | sudo -S -p '' "$@"; else sudo "$@"; fi; }
S python3 -c "
import json
d=json.load(open('/etc/sonic/config_db.json'))
print('    saved PORT entries  : %d' % len(d.get('PORT',{})))
print('    saved VLAN members  : %d %s' % (len(d.get('VLAN_MEMBER',{})), list(d.get('VLAN',{}))))
print('    saved BREAKOUT_CFG  : %s' % ('present (unexpected)' if d.get('BREAKOUT_CFG') else 'none'))
print('    saved BGP/L3 ents   : %d neighbors, %d interface IPs' % (len(d.get('BGP_NEIGHBOR',{})), len(d.get('INTERFACE',{}))))
print('    saved bgp feature   : %s' % d.get('FEATURE',{}).get('bgp',{}).get('state'))
"
st="$(S show interfaces status 2>/dev/null)"
echo "    ports admin-up      : $(awk '$9=="up"' <<<"$st" | wc -l) (1G $(awk '$3=="1G" && $9=="up"' <<<"$st" | wc -l), 10G $(awk '$3=="10G" && $9=="up"' <<<"$st" | wc -l), 100G $(awk '$3=="100G" && $9=="up"' <<<"$st" | wc -l))"
echo "    ports oper-up       : $(awk '$8=="up"{printf "%s ", $1}' <<<"$st")"
echo "    bgp container       : $(docker ps --format '{{.Names}}' | grep -c '^bgp$') running"
echo "    chrony makestep     : $(cat /etc/chrony/conf.d/makestep.conf 2>/dev/null | tail -1)"
echo "    swss / syncd        : $(systemctl is-active swss) / $(systemctl is-active syncd)"
echo "    mgmt eth0           : $(ip -o -4 addr show eth0 | awk '{print $4}')"
sleep 30
echo "    MACs learned (30 s) : $(S show mac 2>/dev/null | awk '/Total number of entries/{print $NF}')"
REMOTE

"${SCP[@]}" "$TMPD/apply.sh" "$TMPD/verify.sh" "$USER@$TARGET:/tmp/"
echo "==> applying (includes a config reload; ~1-2 min)"
rsh "SUDO_PW='${PASS}' bash /tmp/apply.sh </dev/null" \
  || { echo "==> apply FAILED (see above)" >&2; exit 1; }
echo "==> verification"
rsh "SUDO_PW='${PASS}' bash /tmp/verify.sh </dev/null; rm -f /tmp/apply.sh /tmp/verify.sh"
echo "==> done. Pre-deploy config backup is on the switch under /home/$USER/."
