"""Unit tests for the publisher -- build a switch_facts row from per-port data."""


def test_build_row_adds_classification_fields():
    from flax_switch_sense.publisher import build_switch_facts_row
    per_port = {
        "Ethernet6/1": {
            "link": "link",
            "mask": "access",
            "macs": ["98:03:9b:a6:fc:24", "98:03:9b:a6:fc:22"],
            "lldp_neighbors": [{"mac": "98:03:9b:a6:fc:24",
                                "sysname": "tiogapass",
                                "port_description": "eth0",
                                "mgmt_addrs": ["172.24.6.101"]}],
        }
    }
    row = build_switch_facts_row("rabbit-fiesta", "eos", per_port, reachable=True)
    assert row["switch"] == "rabbit-fiesta"
    assert row["driver"] == "eos"
    assert row["reachable"] is True
    p = row["ports"]["Ethernet6/1"]
    assert p["bmc_mac"] == "98:03:9b:a6:fc:24"
    assert p["nic_macs"] == ["98:03:9b:a6:fc:22"]
    assert p["junk_macs"] == []
    assert p["classification_source"] == "lldp"
    assert p["lldp_disagreement"] is False
    # Original raw fields preserved
    assert sorted(p["macs"]) == ["98:03:9b:a6:fc:22", "98:03:9b:a6:fc:24"]


def test_macmath_for_port_resolves_by_access_vid():
    from flax_switch_sense.publisher import _macmath_for_port
    cfg = {"scheme": "distinct_oui"}
    by_vid = {26: cfg}
    # int access_vid -> config
    assert _macmath_for_port({"access_vid": 26}, by_vid) is cfg
    # str access_vid is coerced
    assert _macmath_for_port({"access_vid": "26"}, by_vid) is cfg
    # vid with no config -> None (legacy)
    assert _macmath_for_port({"access_vid": 24}, by_vid) is None
    # missing access_vid (trunk/unknown) -> None
    assert _macmath_for_port({}, by_vid) is None
    # empty mapping -> None regardless of vid
    assert _macmath_for_port({"access_vid": 26}, {}) is None
    # unparseable vid -> None
    assert _macmath_for_port({"access_vid": "trunk"}, by_vid) is None


def test_build_row_uses_macmath_distinct_oui_for_matching_vid():
    """A wedge-vid (26) port with a distinct_oui config: the foreign-OUI
    SONiC mgmt mac is a HOST (nic), not junk; the same-OUI extra is junk;
    the BMC is the highest mac. Legacy ±2 pairing would NOT produce this."""
    from flax_switch_sense.publisher import build_switch_facts_row
    # 3c:2c:99 is the Wedge BMC OUI; 00:90:fb is the foreign SONiC mgmt OUI.
    per_port = {
        "Ethernet23/1": {
            "link": "link", "mask": "access", "access_vid": 26,
            "macs": ["3c:2c:99:11:9d:02", "3c:2c:99:11:9d:00",
                     "00:90:fb:61:da:a0"],
            "lldp_neighbors": [],
        }
    }
    by_vid = {26: {"scheme": "distinct_oui", "note": "wedge"}}
    row = build_switch_facts_row("rabbit-hare", "eos", per_port,
                                 reachable=True, macmath_by_vid=by_vid)
    p = row["ports"]["Ethernet23/1"]
    assert p["bmc_mac"] == "3c:2c:99:11:9d:02"          # highest mac
    assert p["nic_macs"] == ["00:90:fb:61:da:a0"]        # foreign OUI = host
    assert [j["mac"] for j in p["junk_macs"]] == ["3c:2c:99:11:9d:00"]
    assert p["classification_source"] == "macmath_distinct_oui"


def test_build_row_no_macmath_for_vid_is_legacy():
    """A port whose access_vid has no macmath config classifies via the
    legacy LLDP/±2 path -- unchanged from today."""
    from flax_switch_sense.publisher import build_switch_facts_row
    per_port = {
        "Ethernet6/1": {
            "link": "link", "mask": "access", "access_vid": 24,
            "macs": ["98:03:9b:a6:fc:24", "98:03:9b:a6:fc:22"],
            "lldp_neighbors": [{"mac": "98:03:9b:a6:fc:24",
                                "sysname": "tiogapass",
                                "port_description": "eth0",
                                "mgmt_addrs": ["172.24.6.101"]}],
        }
    }
    # macmath has a config for vid 26 but NOT 24 -> port 24 stays legacy.
    by_vid = {26: {"scheme": "distinct_oui"}}
    row = build_switch_facts_row("rabbit-fiesta", "eos", per_port,
                                 reachable=True, macmath_by_vid=by_vid)
    p = row["ports"]["Ethernet6/1"]
    assert p["bmc_mac"] == "98:03:9b:a6:fc:24"
    assert p["nic_macs"] == ["98:03:9b:a6:fc:22"]
    assert p["classification_source"] == "lldp"


def test_build_row_extracts_port_mask():
    from flax_switch_sense.publisher import build_switch_facts_row
    per_port = {
        "Ethernet6/1": {"link": "link", "mask": "access",
                        "macs": [], "lldp_neighbors": []},
        "Ethernet49/1": {"link": "link", "mask": "trunk",
                         "macs": [], "lldp_neighbors": []},
    }
    row = build_switch_facts_row("rabbit-fiesta", "eos", per_port, reachable=True)
    assert row["port_mask"] == {"Ethernet6/1": "access",
                                "Ethernet49/1": "trunk"}


def test_build_row_unreachable_uses_empty_ports():
    from flax_switch_sense.publisher import build_switch_facts_row
    row = build_switch_facts_row("rabbit-fiesta", "eos", {}, reachable=False)
    assert row["reachable"] is False
    assert row["ports"] == {}
    assert row["port_mask"] == {}


def test_write_switch_facts_upserts_with_generation_increment(monkeypatch):
    from flax_switch_sense import publisher
    from unittest.mock import MagicMock

    cursor = MagicMock()
    cursor.fetchone.return_value = (42,)
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False

    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False

    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(publisher, "get_pool", lambda: pool)

    row = {
        "switch": "rabbit-fiesta", "driver": "eos",
        "polled_at": "2026-05-23T05:00:00Z", "reachable": True,
        "port_mask": {}, "ports": {},
    }
    gen = publisher.write_switch_facts(row)
    assert gen == 42

    # Single UPSERT, parameterized -- confirm the SQL shape
    args, _kwargs = cursor.execute.call_args
    sql = args[0]
    assert "INSERT INTO switch_facts" in sql
    assert "ON CONFLICT (switch) DO UPDATE" in sql
    assert "generation = switch_facts.generation + 1" in sql
    assert "RETURNING generation" in sql
