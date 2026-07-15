#!/bin/bash
# Static IPv6 ULA address assignment for bang lab-data + mgmt interfaces.
#
# dnsmasq's stateful DHCPv6 server (see roles/apply_bang_services/templates/
# dnsmasq.conf.j2) selects which dhcp-range to serve a SOLICIT from by
# matching the incoming-iface against an iface that already carries an
# address in the matching /64. Without these statically-assigned ULAs,
# dnsmasq has no way to associate an iface like eth1.21 with the
# fd00:21::/64 dhcp-range — and logs "no address range available for
# DHCPv6 request via eth1.21" once per SOLICIT retransmit.
#
# Prefix scheme matches the dnsmasq template:
#   mgmt vlan (eth0)         → fd00:88::<suffix>/64
#   lab-data vlans (vid N)   → fd00:<N>::<suffix>/64 on <parent>.<vid>
#
# <suffix> mirrors `bang_host_suffix` from the site inventory — bang-gouda=1,
# bang-edam=2 (and likewise for other sites' primary/secondary bangs).
# Compatible-but-noop on bangs at sites that haven't cut over to v6 yet
# (early `exit 0` on an unknown hostname).
#
# Run by bang-v6-addrs.service at boot; idempotent under `ip addr replace`
# so manual re-runs are safe.
#
# TODO: wire into apply_bang_services role so future bangs get it without
# a manual scp. Deployed by hand 2026-05-13 alongside the stateful DHCPv6
# template change.

set -eu

case "$(hostname -s)" in
  bang-gouda)  SUFFIX=1 ;;
  bang-edam)   SUFFIX=2 ;;
  *)
    echo "$(basename "$0"): v6 ULA assignment not configured for $(hostname -s); skipping" >&2
    exit 0
    ;;
esac

# Mgmt vlan — bang_mgmt_iface is currently eth0 everywhere. Sites that
# adopt a different mgmt iface will need this mapped (or sourced from a
# config file).
ip -6 addr replace "fd00:88::${SUFFIX}/64" dev eth0

# Per-vlan: read /etc/flax/vlans.json (rendered by apply_lease_agent from
# the inventory `vlans:` array) for the parent-iface + vid pairs. Skipping
# the entire loop is OK if vlans.json doesn't exist yet (early boot before
# apply_lease_agent has run).
if [ -r /etc/flax/vlans.json ]; then
    python3 - "$SUFFIX" <<'PY'
import json, subprocess, sys
suffix = sys.argv[1]
for v in json.load(open('/etc/flax/vlans.json')):
    iface = f"{v['parent']}.{v['vid']}"
    addr = f"fd00:{v['vid']}::{suffix}/64"
    subprocess.run(['ip', '-6', 'addr', 'replace', addr, 'dev', iface], check=True)
PY
fi
