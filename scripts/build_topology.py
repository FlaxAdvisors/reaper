#!/usr/bin/env python3
"""
build_topology.py — Derive topology.yml from LLDP + config data collected by reaper fetch.

Usage: python3 scripts/build_topology.py <site_config_dir>

<site_config_dir> is the path to files/configs/<site>/ — the directory containing
one subdirectory per device, each with config/ and show/ subdirectories.

Writes topology.yml into <site_config_dir>.
"""

import sys
import os
import re
import yaml


# ── LLDP parsers ─────────────────────────────────────────────────────────────

def parse_cumulus_lldp(text):
    """Parse 'net show lldp' tabular output. Returns list of dicts:
       {local_port, remote_host, remote_port (None if absent)}.
    """
    peers = []
    lines = text.strip().splitlines()
    # Skip header and separator lines (first two non-empty lines)
    data_lines = [l for l in lines if l and not l.startswith('-')]
    for line in data_lines[1:]:  # skip header row
        parts = line.split()
        if len(parts) < 4:
            continue
        local_port = parts[0]
        # `net show lldp` columns: LocalPort Speed Mode RemoteHost RemotePort
        remote_host = parts[3]
        remote_port = parts[4] if len(parts) >= 5 else None
        peers.append({
            'local_port': local_port,
            'remote_host': remote_host,
            'remote_port': remote_port,
        })
    return peers


def parse_cisco_lldp(text):
    """Parse Cisco IOS 'show lldp neighbors' tabular output. Returns list of dicts:
       {local_port, remote_host, remote_port (None if not parseable as a port name)}.

    Layout (after the 'Device ID' header):
       <Device ID>  <Local Intf>  <Hold-time>  <Capability>  <Port ID>
    Port ID may be an interface name (peer is a switch — Arista advertises it)
    or a MAC like b8ce.f63e.56c2 (peer is a host with default lldpd config).
    For MAC-only port IDs we return None — the lldpctl side fills that in.
    """
    peers = []
    in_table = False
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.startswith('Device ID'):
            in_table = True
            continue
        if line.startswith('Total entries'):
            break
        if not in_table:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        remote_host = parts[0]
        local_port = parts[1]
        port_id_raw = parts[-1]
        if re.match(r'^[a-f0-9]{4}\.[a-f0-9]{4}\.[a-f0-9]{4}$', port_id_raw):
            remote_port = None  # MAC-only — peer is a host using lldpd default
        else:
            remote_port = port_id_raw
        peers.append({
            'local_port': local_port,
            'remote_host': remote_host,
            'remote_port': remote_port,
        })
    return peers


def parse_jumphost_lldpctl(text):
    """Parse 'lldpctl -f keyvalue' output. Returns dict keyed by local port name:
       {local_port: {remote_host, remote_port}}.
    """
    result = {}
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or '=' not in line:
            continue
        key, _, value = line.partition('=')
        # key format: lldp.<local_port>.<field>
        parts = key.split('.')
        if len(parts) < 3 or parts[0] != 'lldp':
            continue
        local_port = parts[1]
        field = '.'.join(parts[2:])
        if local_port not in result:
            result[local_port] = {}
        if field == 'chassis.name':
            result[local_port]['remote_host'] = value
        elif field == 'port.ifname':
            result[local_port]['remote_port'] = value
    return result


# ── Config file parsers ───────────────────────────────────────────────────────

