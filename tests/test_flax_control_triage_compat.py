"""Tests for flax_control.triage_compat — switchportrecond status.json shape."""


def test_observe_row_to_triage_status_minimal():
    """Empty/unknown vars (no link) → 'off' state (tile blank, not pale-blue)."""
    from flax_control.triage_compat import observe_row_to_triage_status
    row = {
        "switch": "rabbit-lorax", "port": "et6b1",
        "vars": {v: {"value": "unknown", "since": None} for v in (
            "linkstate", "bmcmac", "bmcip", "bmcping", "bmcipmi", "bmcpower",
            "chassissn", "nodeip", "nodeping", "nodepxe", "nodessh", "inventory",
        )},
        "last_polled": "2026-05-24T13:00:00Z", "generation": 53,
    }
    out = observe_row_to_triage_status(row, ou="20L")
    assert out["port"] == "Et6/1"           # display form
    assert out["ou"] == "20"
    assert out["column"] == "L"
    # Nothing reached AND linkstate != 'link' -> 'off' so the React tile paints
    # blank, not the pale-blue "has link" colour.
    assert out["state"] == "off"
    assert out["time"] == "2026-05-24T13:00:00Z"


def test_status_has_link_only_is_linkstate():
    """linkstate=link with nothing deeper -> 'linkstate' (tile pale-blue, "has
    link"). This is the et6b1/et10b4 bug: switch reports link, but the tile was
    blank because no-link and has-link both serialised as 'linkstate'.
    """
    from flax_control.triage_compat import observe_row_to_triage_status
    row = {
        "switch": "rabbit-gouda", "port": "et6b1",
        "vars": {v: {"value": "unknown", "since": None} for v in (
            "bmcmac", "bmcip", "bmcping", "bmcipmi", "bmcpower",
            "chassissn", "nodeip", "nodeping", "nodepxe", "nodessh", "inventory",
        )},
        "last_polled": "2026-06-24T03:00:00Z", "generation": 7,
    }
    row["vars"]["linkstate"] = {"value": "link", "since": "2026-06-23T14:54:48Z"}
    out = observe_row_to_triage_status(row, ou="12L")
    assert out["state"] == "linkstate"      # has link -> pale-blue tile
    assert out["time"] == "2026-06-23T14:54:48Z"


def test_status_no_link_is_off():
    """linkstate=nolink with nothing deeper -> 'off' (tile blank). The dark-port
    counterpart of the has-link case: must NOT look like a linked port.
    """
    from flax_control.triage_compat import observe_row_to_triage_status
    row = {
        "switch": "rabbit-gouda", "port": "et6b2",
        "vars": {v: {"value": "unknown", "since": None} for v in (
            "bmcmac", "bmcip", "bmcping", "bmcipmi", "bmcpower",
            "chassissn", "nodeip", "nodeping", "nodepxe", "nodessh", "inventory",
        )},
        "last_polled": "2026-06-24T03:00:00Z", "generation": 7,
    }
    row["vars"]["linkstate"] = {"value": "nolink", "since": "2026-06-23T09:02:17Z"}
    out = observe_row_to_triage_status(row, ou="14L")
    assert out["state"] == "off"            # no link -> blank tile


def test_observe_row_to_triage_status_advanced():
    """With real values, status reflects the deepest reached var."""
    from flax_control.triage_compat import observe_row_to_triage_status
    row = {
        "switch": "rabbit-lorax", "port": "et6b1",
        "vars": {
            "linkstate":  {"value": "link",  "since": "t1"},
            "bmcmac":     {"value": "found", "since": "t2"},
            "bmcip":      {"value": "found", "since": "t3"},
            "bmcping":    {"value": "ok",    "since": "t4"},
            "bmcipmi":    {"value": "traditional", "since": "t5"},
            "bmcpower":   {"value": "on",    "since": "t6"},
            "chassissn":  {"value": "found", "since": "t7"},
            "nodeip":     {"value": "unknown", "since": None},
            "nodeping":   {"value": "unknown", "since": None},
            "nodepxe":    {"value": "unknown", "since": None},
            "nodessh":    {"value": "unknown", "since": None},
            "inventory":  {"value": "absent", "since": "t8"},
        },
        "last_polled": "2026-05-24T13:00:00Z", "generation": 100,
    }
    out = observe_row_to_triage_status(row, ou="20L")
    # _deepest_state uses per-var SUCCESS sets (matches
    # scripts/switchportrecond.py:_deepest_state). inventory='absent' is
    # NOT in the success set {'found'}, so chassissn (the last var with
    # value in its success set) wins.
    assert out["state"] == "chassissn"
    # time reflects when chassissn transitioned, not last_polled
    assert out["time"] == "t7"


