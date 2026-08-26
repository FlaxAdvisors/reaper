#!/usr/bin/python
# -*- coding: utf-8 -*-

# Copyright: Contributors to the reaper project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

DOCUMENTATION = r'''
---
module: critical_ports
short_description: Detect management-critical config changes after a check-mode apply
description:
  - Runs on localhost after apply_config.yml --check.
  - Reads per-device .check_result.json files, cross-references against topology.yml,
    and returns a list of ports whose changes would affect management connectivity.
options:
  site_dir:
    description: Path to the per-site config output directory.
    required: true
    type: str
  topology:
    description: Path to topology.yml for this site.
    required: true
    type: str
'''

EXAMPLES = r'''
- name: Run critical_ports module
  critical_ports:
    site_dir: "{{ config_output_dir }}/{{ site_name }}"
    topology: "{{ config_output_dir }}/{{ site_name }}/topology.yml"
  register: analysis
'''

RETURN = r'''
critical_changes:
  description: List of port changes that affect management connectivity.
  returned: always
  type: list
  elements: dict
  contains:
    device:
      description: Device hostname.
      type: str
    port:
      description: Interface name.
      type: str
    peer_device:
      description: Connected peer device.
      type: str
    peer_port:
      description: Connected peer port.
      type: str
    peer_role:
      description: Role of the peer (jumphost, switch, etc.).
      type: str
'''

import os
import re
import json
import difflib

from ansible.module_utils.basic import AnsibleModule

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


# ── Analysis functions (importable, testable standalone) ──────────────────────

def parse_arista_diff(diff_text):
    """Walk an Arista eos_config diff (added/changed lines) with stanza context.
    Returns a set of interface names that have changing child lines.

    EOS config is hierarchical: 'interface Foo' lines establish context,
    and indented sub-commands (including VLAN assignments) belong to that interface.
    A '!' line resets context (end of stanza).
    """
    changed = set()
    current_iface = None
    for line in diff_text.splitlines():
        stripped = line.strip()
        m = re.match(r'^interface\s+(\S+)', stripped)
        if m:
            current_iface = m.group(1)
            continue
        if stripped == '!':
            current_iface = None
            continue
        # Any non-empty child line under an interface stanza marks it changed
        if current_iface and stripped:
            changed.add(current_iface)
    return changed


def parse_cisco_commands(commands):
    """Walk a cisco.ios.ios_config `updates` command list with stanza context.
    Returns a set of interface names whose stanzas were touched.

    The `updates` list is flat (no indentation) — but ios_config emits
    `interface <X>` as the first command whenever it enters a stanza to make
    changes within. So any `interface <X>` line means X had at least one change.
    Global commands (e.g. `vtp mode transparent`, `lldp run`) leave the set
    empty, which is what we want — they're not interface-scoped.

    Interface names are normalized to short form (Gi/Te/Fa) so they match
    the form used by `show lldp neighbors` and topology.yml.
    """
    changed = set()
    for cmd in commands:
        m = re.match(r'^interface\s+(\S+)', cmd.strip())
        if m:
            changed.add(_short_ifname(m.group(1)))
    return changed


def _short_ifname(name):
    """Normalize Cisco interface names: GigabitEthernet1/0/4 → Gi1/0/4.
    Cisco LLDP shows short forms; running-config has long forms.
    """
    name = re.sub(r'^GigabitEthernet', 'Gi', name)
    name = re.sub(r'^TenGigabitEthernet', 'Te', name)
    name = re.sub(r'^FastEthernet', 'Fa', name)
    return name


def parse_cumulus_diff(desired_text, live_text):
    """Diff desired vs live /etc/network/interfaces content with stanza context.
    Returns a set of interface names whose stanzas differ.

    Stanzas are separated by blank lines. Each stanza starts with 'auto <iface>'
    or 'iface <iface>' — that line establishes the current interface context.
    Any diff line within the stanza attributes the change to that interface.
    """
    changed = set()
    diff_lines = list(difflib.unified_diff(
        live_text.splitlines(), desired_text.splitlines(), lineterm=''
    ))
    current_iface = None
    for line in diff_lines:
        if line.startswith('---') or line.startswith('+++') or line.startswith('@@'):
            continue
        content = line[1:] if line and line[0] in ('+', '-', ' ') else line
        stripped = content.strip()
        m = re.match(r'^(?:auto|iface)\s+(\S+)', stripped)
        if m:
            current_iface = m.group(1)
        elif not stripped:
            current_iface = None
        # A changed line (+ or -) within a stanza marks that interface
        if line and line[0] in ('+', '-') and current_iface:
            # Don't count the 'auto' or 'iface' header line itself as a change
            # (stanza might be reordered without semantic change)
            if not re.match(r'^(?:auto|iface)\s+', stripped):
                changed.add(current_iface)
    return changed