def parse_cumulus_interfaces(text):
    """Parse /etc/network/interfaces. Returns dict keyed by interface name:
       {native_vlan (int or None), tagged_vlans (list of int)}.
    """
    ifaces = {}
    current = None
    bridge_pvid = None

    for line in text.splitlines():
        stripped = line.strip()
        m = re.match(r'^(?:auto|iface)\s+(\S+)', stripped)
        if m:
            iface_name = m.group(1)
            if iface_name not in ifaces:
                ifaces[iface_name] = {'native_vlan': None, 'tagged_vlans': []}
            current = iface_name
            continue

        if current is None:
            continue

        m_pvid = re.match(r'bridge-pvid\s+(\d+)', stripped)
        if m_pvid:
            vlan = int(m_pvid.group(1))
            ifaces[current]['native_vlan'] = vlan
            if current == 'bridge':
                bridge_pvid = vlan
            continue

        m_vids = re.match(r'bridge-vids\s+(.+)', stripped)
        if m_vids:
            ifaces[current]['tagged_vlans'] = [int(v) for v in m_vids.group(1).split()]
            continue

    return ifaces


def parse_cisco_interfaces(text):
    """Parse Cisco IOS running-config interface stanzas. Returns dict keyed by
       short interface name (Gi/Te/Fa): {native_vlan (int or None), tagged_vlans (list of int)}.

    'switchport access vlan N'        → native_vlan = N
    'switchport trunk native vlan N'  → native_vlan = N
    'switchport trunk allowed vlan …' → tagged_vlans = expanded list
    """
    ifaces = {}
    current = None
    for line in text.splitlines():
        # Stanza header — both 'interface X' and global lines de-indent to col 0
        m = re.match(r'^interface\s+(\S+)', line)
        if m:
            current = _short_ifname(m.group(1))
            ifaces.setdefault(current, {'native_vlan': None, 'tagged_vlans': []})
            continue
        # Lines outside an interface stanza reset context
        if current is None:
            continue
        if line and not line.startswith(' '):
            current = None
            continue
        stripped = line.strip()
        m = re.match(r'switchport access vlan (\d+)', stripped)
        if m:
            ifaces[current]['native_vlan'] = int(m.group(1))
            continue
        m = re.match(r'switchport trunk native vlan (\d+)', stripped)
        if m:
            ifaces[current]['native_vlan'] = int(m.group(1))
            continue
        m = re.match(r'switchport trunk allowed vlan (.+)', stripped)
        if m:
            ifaces[current]['tagged_vlans'] = _expand_vlan_list(m.group(1))
    return ifaces


def parse_cisco_mgmt_vlan(text):
    """Find the lowest-numbered VlanN SVI that has an 'ip address' line.
    Returns int or None.
    """
    candidates = []
    in_vlan = None
    for line in text.splitlines():
        m = re.match(r'^interface\s+Vlan(\d+)', line.strip())
        if m:
            in_vlan = int(m.group(1))
            continue
        if in_vlan is None:
            continue
        if line and not line.startswith(' '):
            in_vlan = None
            continue
        if re.match(r'^\s*ip address\s+\d+\.\d+\.\d+\.\d+', line):
            candidates.append(in_vlan)
            in_vlan = None
    return min(candidates) if candidates else None


def _short_ifname(name):
    """Normalize Cisco interface long-form names to short form (matches LLDP)."""
    name = re.sub(r'^GigabitEthernet', 'Gi', name)
    name = re.sub(r'^TenGigabitEthernet', 'Te', name)
    name = re.sub(r'^FastEthernet', 'Fa', name)
    return name


def _expand_vlan_list(s):
    """Expand '2,4,11-18' → [2, 4, 11, 12, 13, 14, 15, 16, 17, 18]."""
    result = []
    for part in s.split(','):
        part = part.strip()
        if '-' in part:
            a, b = part.split('-', 1)
            if a.isdigit() and b.isdigit():
                result.extend(range(int(a), int(b) + 1))
        elif part.isdigit():
            result.append(int(part))
    return result


def parse_arista_management_vlan(text):
    """Parse Arista running-config. Returns native VLAN for Management1 (int or None).
    Management1 is the OOB management port — we look for 'switchport access vlan N'
    or 'switchport trunk native vlan N' within the Management1 stanza.
    """
    in_mgmt = False
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r'^interface\s+Management1', stripped):
            in_mgmt = True
            continue
        if in_mgmt:
            if re.match(r'^interface\s', stripped) or stripped == '!':
                break
            m = re.match(r'switchport access vlan (\d+)', stripped)
            if m:
                return int(m.group(1))
            m = re.match(r'switchport trunk native vlan (\d+)', stripped)
            if m:
                return int(m.group(1))
    return None  # Management1 exists but no VLAN config — untagged/default