def _row_with(bmcpower_value, bmc_power):
    """observe_state row at the chassissn stage (deepest=chassissn) with a given
    bmcpower var value + resolved watts — mirrors eindhoven Tioga Pass hosts
    whose power status varies independently of how far the pipeline reached."""
    return {
        "switch": "rabbit-gouda", "port": "et25b4",
        "vars": {
            "linkstate":  {"value": "link",  "since": "t1"},
            "bmcmac":     {"value": "found", "since": "t2"},
            "bmcip":      {"value": "found", "since": "t3"},
            "bmcping":    {"value": "ok",    "since": "t4"},
            "bmcipmi":    {"value": "traditional", "since": "t5"},
            "bmcpower":   {"value": bmcpower_value, "since": "t6"},
            "chassissn":  {"value": "found", "since": "t7"},
            "nodeip":     {"value": "notfound", "since": None},
            "nodeping":   {"value": "fail", "since": None},
            "nodepxe":    {"value": "notfound", "since": None},
            "nodessh":    {"value": "fail", "since": None},
            "inventory":  {"value": "notfound", "since": "t8"},
        },
        "last_polled": "2026-06-22T08:53:12Z", "generation": 100,
        "resolved": {"bmc_power": bmc_power},
    }


def test_status_carries_bmcpower_and_real_watts_when_off():
    """A powered-off host exposes bmcpower 'off' AND its real standby watts —
    the button shows the watts (grey) rather than masking them to 0 W."""
    from flax_control.triage_compat import observe_row_to_triage_status
    out = observe_row_to_triage_status(_row_with("off", "11 W"), ou="26C")
    assert out["state"] == "chassissn"     # status is independent of pipeline depth
    assert out["bmcpower"] == "off"
    assert out["power"] == "11 W"


def test_status_carries_bmcpower_on_without_watts():
    """On but no parseable watts: bmcpower 'on', power '?' (button shows green On)."""
    from flax_control.triage_compat import observe_row_to_triage_status
    out = observe_row_to_triage_status(_row_with("on", "?"), ou="12C")
    assert out["bmcpower"] == "on"
    assert out["power"] == "?"


def test_status_bmcpower_unknown_defaults_to_unknown():
    """Missing bmcpower var / unset watts -> 'unknown' + '?' (button shows '—')."""
    from flax_control.triage_compat import observe_row_to_triage_status
    row = {
        "switch": "rabbit-gouda", "port": "et6b1",
        "vars": {v: {"value": "unknown", "since": None} for v in (
            "linkstate", "bmcmac", "bmcip", "bmcping", "bmcipmi", "bmcpower",
            "chassissn", "nodeip", "nodeping", "nodepxe", "nodessh", "inventory",
        )},
        "last_polled": "2026-06-22T08:53:12Z", "generation": 1,
    }
    out = observe_row_to_triage_status(row, ou="20L")
    assert out["bmcpower"] == "unknown"
    assert out["power"] == "?"


def test_port_token_round_trip():
    from flax_control.triage_compat import display_port, internal_port
    assert display_port("et6b1") == "Et6/1"
    assert internal_port("Et6/1") == "et6b1"
    # long Arista form (the switch-overview links) must convert too
    assert internal_port("Ethernet6/1") == "et6b1"
    assert internal_port("et6b1") == "et6b1"       # already-internal passthrough
    assert internal_port("swp23") == "swp23"       # non-Arista passthrough


def test_arista_port_canonicalizes():
    """et6b1 (devices.port) -> Ethernet6/1 (reconcile flap form, spec §6)."""
    from flax_control.triage_compat import arista_port
    assert arista_port("et6b1") == "Ethernet6/1"
    assert arista_port("et1b1") == "Ethernet1/1"
    # Idempotent on the already-canonical switch_detail URL form.
    assert arista_port("Ethernet6/1") == "Ethernet6/1"
    # Passthrough for non-Arista names.
    assert arista_port("swp6") == "swp6"
