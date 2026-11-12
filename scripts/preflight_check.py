#!/usr/bin/env python3
"""
preflight_check.py — Pre-flight VLAN consistency check for staged push.

Usage: python3 scripts/preflight_check.py <site_dir> <critical_changes_json>

Reads critical_changes from a JSON file (written by apply_config.yml check-mode play).
For every critical link pair where BOTH the switch port AND the jumphost port are changing,
verifies that the desired VLAN in the jumphost's ifcfg file matches the desired VLAN in
the switch's config file. Exits 0 if consistent; exits 1 with error message(s) if not.
"""

import sys
import os
import json
import re

# Reuse parsers from build_topology
sys.path.insert(0, os.path.dirname(__file__))
from build_topology import parse_ifcfg_vlan, parse_cumulus_interfaces


def _get_cumulus_port_vlan(site_dir, device, port):
    """Return the intended native VLAN for a Cumulus port from its desired interfaces.txt."""
    iface_path = os.path.join(site_dir, device, 'config', 'interfaces.txt')
    if not os.path.exists(iface_path):
        return None
    ifaces = parse_cumulus_interfaces(open(iface_path).read())
    return ifaces.get(port, {}).get('native_vlan')


def _get_jumphost_port_vlan(site_dir, device, port):
    """Return the intended VLAN for a jumphost port from its desired ifcfg-<port> file."""
    ifcfg_path = os.path.join(site_dir, device, 'config', f'ifcfg-{port}')
    if not os.path.exists(ifcfg_path):
        return None
    return parse_ifcfg_vlan(open(ifcfg_path).read())


def check(site_dir, topology, critical_changes):
    """Return a list of error strings (empty = all consistent).

    Only checks pairs where both the switch side AND the jumphost side appear
    in critical_changes. If only one side is changing, no consistency check is
    needed for that pair.
    """
    errors = []

    # Index critical_changes by (device, port) for fast lookup
    changing = {(c['device'], c['port']) for c in critical_changes}

    for link in topology.get('critical_links', []):
        if link.get('peer_role') != 'jumphost':
            continue

        sw_device = link['local_device']
        sw_port = link['local_port']
        jh_device = link['peer_device']
        jh_port = link['peer_port']

        sw_changing = (sw_device, sw_port) in changing
        jh_changing = (jh_device, jh_port) in changing

        if not (sw_changing and jh_changing):
            continue  # Only one side changing — probe is the safety net

        sw_vlan = _get_cumulus_port_vlan(site_dir, sw_device, sw_port)
        jh_vlan = _get_jumphost_port_vlan(site_dir, jh_device, jh_port)

        if sw_vlan != jh_vlan:
            errors.append(
                f'VLAN MISMATCH: {jh_device}/{jh_port} targets VLAN {jh_vlan} '
                f'but {sw_device}/{sw_port} targets VLAN {sw_vlan}'
            )

    return errors


def main():
    if len(sys.argv) != 3:
        print(f'Usage: {sys.argv[0]} <site_dir> <critical_changes_json>',
              file=sys.stderr)
        sys.exit(1)

    site_dir = sys.argv[1]
    changes_path = sys.argv[2]

    import yaml
    topo_path = os.path.join(site_dir, 'topology.yml')
    topology = yaml.safe_load(open(topo_path).read())
    critical_changes = json.loads(open(changes_path).read())

    errors = check(site_dir, topology, critical_changes)

    if errors:
        print('\nVLAN MISMATCH — aborting before any changes are applied\n',
              file=sys.stderr)
        for e in errors:
            print(f'  {e}', file=sys.stderr)
        print('\nFix the config files so both sides agree, then re-run push.',
              file=sys.stderr)
        sys.exit(1)

    sys.exit(0)


if __name__ == '__main__':
    main()