def parse_ifcfg_vlan(text):
    """Parse openSUSE ifcfg-* file. Returns VLAN ID (int) or None."""
    for line in text.splitlines():
        stripped = line.strip()
        m = re.match(r"VLAN_ID=['\"]?(\d+)['\"]?", stripped)
        if m:
            return int(m.group(1))
    return None


# ── Topology identification ───────────────────────────────────────────────────

def identify_primary_turtle(site_dir):
    """Find the primary OOB management switch by name convention.
    The reaper convention reserves the `turtle-*` prefix for the OOB management
    switch regardless of NOS. Sites currently have at most one turtle each.
    Returns the inventory hostname (directory name) or None.
    """
    turtles = sorted(
        entry.name for entry in os.scandir(site_dir)
        if entry.is_dir() and re.match(r'^turtle-', entry.name)
    )
    if not turtles:
        return None
    if len(turtles) > 1:
        print(f'WARNING: multiple turtle-* devices in {site_dir}: '
              f'{turtles}. Using {turtles[0]}.', file=sys.stderr)
    return turtles[0]


def detect_nos(device_dir):
    """Inspect a device directory's collected files to determine the NOS.
    Returns 'cumulus', 'cisco', 'arista', or None.
    """
    cfg = os.path.join(device_dir, 'config')
    if os.path.exists(os.path.join(cfg, 'interfaces.txt')):
        return 'cumulus'
    rc = os.path.join(cfg, 'running-config.txt')
    if not os.path.exists(rc):
        return None
    with open(rc) as f:
        head = f.read(2048)
    if 'Arista' in head or 'EOS-' in head or 'WEDGE' in head:
        return 'arista'
    if re.search(r'(?:Cisco IOS|version 1[25]\.|^!\s*$)', head, re.MULTILINE):
        # 'version 12.x' or 'version 15.x' is the IOS marker; the bare-comment
        # block ('!\n!\n!') is a weaker fallback.
        return 'cisco'
    return None


def _peer_role(remote_host):
    if re.match(r'^bang', remote_host):
        return 'jumphost'
    if re.match(r'^(rabbit|hare|lapin)-', remote_host):
        return 'managed_switch'
    return None


# ── Main build function ───────────────────────────────────────────────────────

