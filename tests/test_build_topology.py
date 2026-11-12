"""Tests for scripts/build_topology.py.

Fixtures live in tests/fixtures/braintree/ — static copies of collected data.
Tests import the module functions directly (build_topology.py is structured as
importable functions with a __main__ guard).
"""
import sys
import os
import yaml
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import build_topology

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures', 'braintree')


def test_parse_cumulus_lldp_returns_list_of_peers():
    lldp_path = os.path.join(FIXTURES, 'turtle-lorax', 'show', 'show-lldp.txt')
    peers = build_topology.parse_cumulus_lldp(open(lldp_path).read())
    assert {'local_port': 'swp1', 'remote_host': 'bang', 'remote_port': 'postprod'} in peers
    assert {'local_port': 'swp3', 'remote_host': 'turtle-lorax', 'remote_port': 'eth0'} in peers


def test_parse_cumulus_lldp_handles_missing_remote_port():
    lldp_path = os.path.join(FIXTURES, 'turtle-lorax', 'show', 'show-lldp.txt')
    peers = build_topology.parse_cumulus_lldp(open(lldp_path).read())
    swp2 = next(p for p in peers if p['local_port'] == 'swp2')
    assert swp2['remote_host'] == 'rabbit-lorax'
    assert swp2['remote_port'] is None


def test_identify_primary_turtle_finds_turtle_directory_in_site():
    # turtle-* prefix is the by-name convention regardless of NOS.
    result = build_topology.identify_primary_turtle(FIXTURES)
    assert result == 'turtle-lorax'


def test_identify_primary_turtle_returns_none_when_no_turtle_dir(tmp_path):
    (tmp_path / 'rabbit-foo').mkdir()
    (tmp_path / 'bang-foo').mkdir()
    assert build_topology.identify_primary_turtle(str(tmp_path)) is None


def test_detect_nos_cumulus_via_interfaces_file(tmp_path):
    cfg = tmp_path / 'config'
    cfg.mkdir()
    (cfg / 'interfaces.txt').write_text('auto swp1\niface swp1\n')
    assert build_topology.detect_nos(str(tmp_path)) == 'cumulus'


def test_detect_nos_cisco_via_running_config(tmp_path):
    cfg = tmp_path / 'config'
    cfg.mkdir()
    (cfg / 'running-config.txt').write_text(
        'Building configuration...\n\n'
        'Current configuration : 1234 bytes\n!\n'
        'version 12.2\n'
        'hostname turtle-test\n'
    )
    assert build_topology.detect_nos(str(tmp_path)) == 'cisco'


def test_parse_cisco_lldp_extracts_neighbors():
    text = (
        'Capability codes:\n'
        '    (R) Router, (B) Bridge, (T) Telephone\n'
        '\n'
        'Device ID           Local Intf     Hold-time  Capability      Port ID\n'
        'bang-gouda          Te1/1/1        120        R               b8ce.f63e.56c2\n'
        'rabbit-gouda        Gi1/0/4        120        B,R             Management1\n'
        '\n'
        'Total entries displayed: 2\n'
    )
    peers = build_topology.parse_cisco_lldp(text)
    bg = next(p for p in peers if p['local_port'] == 'Te1/1/1')
    rg = next(p for p in peers if p['local_port'] == 'Gi1/0/4')
    assert bg['remote_host'] == 'bang-gouda'
    assert bg['remote_port'] is None  # MAC-only Port ID
    assert rg['remote_host'] == 'rabbit-gouda'
    assert rg['remote_port'] == 'Management1'


def test_parse_cisco_interfaces_extracts_access_and_trunk_vlans():
    text = (
        'interface GigabitEthernet1/0/4\n'
        ' description Rabbit-1 OOB mgmt\n'
        ' switchport access vlan 4\n'
        ' switchport mode access\n'
        '!\n'
        'interface TenGigabitEthernet1/1/1\n'
        ' description Bang-1\n'
        ' switchport trunk encapsulation dot1q\n'
        ' switchport trunk native vlan 4\n'
        ' switchport trunk allowed vlan 2,4,11-13\n'
        ' switchport mode trunk\n'
        '!\n'
    )
    ifaces = build_topology.parse_cisco_interfaces(text)
    assert ifaces['Gi1/0/4']['native_vlan'] == 4
    assert ifaces['Gi1/0/4']['tagged_vlans'] == []
    assert ifaces['Te1/1/1']['native_vlan'] == 4
    assert ifaces['Te1/1/1']['tagged_vlans'] == [2, 4, 11, 12, 13]


