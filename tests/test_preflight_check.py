import sys
import os
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import preflight_check

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures', 'braintree')


@pytest.fixture
def topology():
    return {
        'critical_links': [
            {
                'local_device': 'turtle-lorax',
                'local_port': 'swp1',
                'peer_device': 'bang-lorax',
                'peer_role': 'jumphost',
                'peer_port': 'postprod',
            },
        ]
    }


@pytest.fixture
def both_sides_changing():
    return [
        {'device': 'turtle-lorax', 'port': 'swp1', 'peer_device': 'bang-lorax',
         'peer_port': 'postprod', 'peer_role': 'jumphost'},
        {'device': 'bang-lorax', 'port': 'postprod', 'peer_device': 'turtle-lorax',
         'peer_port': 'swp1', 'peer_role': 'switch'},
    ]


def test_consistent_vlans_returns_no_errors(topology, both_sides_changing, tmp_path):
    # Write matching config files
    jh_dir = tmp_path / 'bang-lorax' / 'config'
    jh_dir.mkdir(parents=True)
    (jh_dir / 'ifcfg-postprod').write_text("VLAN_ID='1'\nVLAN='yes'\n")
    sw_dir = tmp_path / 'turtle-lorax' / 'config'
    sw_dir.mkdir(parents=True)
    (sw_dir / 'interfaces.txt').write_text(
        "auto swp1\niface swp1\n    bridge-pvid 1\n"
    )
    errors = preflight_check.check(str(tmp_path), topology, both_sides_changing)
    assert errors == []


def test_mismatched_vlans_returns_error(topology, both_sides_changing, tmp_path):
    jh_dir = tmp_path / 'bang-lorax' / 'config'
    jh_dir.mkdir(parents=True)
    (jh_dir / 'ifcfg-postprod').write_text("VLAN_ID='99'\nVLAN='yes'\n")
    sw_dir = tmp_path / 'turtle-lorax' / 'config'
    sw_dir.mkdir(parents=True)
    (sw_dir / 'interfaces.txt').write_text(
        "auto swp1\niface swp1\n    bridge-pvid 10\n"
    )
    errors = preflight_check.check(str(tmp_path), topology, both_sides_changing)
    assert len(errors) == 1
    assert 'bang-lorax/postprod' in errors[0]
    assert 'turtle-lorax/swp1' in errors[0]


def test_only_switch_changing_skips_preflight(topology, tmp_path):
    # Only switch side in critical_changes — jumphost not changing
    switch_only = [
        {'device': 'turtle-lorax', 'port': 'swp1', 'peer_device': 'bang-lorax',
         'peer_port': 'postprod', 'peer_role': 'jumphost'},
    ]
    errors = preflight_check.check(str(tmp_path), topology, switch_only)
    # No jumphost entry in critical_changes → no consistency check needed
    assert errors == []
