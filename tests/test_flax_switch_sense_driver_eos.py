"""Unit tests for flax_switch_sense.driver_eos.EosDriver."""
from unittest.mock import patch, MagicMock


def test_eos_driver_holds_persistent_session():
    """The driver creates one requests.Session and reuses it across calls."""
    from flax_switch_sense.driver_eos import EosDriver
    with patch("flax_switch_sense.driver_eos.requests.Session") as mock_session_cls:
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {"result": [{}]}
        mock_session.post.return_value = mock_response
        mock_session_cls.return_value = mock_session
        d = EosDriver("rabbit-fiesta", "admin", "secret")
        # Trigger two no-op calls that go through the session
        d._post(["show version"])
        d._post(["show clock"])
        assert mock_session_cls.call_count == 1   # one Session per driver instance
        assert d._session.post.call_count == 2


def test_eos_driver_url_uses_https():
    from flax_switch_sense.driver_eos import EosDriver
    d = EosDriver("rabbit-fiesta", "admin", "secret")
    assert d._url == "https://rabbit-fiesta/command-api"


def test_interfaces_status_parses_link():
    from flax_switch_sense.driver_eos import EosDriver
    fake_response = [{
        "interfaceStatuses": {
            "Ethernet6/1": {"linkStatus": "connected",
                            "lineProtocolStatus": "up",
                            "bandwidth": 1000000000},
            "Ethernet6/2": {"linkStatus": "notconnect",
                            "lineProtocolStatus": "down",
                            "bandwidth": 0},
        }
    }]
    with patch.object(EosDriver, "_post", return_value=fake_response):
        d = EosDriver("rabbit-fiesta", "u", "p")
        out = d.interfaces_status()
    assert out["Ethernet6/1"] == "link"
    assert out["Ethernet6/2"] == "nolink"


def _switchports(rows):
    return [{"switchports": rows}]


def test_vlans_reads_configured_switchport_mode_including_down_ports():
    from flax_switch_sense.driver_eos import EosDriver
    rows = {
        "Ethernet7/1": {"enabled": True, "switchportInfo": {"mode": "access", "accessVlanId": 25}},
        "Ethernet7/2": {"enabled": True, "switchportInfo": {"mode": "access", "accessVlanId": 24}},
        "Ethernet49/1": {"enabled": True, "switchportInfo": {"mode": "trunk", "accessVlanId": 1}},
    }
    with patch.object(EosDriver, "_post", return_value=_switchports(rows)) as post:
        d = EosDriver("rabbit-edam", "u", "p")
        out = d.vlans()
    assert out["Ethernet7/1"] == {"access_vid": 25, "trunk": False}
    assert out["Ethernet7/2"] == {"access_vid": 24, "trunk": False}
    assert out["Ethernet49/1"] == {"access_vid": None, "trunk": True}
    assert post.call_args[0][0] == ["show interfaces switchport"]


def test_vlans_skips_non_switchport_and_unknown_modes():
    from flax_switch_sense.driver_eos import EosDriver
    rows = {
        "Ethernet7/1": {"enabled": True, "switchportInfo": {"mode": "access", "accessVlanId": 25}},
        "Ethernet50/1": {"enabled": True},
        "Ethernet51/1": {"enabled": False, "switchportInfo": {"mode": "access", "accessVlanId": 25}},
        "Ethernet52/1": {"enabled": True, "switchportInfo": {"mode": "dot1qTunnel", "accessVlanId": 25}},
    }
    with patch.object(EosDriver, "_post", return_value=_switchports(rows)):
        out = EosDriver("rabbit-edam", "u", "p").vlans()
    assert set(out) == {"Ethernet7/1"}


def test_admin_down_ports_from_disabled_status():
    from flax_switch_sense.driver_eos import EosDriver
    fake = [{"interfaceStatuses": {
        "Ethernet7/1": {"linkStatus": "disabled"},
        "Ethernet7/2": {"linkStatus": "notconnect"},
        "Ethernet7/3": {"linkStatus": "connected"},
    }}]
    with patch.object(EosDriver, "_post", return_value=fake):
        d = EosDriver("rabbit-edam", "u", "p")
        st = d.interfaces_status()
    assert st["Ethernet7/1"] == "nolink"
    assert st["Ethernet7/3"] == "link"
    assert d.admin_down_ports() == {"Ethernet7/1"}


def test_admin_down_ports_empty_before_any_status_read():
    from flax_switch_sense.driver_eos import EosDriver
    assert EosDriver("h", "u", "p").admin_down_ports() == set()


def test_mac_address_table_parses_entries():
    from flax_switch_sense.driver_eos import EosDriver
    fake_response = [{
        "unicastTable": {
            "tableEntries": [
                {"vlanId": 24, "macAddress": "98:03:9b:a6:fc:24",
                 "interface": "Ethernet6/1", "entryType": "dynamic"},
                {"vlanId": 24, "macAddress": "98:03:9b:a6:fc:22",
                 "interface": "Ethernet6/1", "entryType": "dynamic"},
                {"vlanId": 99, "macAddress": "ff:ff:ff:ff:ff:ff",
                 "interface": "Cpu",          "entryType": "static"},
            ]
        }
    }]
    with patch.object(EosDriver, "_post", return_value=fake_response):
        d = EosDriver("rabbit-fiesta", "u", "p")
        out = d.mac_address_table()
    # Returns list of (vlan, mac, port) tuples; CPU + static entries skipped
    assert (24, "98:03:9b:a6:fc:24", "Ethernet6/1") in out
    assert (24, "98:03:9b:a6:fc:22", "Ethernet6/1") in out
    assert not any(e[1] == "ff:ff:ff:ff:ff:ff" for e in out)


def test_lldp_neighbors_detail_extracts_per_port():
    from flax_switch_sense.driver_eos import EosDriver
    fake_response = [{
        "lldpNeighbors": {
            "Ethernet6/1": {
                "lldpNeighborInfo": [{
                    "chassisId": "98:03:9b:a6:fc:24",
                    "systemName": "tiogapass-bmc-6-1",
                    "neighborInterfaceInfo": {
                        "interfaceId": "\"98:03:9b:a6:fc:24\"",
                        "interfaceDescription": "eth0",
                    },
                    "managementAddresses": [
                        {"address": "172.24.6.101"},
                    ],
                }],
            }
        }
    }]
    with patch.object(EosDriver, "_post", return_value=fake_response):
        d = EosDriver("rabbit-fiesta", "u", "p")
        out = d.lldp_neighbors_detail()
    assert "Ethernet6/1" in out
    n = out["Ethernet6/1"][0]
    assert n["mac"] == "98:03:9b:a6:fc:24"
    assert n["sysname"] == "tiogapass-bmc-6-1"
    assert n["port_description"] == "eth0" and n["port_id"] == "98:03:9b:a6:fc:24"
    assert n["mgmt_addrs"] == ["172.24.6.101"]