def test_parse_cisco_mgmt_vlan_picks_lowest_with_ip():
    text = (
        'interface Vlan1\n'
        ' no ip address\n'
        ' shutdown\n'
        '!\n'
        'interface Vlan4\n'
        ' description Lab mgmt SVI\n'
        ' ip address 192.168.88.33 255.255.255.0\n'
        '!\n'
    )
    assert build_topology.parse_cisco_mgmt_vlan(text) == 4


def test_parse_interfaces_pvid_and_vids():
    iface_path = os.path.join(FIXTURES, 'turtle-lorax', 'config', 'interfaces.txt')
    ifaces = build_topology.parse_cumulus_interfaces(open(iface_path).read())
    assert ifaces['swp1']['native_vlan'] == 1
    assert ifaces['swp1']['tagged_vlans'] == [2, 6]
    assert ifaces['swp3']['native_vlan'] is None
    assert ifaces['swp3']['tagged_vlans'] == []


def test_parse_interfaces_falls_back_to_bridge_pvid_when_port_has_none():
    iface_path = os.path.join(FIXTURES, 'turtle-lorax', 'config', 'interfaces.txt')
    ifaces = build_topology.parse_cumulus_interfaces(open(iface_path).read())
    # swp3 has no bridge-pvid — native_vlan should be None (port is unconfigured)
    assert ifaces['swp3']['native_vlan'] is None


def test_parse_jumphost_lldpctl_returns_peer_info():
    lldpctl_path = os.path.join(FIXTURES, 'bang-lorax', 'show', 'lldpctl.txt')
    peers = build_topology.parse_jumphost_lldpctl(open(lldpctl_path).read())
    # local port 'postprod' on bang-lorax connects to turtle-lorax swp1
    assert peers['postprod']['remote_host'] == 'turtle-lorax'
    assert peers['postprod']['remote_port'] == 'swp1'


def test_parse_ifcfg_extracts_vlan_id():
    ifcfg_path = os.path.join(FIXTURES, 'bang-lorax', 'config', 'ifcfg-postprod')
    vlan_id = build_topology.parse_ifcfg_vlan(open(ifcfg_path).read())
    assert vlan_id == 1


def test_build_topology_produces_valid_yaml(tmp_path):
    import shutil
    site_dir = tmp_path / 'braintree'
    shutil.copytree(FIXTURES, site_dir)
    build_topology.build(str(site_dir))
    topo_path = site_dir / 'topology.yml'
    assert topo_path.exists()
    topo = yaml.safe_load(topo_path.read_text())
    assert topo['site'] == 'braintree'
    assert topo['primary_turtle'] == 'turtle-lorax'
    assert topo['mgmt_vlan'] == 1


def test_build_topology_critical_links_include_jumphost():
    import shutil, tempfile
    with tempfile.TemporaryDirectory() as tmp:
        site_dir = os.path.join(tmp, 'braintree')
        import shutil
        shutil.copytree(FIXTURES, site_dir)
        build_topology.build(site_dir)
        topo = yaml.safe_load(open(os.path.join(site_dir, 'topology.yml')).read())
    links = topo['critical_links']
    jh_link = next((l for l in links if l.get('peer_role') == 'jumphost'), None)
    assert jh_link is not None
    assert jh_link['peer_device'] == 'bang-lorax'
    assert jh_link['peer_port'] == 'postprod'


def test_build_topology_emits_warning_for_unknown_peer_port(tmp_path, capsys):
    import shutil
    site_dir = tmp_path / 'braintree'
    shutil.copytree(FIXTURES, site_dir)
    # Remove the rabbit-lorax running-config so peer_port cannot be resolved
    rc = site_dir / 'rabbit-lorax' / 'config' / 'running-config.txt'
    rc.unlink()
    build_topology.build(str(site_dir))
    captured = capsys.readouterr()
    assert 'WARNING' in captured.out or 'WARNING' in captured.err


def test_build_topology_jumphost_pairs_stage1_is_primary():
    import shutil, tempfile
    with tempfile.TemporaryDirectory() as tmp:
        site_dir = os.path.join(tmp, 'braintree')
        shutil.copytree(FIXTURES, site_dir)
        build_topology.build(site_dir)
        topo = yaml.safe_load(open(os.path.join(site_dir, 'topology.yml')).read())
    # stage1 jumphost is the one directly connected to primary turtle's swp1
    assert topo['jumphost_pairs']['stage1'] == 'bang-lorax'