def build(site_dir):
    """Read collected data from site_dir, write topology.yml."""
    site_name = os.path.basename(site_dir.rstrip('/'))

    primary_turtle = identify_primary_turtle(site_dir)
    if primary_turtle is None:
        print(f'WARNING: No turtle-* device in {site_dir} — skipping topology build.',
              file=sys.stderr)
        return

    turtle_dir = os.path.join(site_dir, primary_turtle)
    turtle_nos = detect_nos(turtle_dir)
    if turtle_nos is None:
        print(f'WARNING: Could not detect NOS for {primary_turtle} — skipping.',
              file=sys.stderr)
        return

    # Parse the turtle's LLDP table and per-port VLAN info (NOS-specific).
    if turtle_nos == 'cumulus':
        lldp_path = os.path.join(turtle_dir, 'show', 'show-lldp.txt')
        turtle_peers = parse_cumulus_lldp(open(lldp_path).read()) if os.path.exists(lldp_path) else []
        iface_path = os.path.join(turtle_dir, 'config', 'interfaces.txt')
        ifaces = parse_cumulus_interfaces(open(iface_path).read()) if os.path.exists(iface_path) else {}
        # Cumulus mgmt VLAN comes from the bridge's bridge-pvid.
        mgmt_vlan = ifaces.get('bridge', {}).get('native_vlan', 1) or 1
    elif turtle_nos == 'cisco':
        lldp_path = os.path.join(turtle_dir, 'show', 'show-lldp.txt')
        turtle_peers = parse_cisco_lldp(open(lldp_path).read()) if os.path.exists(lldp_path) else []
        rc_path = os.path.join(turtle_dir, 'config', 'running-config.txt')
        rc_text = open(rc_path).read() if os.path.exists(rc_path) else ''
        ifaces = parse_cisco_interfaces(rc_text)
        mgmt_vlan = parse_cisco_mgmt_vlan(rc_text) or 1
    else:
        print(f'WARNING: NOS {turtle_nos!r} not supported for primary turtle.',
              file=sys.stderr)
        return

    # Parse each jumphost's lldpctl to supplement peer_port discovery
    jumphost_lldp = {}  # {host_name: {local_port: {remote_host, remote_port}}}
    jumphost_ifcfg = {}  # {host_name: {port_name: vlan_id}}
    for entry in os.scandir(site_dir):
        if not re.match(r'^bang', entry.name):
            continue
        lldpctl_path = os.path.join(entry.path, 'show', 'lldpctl.txt')
        if os.path.exists(lldpctl_path):
            jumphost_lldp[entry.name] = parse_jumphost_lldpctl(
                open(lldpctl_path).read()
            )
        # Collect VLAN info from each ifcfg-* in config/
        ifcfg_dir = os.path.join(entry.path, 'config')
        jumphost_ifcfg[entry.name] = {}
        if os.path.isdir(ifcfg_dir):
            for f in os.scandir(ifcfg_dir):
                if f.name.startswith('ifcfg-') and not f.name.endswith('.bak'):
                    port_name = f.name[len('ifcfg-'):]
                    vlan = parse_ifcfg_vlan(open(f.path).read())
                    jumphost_ifcfg[entry.name][port_name] = vlan

    # Build a reverse map: (switch_port) → jumphost_name from jumphost LLDP
    # key: (remote_host=primary_turtle, remote_port=swpN) → (jumphost_name, local_port)
    jh_by_switch_port = {}
    for jh_name, port_map in jumphost_lldp.items():
        for local_port, info in port_map.items():
            if info.get('remote_host') == primary_turtle:
                swp = info.get('remote_port')
                if swp:
                    jh_by_switch_port[swp] = (jh_name, local_port)

    # Build critical_links list
    critical_links = []
    warnings = []

    # Identify which turtle port is the OOB loopback (swp→eth0 on itself)
    loopback_port = next(
        (p['local_port'] for p in turtle_peers
         if p['remote_host'] == primary_turtle and p['remote_port'] == 'eth0'),
        None
    )

    for peer in turtle_peers:
        local_port = peer['local_port']
        remote_host = peer['remote_host']
        remote_port = peer['remote_port']

        # Skip eth0 (management) and the loopback target side
        if local_port == 'eth0':
            continue

        # OOB loopback
        if local_port == loopback_port:
            port_iface = ifaces.get(local_port, {})
            critical_links.append({
                'local_device': primary_turtle,
                'local_port': local_port,
                'local_native_vlan': port_iface.get('native_vlan'),
                'local_tagged_vlans': port_iface.get('tagged_vlans', []),
                'peer_device': primary_turtle,
                'peer_role': 'oob_loopback',
                'peer_port': 'eth0',
                'peer_native_vlan': None,
                'peer_tagged_vlans': [],
            })
            continue

        role = _peer_role(remote_host)
        if role is None:
            continue

        port_iface = ifaces.get(local_port, {})
        local_native = port_iface.get('native_vlan')
        local_tagged = port_iface.get('tagged_vlans', [])

        # Resolve peer_port
        peer_port = remote_port

        if role == 'jumphost':
            # Supplement from jumphost LLDP if switch LLDP didn't give us the port
            if peer_port is None:
                info = jh_by_switch_port.get(local_port)
                if info:
                    remote_host, peer_port = info[0], info[1]

            # Resolve the peer's VLAN from its ifcfg file
            jh_name = None
            for jh, port_map in jumphost_lldp.items():
                for lp, inf in port_map.items():
                    if inf.get('remote_host') == primary_turtle and inf.get('remote_port') == local_port:
                        jh_name = jh
                        if peer_port is None:
                            peer_port = lp
                        break
            if jh_name is None:
                # Try matching by bang-* pattern on remote_host
                jh_name = remote_host if re.match(r'^bang', remote_host) else None

            peer_native = None
            if jh_name and peer_port:
                peer_native = jumphost_ifcfg.get(jh_name, {}).get(peer_port)

            if peer_port is None:
                warnings.append(
                    f'WARNING: Cannot resolve peer_port for {primary_turtle}/{local_port} '
                    f'→ {remote_host}. Set peer_port: unknown in topology.yml.'
                )

            critical_links.append({
                'local_device': primary_turtle,
                'local_port': local_port,
                'local_native_vlan': local_native,
                'local_tagged_vlans': local_tagged,
                'peer_device': jh_name or remote_host,
                'peer_role': 'jumphost',
                'peer_port': peer_port or 'unknown',
                'peer_native_vlan': peer_native,
                'peer_tagged_vlans': [],
            })

        elif role == 'managed_switch':
            # Peer port defaults to Management1 for Arista (standard OOB port)
            if peer_port is None:
                rc_path = os.path.join(site_dir, remote_host, 'config', 'running-config.txt')
                if os.path.exists(rc_path):
                    peer_port = 'Management1'
                else:
                    peer_port = 'unknown'
                    warnings.append(
                        f'WARNING: Cannot resolve peer_port for {primary_turtle}/{local_port} '
                        f'→ {remote_host}. running-config.txt not found.'
                    )

            # Peer native VLAN from Arista Management1
            peer_native = None
            rc_path = os.path.join(site_dir, remote_host, 'config', 'running-config.txt')
            if os.path.exists(rc_path):
                peer_native = parse_arista_management_vlan(open(rc_path).read())

            critical_links.append({
                'local_device': primary_turtle,
                'local_port': local_port,
                'local_native_vlan': local_native,
                'local_tagged_vlans': local_tagged,
                'peer_device': remote_host,
                'peer_role': 'managed_switch',
                'peer_port': peer_port,
                'peer_native_vlan': peer_native,
                'peer_tagged_vlans': [],
            })

    for w in warnings:
        print(w, file=sys.stderr)

    # Determine jumphost_pairs.
    # Order: whichever jumphosts appear first in critical_links become stage1/stage3.
    # `members` is the de-duplicated full list of jumphosts seen on the turtle —
    # populated whenever ≥1 bang exists, so HA-aware staged push (VIP-holder ==
    # primary == stage1, looked up at apply time) has the membership it needs.
    # Static stage1/stage3 stay populated for the non-HA case (single-bang sites).
    jh_links = [l for l in critical_links if l['peer_role'] == 'jumphost']
    seen = []
    for l in jh_links:
        if l['peer_device'] not in seen:
            seen.append(l['peer_device'])
    stage1_jh = seen[0] if seen else None
    stage3_jh = seen[1] if len(seen) > 1 else None

    topology = {
        'site': site_name,
        'mgmt_vlan': mgmt_vlan,
        'primary_turtle': primary_turtle,
        'jumphost_pairs': {
            'stage1': stage1_jh,
            'stage3': stage3_jh,
            'members': seen,
        },
        'critical_links': critical_links,
    }

    topo_path = os.path.join(site_dir, 'topology.yml')
    with open(topo_path, 'w') as f:
        f.write('# Generated by: reaper fetch — do not edit manually\n')
        yaml.dump(topology, f, default_flow_style=False, sort_keys=False)

    print(f'Wrote {topo_path}')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print(f'Usage: {sys.argv[0]} <site_config_dir>', file=sys.stderr)
        sys.exit(1)
    build(sys.argv[1])
