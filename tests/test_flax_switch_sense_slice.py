"""Unit tests for slice_by_port -- turns raw eAPI tables into per-port dict."""


def test_slice_combines_link_macs_lldp_per_port():
    from flax_switch_sense.slice import slice_by_port
    link = {"Ethernet6/1": "link", "Ethernet6/2": "nolink"}
    vlans = {"Ethernet6/1": {"access_vid": 24, "trunk": False},
             "Ethernet49/1": {"access_vid": 1, "trunk": True}}
    macs = [
        (24, "98:03:9b:a6:fc:24", "Ethernet6/1"),
        (24, "98:03:9b:a6:fc:22", "Ethernet6/1"),
    ]
    lldp = {
        "Ethernet6/1": [{"mac": "98:03:9b:a6:fc:24",
                         "sysname": "tiogapass",
                         "port_description": "eth0",
                         "mgmt_addrs": ["172.24.6.101"]}]
    }

    out = slice_by_port(link, vlans, macs, lldp)

    assert out["Ethernet6/1"]["link"] == "link"
    assert sorted(out["Ethernet6/1"]["macs"]) == [
        "98:03:9b:a6:fc:22", "98:03:9b:a6:fc:24"]
    assert out["Ethernet6/1"]["lldp_neighbors"][0]["sysname"] == "tiogapass"
    # Port with no MACs is still emitted (so consumers see it)
    assert out["Ethernet6/2"]["link"] == "nolink"
    assert out["Ethernet6/2"]["macs"] == []
    # Trunk port appears with mask='trunk'
    assert out["Ethernet49/1"]["link"] == "unknown"
    assert out["Ethernet49/1"].get("mask") == "trunk"


def test_slice_marks_access_vs_trunk_via_mask_field():
    from flax_switch_sense.slice import slice_by_port
    link = {"Ethernet6/1": "link", "Ethernet49/1": "link"}
    vlans = {"Ethernet6/1": {"access_vid": 24, "trunk": False},
             "Ethernet49/1": {"access_vid": 1,  "trunk": True}}
    out = slice_by_port(link, vlans, [], {})
    assert out["Ethernet6/1"].get("mask") == "access"
    assert out["Ethernet49/1"].get("mask") == "trunk"


def test_slice_propagates_access_vid_for_access_ports():
    from flax_switch_sense.slice import slice_by_port

    link = {"Ethernet6/1": "link"}
    vlans = {"Ethernet6/1": {"access_vid": 17, "trunk": False}}
    macs = []
    lldp = {}
    out = slice_by_port(link, vlans, macs, lldp)
    assert out["Ethernet6/1"]["access_vid"] == 17
    assert out["Ethernet6/1"]["mask"] == "access"


def test_slice_omits_access_vid_for_trunk_ports():
    from flax_switch_sense.slice import slice_by_port

    link = {"Ethernet1/1": "link"}
    vlans = {"Ethernet1/1": {"access_vid": 1, "trunk": True}}
    out = slice_by_port(link, vlans, [], {})
    # Trunks have no single access_vid — must be omitted, not zero
    assert "access_vid" not in out["Ethernet1/1"]
    assert out["Ethernet1/1"]["mask"] == "trunk"


def test_slice_omits_access_vid_when_vlans_table_missing_port():
    from flax_switch_sense.slice import slice_by_port

    link = {"Ethernet9/9": "link"}
    out = slice_by_port(link, {}, [], {})
    assert "access_vid" not in out["Ethernet9/9"]
    assert "mask" not in out["Ethernet9/9"]


def test_slice_marks_admin_down_only_for_listed_ports():
    from flax_switch_sense.slice import slice_by_port
    out = slice_by_port({"Ethernet7/1": "nolink", "Ethernet7/2": "nolink"},
                        {"Ethernet7/1": {"access_vid": 25, "trunk": False},
                         "Ethernet7/2": {"access_vid": 25, "trunk": False}},
                        [], {}, admin_down={"Ethernet7/1"})
    assert out["Ethernet7/1"]["admin"] == "down"
    assert "admin" not in out["Ethernet7/2"]
    assert out["Ethernet7/2"]["mask"] == "access" and out["Ethernet7/2"]["access_vid"] == 25


def test_slice_default_admin_down_is_empty():
    from flax_switch_sense.slice import slice_by_port
    assert "admin" not in slice_by_port({"E1": "nolink"}, {}, [], {})["E1"]
