"""Tests for library/critical_ports.py.

critical_ports.py is an Ansible module. We test its analysis functions directly
by importing the module and calling internal functions — bypassing AnsibleModule.
"""
import sys
import os
import json
import yaml
import pytest

pytest.importorskip("ansible")    # library/critical_ports.py is an Ansible module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'library'))
import critical_ports

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures', 'braintree')


@pytest.fixture
def topology():
    # Minimal topology for testing — does not require topology.yml on disk
    return {
        'primary_turtle': 'turtle-lorax',
        'mgmt_vlan': 1,
        'critical_links': [
            {
                'local_device': 'turtle-lorax',
                'local_port': 'swp1',
                'local_native_vlan': 1,
                'peer_device': 'bang-lorax',
                'peer_role': 'jumphost',
                'peer_port': 'postprod',
                'peer_native_vlan': 1,
            },
            {
                'local_device': 'turtle-lorax',
                'local_port': 'swp3',
                'local_native_vlan': None,
                'peer_device': 'turtle-lorax',
                'peer_role': 'oob_loopback',
                'peer_port': 'eth0',
                'peer_native_vlan': None,
            },
        ]
    }


# ── Arista hierarchical diff parsing ─────────────────────────────────────────

def test_parse_arista_diff_attributes_vlan_change_to_correct_interface():
    diff_text = (
        "interface Management1\n"
        "   switchport access vlan 10\n"
        "interface Ethernet1/1\n"
        "   switchport access vlan 20\n"
    )
    changed = critical_ports.parse_arista_diff(diff_text)
    assert 'Management1' in changed
    assert 'Ethernet1/1' in changed


def test_parse_arista_diff_handles_trunk_vlan_change():
    diff_text = (
        "interface Ethernet19/1\n"
        "   switchport trunk allowed vlan 1,10,30,40,50\n"
    )
    changed = critical_ports.parse_arista_diff(diff_text)
    assert 'Ethernet19/1' in changed


def test_parse_arista_diff_ignores_lines_without_interface_context():
    diff_text = "vlan 10\n   name new-vlan\n"
    changed = critical_ports.parse_arista_diff(diff_text)
    # No interface stanza → no interfaces reported as changed
    assert changed == set()


def test_parse_arista_diff_resets_context_between_interfaces():
    diff_text = (
        "interface Management1\n"
        "   switchport access vlan 10\n"
        "!\n"
        "interface Ethernet1/1\n"
        "   speed forced 10000full\n"
    )
    changed = critical_ports.parse_arista_diff(diff_text)
    assert 'Management1' in changed
    assert 'Ethernet1/1' in changed


# ── Cumulus stanza diff parsing ───────────────────────────────────────────────

def test_parse_cumulus_diff_attributes_bridge_pvid_change_to_correct_iface():
    desired = "auto swp1\niface swp1\n    bridge-pvid 10\n    bridge-vids 2 6\n"
    live    = "auto swp1\niface swp1\n    bridge-pvid 1\n    bridge-vids 2 6\n"
    changed = critical_ports.parse_cumulus_diff(desired, live)
    assert 'swp1' in changed


def test_parse_cumulus_diff_no_change_when_identical():
    content = "auto swp1\niface swp1\n    bridge-pvid 1\n"
    changed = critical_ports.parse_cumulus_diff(content, content)
    assert changed == set()


def test_parse_cumulus_diff_handles_multiple_stanzas():
    desired = (
        "auto swp1\niface swp1\n    bridge-pvid 10\n\n"
        "auto swp3\niface swp3\n\n"
        "auto swp5\niface swp5\n    bridge-pvid 1\n"
    )
    live = (
        "auto swp1\niface swp1\n    bridge-pvid 1\n\n"
        "auto swp3\niface swp3\n\n"
        "auto swp5\niface swp5\n    bridge-pvid 1\n"
    )
    changed = critical_ports.parse_cumulus_diff(desired, live)
    assert 'swp1' in changed
    assert 'swp3' not in changed
    assert 'swp5' not in changed


# ── Critical link cross-reference ─────────────────────────────────────────────

def test_find_critical_changes_detects_vlan_change_on_critical_port(topology):
    # swp1 is in critical_links
    arista_changed = set()
    cumulus_changed = {'swp1'}
    jumphost_changed = set()  # {(host, port)}
    result = critical_ports.find_critical_changes(
        topology, arista_changed, cumulus_changed, set(), jumphost_changed
    )
    assert len(result) == 1
    assert result[0]['port'] == 'swp1'
    assert result[0]['device'] == 'turtle-lorax'


def test_find_critical_changes_ignores_non_critical_port(topology):
    arista_changed = set()
    cumulus_changed = {'swp10'}  # not in critical_links
    jumphost_changed = set()
    result = critical_ports.find_critical_changes(
        topology, arista_changed, cumulus_changed, set(), jumphost_changed
    )
    assert result == []


def test_find_critical_changes_detects_jumphost_port_change(topology):
    arista_changed = set()
    cumulus_changed = set()
    jumphost_changed = {('bang-lorax', 'postprod')}
    result = critical_ports.find_critical_changes(
        topology, arista_changed, cumulus_changed, set(), jumphost_changed
    )
    assert len(result) == 1
    assert result[0]['device'] == 'bang-lorax'
    assert result[0]['port'] == 'postprod'


def test_parse_cisco_commands_extracts_changed_interfaces():
    # Flat command list from cisco.ios.ios_config — one `interface X` line means X changed.
    commands = [
        'interface GigabitEthernet1/0/4',
        'switchport access vlan 5',
        'no shutdown',
        'interface TenGigabitEthernet1/1/2',
        'description new',
    ]
    changed = critical_ports.parse_cisco_commands(commands)
    assert changed == {'Gi1/0/4', 'Te1/1/2'}


def test_parse_cisco_commands_ignores_global_only_changes():
    # No interface stanza touched — should return empty set.
    commands = ['vtp mode transparent', 'lldp run']
    assert critical_ports.parse_cisco_commands(commands) == set()


def test_find_critical_changes_detects_cisco_change_on_critical_port():
    topology = {
        'critical_links': [{
            'local_device': 'turtle-gouda',
            'local_port': 'Te1/1/1',
            'peer_device': 'bang-gouda',
            'peer_role': 'jumphost',
            'peer_port': 'eth0',
        }]
    }
    result = critical_ports.find_critical_changes(
        topology, set(), set(), {'Te1/1/1'}, set()
    )
    assert len(result) == 1
    assert result[0]['port'] == 'Te1/1/1'
    assert result[0]['device'] == 'turtle-gouda'