def find_critical_changes(topology, arista_changed, cumulus_changed, cisco_changed, jumphost_changed):
    """Cross-reference changed ports against topology critical_links.

    Args:
        topology: parsed topology.yml dict
        arista_changed: set of Arista interface names that changed
        cumulus_changed: set of Cumulus interface names that changed
        cisco_changed: set of Cisco IOS interface names that changed (short form)
        jumphost_changed: set of (host_name, port_name) tuples that changed

    Returns list of dicts: [{device, port, peer_device, peer_port, peer_role}, ...]
    """
    critical = []
    for link in topology.get('critical_links', []):
        local_device = link['local_device']
        local_port = link['local_port']
        peer_device = link['peer_device']
        peer_port = link.get('peer_port')
        peer_role = link.get('peer_role')

        # Check switch side (cumulus, arista, or cisco)
        if local_port in cumulus_changed or local_port in arista_changed or local_port in cisco_changed:
            critical.append({
                'device': local_device,
                'port': local_port,
                'peer_device': peer_device,
                'peer_port': peer_port,
                'peer_role': peer_role,
            })

        # Check jumphost side
        if peer_role == 'jumphost' and (peer_device, peer_port) in jumphost_changed:
            critical.append({
                'device': peer_device,
                'port': peer_port,
                'peer_device': local_device,
                'peer_port': local_port,
                'peer_role': 'switch',
            })

    return critical


def analyse_site(site_dir, topology_path):
    """Read per-device .check_result.json files and topology.yml.
    Return list of critical_changes dicts.
    """
    with open(topology_path) as f:
        topology = yaml.safe_load(f)

    arista_changed = set()
    cumulus_changed = set()
    cisco_changed = set()
    jumphost_changed = set()

    for entry in os.scandir(site_dir):
        if not entry.is_dir():
            continue
        result_path = os.path.join(entry.path, '.check_result.json')
        if not os.path.exists(result_path):
            continue
        with open(result_path) as f:
            result = json.load(f)

        device_type = result.get('device_type')

        if device_type == 'arista':
            diff_text = result.get('diff', '')
            arista_changed |= parse_arista_diff(diff_text)

        elif device_type == 'cumulus':
            if result.get('changed'):
                desired = result.get('desired_content', '')
                live = result.get('live_content', '')
                cumulus_changed |= parse_cumulus_diff(desired, live)

        elif device_type == 'cisco':
            if result.get('changed'):
                cisco_changed |= parse_cisco_commands(result.get('commands', []))

        elif device_type == 'jumphost':
            if result.get('changed'):
                for port in result.get('changed_ports', []):
                    jumphost_changed.add((entry.name, port))

    return find_critical_changes(topology, arista_changed, cumulus_changed, cisco_changed, jumphost_changed)


# ── Ansible module entrypoint ─────────────────────────────────────────────────

def main():
    module = AnsibleModule(
        argument_spec=dict(
            site_dir=dict(type='str', required=True),
            topology=dict(type='str', required=True),
        ),
        supports_check_mode=False,
    )

    site_dir = module.params['site_dir']
    topology_path = module.params['topology']

    if not HAS_YAML:
        module.fail_json(msg='PyYAML is required for critical_ports module')

    if not os.path.exists(topology_path):
        module.fail_json(
            msg=f"topology.yml not found at {topology_path}. Run 'reaper fetch' first."
        )

    try:
        critical_changes = analyse_site(site_dir, topology_path)
    except Exception as e:
        module.fail_json(msg=str(e))

    module.exit_json(changed=False, critical_changes=critical_changes)


if __name__ == '__main__':
    main()
