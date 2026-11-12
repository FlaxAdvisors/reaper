"""Smoke tests for flax_observe.state_machine.

The state machine is large; full behaviour coverage lives in
tests/test_switchportrecond.py (the original). These tests just confirm
the lift is wired up -- the function imports, transitions linkstate
correctly given canned switch_facts, and emits a transition event.
"""
import types


# ---------------------------------------------------------------------------
# Helper: initial port_state shape
# ---------------------------------------------------------------------------

def _initial_port_state(switch="rabbit-lorax", port="Et6/1", ou="20"):
    """Build a fresh port_state dict with all 12 vars initialised to unknown.

    Mirrors the shape that switchportrecond's main() and PortWorker expect
    (see scripts/switchportrecond.py around the _initial_port_state region).
    """
    from flax_observe.state_machine import STATE_VARS
    return {
        "switch": switch,
        "port": port,
        "ou": ou,
        "vars": {v: {"value": "unknown", "since": None} for v in STATE_VARS},
        "last_polled": None,
        "snapshot": {},
        # fields that port_worker_one_iter may read/write
        "bmc_ip": None,
        "nic_ip": None,
        "bmc_mac": None,
        "nic_mac": None,
        "nic_macs": [],
        "junk_macs": [],
        "chassis_sn": None,
        "bmc_power": "?",
        "bmcpower_unknown_streak": 0,
        "bmcpower_stale_since": None,
        "classification_source": None,
        "lldp_disagreement": False,
    }


# ---------------------------------------------------------------------------
# Helper: stub env namespace
# ---------------------------------------------------------------------------

def _stub_env(**overrides):
    """Build an env namespace whose probe callables all return safe defaults."""
    env = types.SimpleNamespace()
    # Probe callables
    env.probe_bmc_kind = lambda *a, **k: {
        "kind": "unknown", "product_name": None, "creds_used": None
    }
    env.bmc_power_status_openbmc = lambda *a, **k: "unknown"
    env.bmc_power_and_sdr_traditional = lambda *a, **k: ("unknown", None)
    env.chassis_serial_traditional = lambda *a, **k: None
    env.chassis_serial_openbmc = lambda *a, **k: None
    env.ping_host = lambda *a, **k: "unknown"
    env.lookup_lease_ip = lambda *a, **k: None
    env.nginx_pxe_seen = lambda *a, **k: "unknown"
    env.ssh_uptime = lambda *a, **k: "unknown"
    env.inventory_status = lambda *a, **k: "unknown"
    env.intentional_flap_active = lambda *a, **k: False
    env.sol_session_active = lambda *a, **k: False
    env.ensure_inband_admin_configured = lambda *a, **k: None
    # Config
    env.credentials = {}
    env.bmc_credentials = []
    env.host_credentials = []
    env.leases_path = "/var/lib/misc/dnsmasq.leases"
    env.dhcp_hosts_dir = "/etc/dnsmasq.dhcp-hosts"
    env.nginx_access_log = "/var/log/nginx/access.log"
    env.nodes_root = "/export/nodes"
    for k, v in overrides.items():
        setattr(env, k, v)
    return env


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_state_machine_transitions_link_when_facts_show_connected():
    """Smoke test: linkstate flips unknown -> link given a 'connected' fact."""
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state(switch="rabbit-lorax", port="Et6/1")
    switch_facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "connected",
        "macs": [],
        "lldp_neighbors": [],
    }}
    events = []
    port_worker_one_iter(port_state, switch_facts,
                         emit_event=events.append, env=_stub_env())

    assert port_state["vars"]["linkstate"]["value"] == "link", (
        f"expected 'link', got {port_state['vars']['linkstate']['value']!r}"
    )
    assert any(e.get("var") == "linkstate" for e in events), (
        "expected a linkstate transition event"
    )


def test_state_machine_nolink_when_facts_show_notconnect():
    """Smoke test: linkstate flips unknown -> nolink on notconnect."""
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    switch_facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "notconnect",
        "macs": [],
        "lldp_neighbors": [],
    }}
    events = []
    port_worker_one_iter(port_state, switch_facts,
                         emit_event=events.append, env=_stub_env())

    assert port_state["vars"]["linkstate"]["value"] == "nolink"
    # All other vars short-circuit to unknown
    assert port_state["vars"]["bmcmac"]["value"] == "unknown"


def test_state_machine_unknown_when_no_fact():
    """Smoke test: linkstate remains unknown when the port has no fact entry."""
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    # Empty switch_facts -- port not yet fetched
    events = []
    port_worker_one_iter(port_state, {},
                         emit_event=events.append, env=_stub_env())

    assert port_state["vars"]["linkstate"]["value"] == "unknown"
    # No transition event because value did not change (was already unknown)
    assert not any(e.get("var") == "linkstate" for e in events)


def test_state_machine_bmcmac_found_when_mac_present():
    """Smoke test: bmcmac -> found when a single MAC is on the port."""
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    switch_facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "connected",
        "macs": ["aa:bb:cc:dd:ee:ff"],
        "lldp_neighbors": [],
    }}
    events = []
    port_worker_one_iter(port_state, switch_facts,
                         emit_event=events.append, env=_stub_env())

    assert port_state["vars"]["linkstate"]["value"] == "link"
    assert port_state["vars"]["bmcmac"]["value"] == "found"
    assert port_state["bmc_mac"] == "aa:bb:cc:dd:ee:ff"


def test_state_machine_captures_product_name_from_probe():
    """The BMC probe returns the FRU product_name; the state machine must
    capture it into port_state (prereq for flax-discover classification).
    """
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    switch_facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "connected",
        "macs": ["aa:bb:cc:dd:ee:ff"],
        "lldp_neighbors": [],
    }}
    # Resolve the BMC MAC to an IP so the BMC-probe branch runs, and make the
    # kind probe surface a product_name.
    env = _stub_env(
        lookup_lease_ip=lambda *a, **k: "192.168.88.50",
        probe_bmc_kind=lambda *a, **k: {
            "kind": "traditional",
            "product_name": "Leopard-DDR4",
            "creds_used": ("admin", "admin"),
        },
    )
    events = []
    port_worker_one_iter(port_state, switch_facts,
                         emit_event=events.append, env=env)

    assert port_state["bmc_mac"] == "aa:bb:cc:dd:ee:ff"
    assert port_state["bmc_ip"] == "192.168.88.50"
    assert port_state["product_name"] == "Leopard-DDR4"


def test_state_machine_clears_product_name_when_no_bmc():
    """A SUSTAINED link-down (chassis-swap signal) clears stale product_name.

    A link-down is the chassis-swap signal, but it is now debounced: only once
    the down has persisted DOWN_DEBOUNCE_SECS is it treated as a durable session
    break that clears the latched chassis identity (product_name, chassis_sn,
    bmc_kind_cached). A sub-threshold flap retains them (see the inventory-latch
    tests below). Pre-seed an already-elapsed down so this cycle commits.
    """
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    port_state["product_name"] = "Leopard-DDR4"  # stale from a prior chassis
    port_state["link_down_since"] = _iso_ago(40)  # down already past the threshold
    switch_facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "notconnect",
        "macs": [],
        "lldp_neighbors": [],
    }}
    events = []
    port_worker_one_iter(port_state, switch_facts,
                         emit_event=events.append, env=_stub_env())

    assert port_state["product_name"] is None


def test_state_machine_clears_stale_bmc_ip_when_bmcmac_notfound():
    """A linked port with NO classifiable BMC MAC must not retain a stale bmc_ip.

    Without a confirmed BMC MAC there is no basis to vouch for any IP. The old
    `port_state.setdefault("bmc_ip", None)` kept the last-known lease, so a node
    whose BMC moved/vanished (e.g. Tioga Pass bmcmac=notfound after a FW-update
    + DHCP-lease change) stranded a dead address that the Triage tile then
    offered SOL + power against. The IP must be cleared so the UI shows it as
    unreachable, and re-acquired fresh once the MAC is classifiable again.
    """
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    port_state["bmc_ip"] = "172.17.25.102"  # stale lease from a prior cycle
    switch_facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "connected",
        "macs": [],  # link is up but nothing classifiable as a BMC MAC
        "lldp_neighbors": [],
    }}
    port_worker_one_iter(port_state, switch_facts,
                         emit_event=lambda e: None, env=_stub_env())

    assert not port_state["bmc_mac"], (
        f"precondition: bmc_mac should be falsy, got {port_state['bmc_mac']!r}")
    assert port_state["bmc_ip"] is None, (
        f"stale bmc_ip should be cleared, got {port_state['bmc_ip']!r}")


def test_state_machine_bmcpower_latch_is_bounded():
    """The bmcpower latch absorbs transient IPMI glitches but must NOT hold a
    stale value forever. After LATCH_MAX_UNKNOWN_STREAK consecutive unknown
    polls (BMC RMCP+ session-table exhaustion on a powered-off node), it gives
    up holding and surfaces 'unknown' + bmc_power '?' -> the Triage tile shows
    '—' rather than a confidently-wrong green/watts.
    """
    from flax_observe.state_machine import (
        port_worker_one_iter, LATCH_MAX_UNKNOWN_STREAK)

    port_state = _initial_port_state()
    # An identified, previously-ON BMC (chassis latched, bmcpower=on).
    port_state["chassis_sn"] = "WTH123"
    port_state["bmc_mac"] = "aa:bb:cc:dd:ee:ff"
    port_state["vars"]["bmcpower"] = {"value": "on", "since": "2026-01-01T00:00:00Z"}
    port_state["bmc_power"] = "129.80 W"

    switch_facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "connected",
        "macs": ["aa:bb:cc:dd:ee:ff"],
        "lldp_neighbors": [],
    }}
    # BMC pings + classifies, but every power poll fails (returns unknown).
    env = _stub_env(
        lookup_lease_ip=lambda *a, **k: "172.17.25.102",
        ping_host=lambda *a, **k: "ok",
        probe_bmc_kind=lambda *a, **k: {
            "kind": "traditional", "product_name": None, "creds_used": ("root", "x")},
        bmc_power_and_sdr_traditional=lambda *a, **k: ("unknown", None),
    )

    # Holds the stale 'on' through the bound...
    for i in range(LATCH_MAX_UNKNOWN_STREAK):
        port_worker_one_iter(port_state, switch_facts,
                             emit_event=lambda e: None, env=env)
        assert port_state["vars"]["bmcpower"]["value"] == "on", (
            f"cycle {i}: latch should still hold 'on'")
    # ...then gives up and tells the truth.
    port_worker_one_iter(port_state, switch_facts,
                         emit_event=lambda e: None, env=env)
    assert port_state["vars"]["bmcpower"]["value"] == "unknown", (
        "latch must release after the bound, not hold 'on' forever")
    assert port_state["bmc_power"] == "?", "stale watts must clear on release"


def test_classify_macs_inverts_when_host_nic_speaks_lldp():
    """Pinned to flax_switch_sense.classify behavior — both daemons must
    invert identically (leopard6b4 pattern, 2026-05-24).
    """
    from flax_observe.state_machine import classify_macs

    macs = ["98:03:9b:97:e4:06", "98:03:9b:97:e4:08"]
    lldp = [{"mac": "98:03:9b:97:e4:06", "sysname": "leopard6b4"}]
    r = classify_macs(macs, lldp)
    assert r.bmc == "98:03:9b:97:e4:08"
    assert r.nics == ["98:03:9b:97:e4:06"]
    assert r.classification_source == "lldp_nic_inverted"


def test_state_machine_intentional_flap_freezes_state():
    """Smoke test: intentional flap sentinel freezes linkstate, emits forensic event."""
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    port_state["vars"]["linkstate"] = {"value": "link", "since": "2026-05-22T00:00:00Z"}
    switch_facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "notconnect",
        "macs": [],
        "lldp_neighbors": [],
    }}
    events = []
    # Simulate sentinel active
    env = _stub_env(intentional_flap_active=lambda *a, **k: True)
    port_worker_one_iter(port_state, switch_facts,
                         emit_event=events.append, env=env)

    # linkstate must NOT have been changed to nolink
    assert port_state["vars"]["linkstate"]["value"] == "link"
    # A forensic event should have fired
    assert any(e.get("kind") == "intentional_flap_observed" for e in events)


# ---------------------------------------------------------------------------
# Task 3: IPv6 link-local BMC reach (probe as soon as MAC + IPv6 are up)
# ---------------------------------------------------------------------------

_LL_MAC = "3c:2c:99:58:9d:02"
_LL_ADDR = "fe80::3e2c:99ff:fe58:9d02"  # EUI-64 of _LL_MAC


def test_state_machine_reaches_bmc_via_link_local_when_no_ipv4_lease():
    """An OpenBMC switch with no IPv4 lease yet is reached over its EUI-64
    link-local (fe80::...%iface) so we can weutil it before it DHCPs; the
    probe targets the LL and port_state['bmc_ll'] is recorded.
    """
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    switch_facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "connected",
        "macs": [_LL_MAC],
        "lldp_neighbors": [],
        "access_vid": 26,
    }}

    probed_hosts = []

    def _probe(host, *a, **k):
        probed_hosts.append(host)
        return {"kind": "openbmc", "product_name": "WEDGE100S12V",
                "creds_used": ("root", "test-obmc-pw")}

    env = _stub_env(
        vlan_parents={26: "eth0"},
        # Reachable: any ll candidate pings true.
        ping6_reachable=lambda addr, timeout=2: True,
        # No IPv4 lease yet -> LL is the (only) reach.
        lookup_lease_ip=lambda *a, **k: None,
        probe_bmc_kind=_probe,
    )
    events = []
    port_worker_one_iter(port_state, switch_facts,
                         emit_event=events.append, env=env)

    expected_ll = _LL_ADDR + "%eth0.26"
    assert port_state["bmc_ll"] == expected_ll
    assert probed_hosts == [expected_ll], (
        f"probe should target the LL, got {probed_hosts!r}"
    )
    assert port_state["product_name"] == "WEDGE100S12V"


def test_state_machine_prefers_ipv4_when_it_answers_icmp():
    """REGRESSION GUARD: when the BMC answers at its IPv4 (ICMP ok), the probe
    targets the IPv4 IP even if the LL answers ping6 -- IPMI/RMCP (traditional
    BMCs) does not work over fe80::...%iface, so an LL-first reach silently lost
    every traditional BMC's chassis serial / product name.
    """
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    switch_facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "connected",
        "macs": [_LL_MAC],
        "lldp_neighbors": [],
        "access_vid": 26,
    }}

    probed_hosts = []

    def _probe(host, *a, **k):
        probed_hosts.append(host)
        return {"kind": "traditional", "product_name": "Type6 Tioga Pass",
                "creds_used": ("admin", "admin")}

    env = _stub_env(
        vlan_parents={26: "eth0"},
        ping6_reachable=lambda addr, timeout=2: True,    # LL answers...
        ping_host=lambda *a, **k: "ok",                  # ...and IPv4 answers
        lookup_lease_ip=lambda *a, **k: "172.26.0.123",
        probe_bmc_kind=_probe,
    )
    events = []
    port_worker_one_iter(port_state, switch_facts,
                         emit_event=events.append, env=env)

    assert port_state["bmc_ll"] is None
    assert probed_hosts == ["172.26.0.123"], (
        f"probe should target the IPv4, got {probed_hosts!r}"
    )
    assert port_state["bmc_ip"] == "172.26.0.123"


def test_state_machine_reaches_ll_when_reservation_ipv4_is_dead():
    """REGRESSION GUARD (wedge OpenBMC): resolve_ip returns a reservation IP
    the device hasn't claimed (no ICMP answer); the BMC probe must fall to the
    OpenBMC link-local, not the dead reservation IP.
    """
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    switch_facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "connected",
        "macs": [_LL_MAC],
        "lldp_neighbors": [],
        "access_vid": 26,
    }}

    probed_hosts = []

    def _probe(host, *a, **k):
        probed_hosts.append(host)
        return {"kind": "openbmc", "product_name": "WEDGE100S12V",
                "creds_used": ("root", "test-obmc-pw")}

    env = _stub_env(
        vlan_parents={26: "eth0"},
        ping6_reachable=lambda addr, timeout=2: True,    # LL answers...
        ping_host=lambda *a, **k: "fail",                # ...IPv4 is dead
        lookup_lease_ip=lambda *a, **k: "172.26.0.123",  # a reservation IP
        probe_bmc_kind=_probe,
    )
    events = []
    port_worker_one_iter(port_state, switch_facts,
                         emit_event=events.append, env=env)

    expected_ll = _LL_ADDR + "%eth0.26"
    assert port_state["bmc_ll"] == expected_ll
    assert probed_hosts == [expected_ll], (
        f"probe should target the LL, got {probed_hosts!r}"
    )
    assert port_state["product_name"] == "WEDGE100S12V"


def test_state_machine_falls_back_to_ipv4_when_link_local_unreachable():
    """ping6 fails -> fall back to today's IPv4-lease reach (probe gets the
    v4 IP), and bmc_ll is None.
    """
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    switch_facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "connected",
        "macs": [_LL_MAC],
        "lldp_neighbors": [],
        "access_vid": 26,
    }}

    probed_hosts = []

    def _probe(host, *a, **k):
        probed_hosts.append(host)
        return {"kind": "traditional", "product_name": "Leopard",
                "creds_used": ("admin", "admin")}

    env = _stub_env(
        vlan_parents={26: "eth0"},
        ping6_reachable=lambda addr, timeout=2: False,  # not reachable
        lookup_lease_ip=lambda *a, **k: "172.26.0.123",
        probe_bmc_kind=_probe,
    )
    events = []
    port_worker_one_iter(port_state, switch_facts,
                         emit_event=events.append, env=env)

    assert port_state["bmc_ll"] is None
    assert probed_hosts == ["172.26.0.123"], (
        f"probe should target the IPv4 lease, got {probed_hosts!r}"
    )


def test_state_machine_no_vid_iface_uses_ipv4_path_no_crash():
    """No vid->iface mapping (vlan_parents missing the vid) -> IPv4 path,
    bmc_ll None, no crash. ping6 must never even be called.
    """
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    switch_facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "connected",
        "macs": [_LL_MAC],
        "lldp_neighbors": [],
        "access_vid": 26,
    }}

    probed_hosts = []
    ping6_calls = []

    def _probe(host, *a, **k):
        probed_hosts.append(host)
        return {"kind": "unknown", "product_name": None, "creds_used": None}

    def _ping6(addr, timeout=2):
        ping6_calls.append(addr)
        return True

    env = _stub_env(
        vlan_parents={99: "eth0"},  # vid 26 absent
        ping6_reachable=_ping6,
        lookup_lease_ip=lambda *a, **k: "172.26.0.123",
        probe_bmc_kind=_probe,
    )
    events = []
    port_worker_one_iter(port_state, switch_facts,
                         emit_event=events.append, env=env)

    assert port_state["bmc_ll"] is None
    assert ping6_calls == [], "ping6 must not be called without a vid->iface map"
    assert probed_hosts == ["172.26.0.123"]


# ---------------------------------------------------------------------------
# Per-vid macmath in the port classifier (Task 4)
# ---------------------------------------------------------------------------

def test_state_machine_honors_distinct_oui_macmath_for_wedge_vid():
    """A wedge-vid (26) port whose macmath_by_vid entry is a distinct_oui
    config classifies the foreign-OUI SONiC mgmt mac as the host nic_mac and
    the highest same-OUI mac as the BMC -- NOT the legacy +/-2 pairing that
    would have made the phantom ...9d:00 the nic. observe_state.nic_mac drives
    the flax-classify host reservation, so this must match switch-sense.
    """
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    switch_facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "connected",
        "macs": ["3c:2c:99:58:9d:00", "3c:2c:99:58:9d:02", "00:90:fb:61:da:a0"],
        "lldp_neighbors": [],
        "access_vid": 26,
    }}
    env = _stub_env(macmath_by_vid={26: {"scheme": "distinct_oui"}})
    events = []
    port_worker_one_iter(port_state, switch_facts,
                         emit_event=events.append, env=env)

    # BMC = highest mac; nic_mac = the foreign-OUI SONiC host mac.
    assert port_state["bmc_mac"] == "3c:2c:99:58:9d:02"
    assert port_state["nic_mac"] == "00:90:fb:61:da:a0"
    assert port_state["nic_macs"] == ["00:90:fb:61:da:a0"]
    # The phantom same-OUI extra is junk, not the BMC or the host.
    assert port_state["bmc_mac"] != "3c:2c:99:58:9d:00"
    assert [j["mac"] for j in port_state["junk_macs"]] == ["3c:2c:99:58:9d:00"]
    assert port_state["classification_source"] == "macmath_distinct_oui"


def test_state_machine_macmath_unset_for_vid_uses_legacy_pairing():
    """A port whose access_vid has no macmath_by_vid entry falls back to the
    legacy +/-2 pairing (macmath=None), so observe behavior is unchanged for
    vids without a config.
    """
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    switch_facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "connected",
        # Paired-NIC family: bmc = nic + 2.
        "macs": ["98:03:9b:97:e4:06", "98:03:9b:97:e4:08"],
        "lldp_neighbors": [],
        "access_vid": 26,
    }}
    # macmath configured for a DIFFERENT vid only -> vid 26 -> None -> legacy.
    env = _stub_env(macmath_by_vid={99: {"scheme": "distinct_oui"}})
    events = []
    port_worker_one_iter(port_state, switch_facts,
                         emit_event=events.append, env=env)

    assert port_state["bmc_mac"] == "98:03:9b:97:e4:08"  # numeric-highest BMC
    assert port_state["nic_mac"] == "98:03:9b:97:e4:06"
    assert port_state["classification_source"] == "numeric_fallback"


# ---------------------------------------------------------------------------
# reach_for_mac — module-level reach helper (IPv4-lease preferred; LL only when
# there is no lease — IPMI/RMCP does not work over an IPv6 link-local address)
# ---------------------------------------------------------------------------

def test_reach_for_mac_uses_link_local_when_no_lease():
    from flax_observe.state_machine import reach_for_mac
    parents = {26: "eth0"}
    # No IPv4 lease yet + ping6 reachable -> LL target, is_ll True
    tgt, is_ll = reach_for_mac("3c:2c:99:58:9d:02", 26, parents,
                               ping6=lambda a, timeout=2: True,
                               resolve_ip=lambda m: None)
    assert tgt == "fe80::3e2c:99ff:fe58:9d02%eth0.26"
    assert is_ll is True


def test_reach_for_mac_prefers_ipv4_lease_over_link_local():
    # Without a ping4 gate, the IPv4 is preferred unconditionally (back-compat).
    from flax_observe.state_machine import reach_for_mac
    tgt, is_ll = reach_for_mac("08:c0:eb:d6:03:18", 24, {24: "eth0"},
                               ping6=lambda a, timeout=2: True,
                               resolve_ip=lambda m: "172.24.10.102")
    assert tgt == "172.24.10.102"
    assert is_ll is False


def test_reach_for_mac_uses_ipv4_when_it_answers_icmp():
    # REGRESSION GUARD (chassis SN / product name vanished, 0.9.21-0.9.23):
    # a traditional BMC answering at its IPv4 must be reached over IPv4 even
    # when its IPv6 LL answers ping6 -- IPMI/RMCP does not work over LL.
    from flax_observe.state_machine import reach_for_mac
    tgt, is_ll = reach_for_mac("08:c0:eb:d6:03:18", 24, {24: "eth0"},
                               ping6=lambda a, timeout=2: True,
                               resolve_ip=lambda m: "172.24.10.102",
                               ping4=lambda ip: True)
    assert tgt == "172.24.10.102"
    assert is_ll is False


def test_reach_for_mac_uses_ll_when_reservation_ipv4_is_dead():
    # REGRESSION GUARD (wedge OpenBMC lost openbmc kind under IPv4-pref):
    # resolve_ip returns a reservation IP the device hasn't claimed (no ICMP
    # answer); the reach must fall to the OpenBMC link-local, not the dead IP.
    from flax_observe.state_machine import reach_for_mac
    tgt, is_ll = reach_for_mac("3c:2c:99:58:9d:02", 26, {26: "eth0"},
                               ping6=lambda a, timeout=2: True,
                               resolve_ip=lambda m: "172.26.0.123",
                               ping4=lambda ip: False)
    assert tgt == "fe80::3e2c:99ff:fe58:9d02%eth0.26"
    assert is_ll is True


def test_reach_for_mac_falls_back_to_lease_ip():
    from flax_observe.state_machine import reach_for_mac
    tgt, is_ll = reach_for_mac("3c:2c:99:58:9d:02", 26, {26: "eth0"},
                               ping6=lambda a, timeout=2: False,
                               resolve_ip=lambda m: "172.26.0.123")
    assert tgt == "172.26.0.123"
    assert is_ll is False


def test_reach_for_mac_no_iface_uses_lease():
    from flax_observe.state_machine import reach_for_mac
    tgt, is_ll = reach_for_mac("3c:2c:99:58:9d:02", 99, {26: "eth0"},
                               ping6=lambda a, timeout=2: True,
                               resolve_ip=lambda m: "1.2.3.4")
    assert tgt == "1.2.3.4"
    assert is_ll is False


def test_ssh_uptime_accepts_link_local_target(monkeypatch):
    import flax_observe.host_probe as hp
    seen = {}

    class _CP:
        returncode = 0

    def _run(argv, **kw):
        seen["argv"] = argv
        return _CP()

    monkeypatch.setattr(hp.subprocess, "run", _run)
    out = hp.ssh_uptime("fe80::3e2c:99ff:fe58:9d02%eth0.26",
                        [{"user": "root", "pass": "x"}])
    assert out == "ok"
    assert "fe80::3e2c:99ff:fe58:9d02%eth0.26" in seen["argv"]


# ---------------------------------------------------------------------------
# Task 3: probe-confirmed bmc/host role wiring (confirm_roles integration)
# ---------------------------------------------------------------------------
#
# These drive a full cycle and assert the role label committed to port_state
# is probe-confirmed, not MAC-order luck. To make the per-MAC fakes
# deterministic we keep ping6 off (IPv4-lease reach) and map each MAC to a
# distinct lease IP, then key probe_bmc_kind / ssh_uptime on that target IP.

_CAPRI_HOST = "0c:42:a1:7b:e4:2d"
_SONIC = "00:90:fb:61:da:a0"
_OBMC = "3c:2c:99:58:9d:02"


def _ip_for_mac(mac):
    # Stable, distinct per-MAC lease IP so the probe fakes can switch on host.
    # None mac (e.g. a flipped-away bmc_mac) resolves to no lease, mirroring
    # the production lookup_lease_ip(None) -> None.
    if not mac:
        return None
    return "172.26.0." + str(100 + (int(mac.replace(":", ""), 16) % 100))


def test_state_machine_role_confirm_flip_capri_host():
    """capri: a lone visible host NIC. probe_bmc_kind says unknown; the common
    host login succeeds -> flip to host, NO bmc reservation.
    """
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    switch_facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "connected",
        "macs": [_CAPRI_HOST],
        "lldp_neighbors": [],
        "access_vid": 26,
    }}

    env = _stub_env(
        lookup_lease_ip=lambda path, mac, d: _ip_for_mac(mac),
        probe_bmc_kind=lambda host, *a, **k: {
            "kind": "unknown", "product_name": None, "creds_used": None},
        ssh_uptime=lambda host, *a, **k: "ok",
    )
    events = []
    port_worker_one_iter(port_state, switch_facts,
                         emit_event=events.append, env=env)

    assert port_state["bmc_mac"] is None
    assert port_state["nic_mac"] == _CAPRI_HOST
    assert port_state["role_source"] == "probe_flip_host"
    assert port_state["vars"]["bmcmac"]["value"] == "notfound"


def test_state_machine_role_confirm_promote_wedge_obmc():
    """promote: the numeric-highest MAC heuristically wins the BMC role but
    probes unknown + answers a host login (it is really the host); the OTHER
    visible MAC probes OpenBMC -> promote it to BMC, demote the heuristic
    primary to host. bmc_kind_cached must end keyed to the promoted MAC so the
    downstream power/serial block is a cache hit (no double-probe).
    """
    from flax_observe.state_machine import port_worker_one_iter

    # Paired family (+2): numeric-highest ..ee:04 is the heuristic BMC, ..ee:02
    # the heuristic nic. We invert reality: ..ee:04 is the host, ..ee:02 the
    # real OpenBMC.
    host_primary = "aa:bb:cc:dd:ee:04"   # heuristic bmc, really the host
    real_bmc = "aa:bb:cc:dd:ee:02"       # heuristic nic, really the OpenBMC

    port_state = _initial_port_state()
    switch_facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "connected",
        "macs": [real_bmc, host_primary],
        "lldp_neighbors": [],
        "access_vid": 26,
    }}

    bmc_ip = _ip_for_mac(real_bmc)
    host_ip = _ip_for_mac(host_primary)

    def _probe(host, *a, **k):
        if host == bmc_ip:
            return {"kind": "openbmc", "product_name": "WEDGE100S12V",
                    "creds_used": ("root", "test-obmc-pw")}
        return {"kind": "unknown", "product_name": None, "creds_used": None}

    def _ssh(host, *a, **k):
        return "ok" if host == host_ip else "fail"

    env = _stub_env(
        lookup_lease_ip=lambda path, mac, d: _ip_for_mac(mac),
        probe_bmc_kind=_probe,
        ssh_uptime=_ssh,
    )
    events = []
    port_worker_one_iter(port_state, switch_facts,
                         emit_event=events.append, env=env)

    assert port_state["bmc_mac"] == real_bmc
    assert port_state["nic_mac"] == host_primary
    assert port_state["role_source"] == "probe_promote_bmc"
    assert port_state["bmc_kind_cached"]["for_mac"] == real_bmc


def test_state_machine_role_confirm_healthy_no_extra_probes():
    """Healthy node: primary probes openbmc -> probe_confirmed, identical
    labels, and ZERO host probes fire (cost bound: happy path pays nothing
    extra).
    """
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    switch_facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "connected",
        "macs": ["98:03:9b:97:e4:06", "98:03:9b:97:e4:08"],
        "lldp_neighbors": [],
        "access_vid": 26,
    }}

    ssh_calls = {"n": 0}

    def _ssh(*a, **k):
        ssh_calls["n"] += 1
        return "ok"

    primary = "98:03:9b:97:e4:08"  # numeric-highest -> heuristic bmc

    env = _stub_env(
        lookup_lease_ip=lambda path, mac, d: _ip_for_mac(mac),
        probe_bmc_kind=lambda host, *a, **k: {
            "kind": "openbmc", "product_name": "Leopard-DDR4",
            "creds_used": ("root", "test-obmc-pw")},
        ssh_uptime=_ssh,
    )
    events = []
    port_worker_one_iter(port_state, switch_facts,
                         emit_event=events.append, env=env)

    assert port_state["bmc_mac"] == primary
    assert port_state["role_source"] == "probe_confirmed"
    # Cost bound: a confirmed primary fires no extra host/other-mac probing.
    # ssh_uptime is still called ONCE downstream for nodessh on nic_ip, so the
    # gather itself must add nothing -> total stays at that single downstream
    # call.
    assert ssh_calls["n"] == 1


def test_state_machine_role_confirm_healthy_steady_state_zero_bmc_probes():
    """Cost bound across cycles: a confirmed-openbmc BMC is bmc-kind-probed
    exactly ONCE (first cycle) and then served from bmc_kind_cached -- the
    role-confirmation gather must add NO per-cycle probe over the legacy
    downstream cache. Drive two cycles; the second fires zero probe_bmc_kind.
    """
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    switch_facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "connected",
        "macs": ["98:03:9b:97:e4:06", "98:03:9b:97:e4:08"],
        "lldp_neighbors": [],
        "access_vid": 26,
    }}

    bmc_probe_calls = {"n": 0}

    def _probe(*a, **k):
        bmc_probe_calls["n"] += 1
        return {"kind": "openbmc", "product_name": "Leopard-DDR4",
                "creds_used": ("root", "test-obmc-pw")}

    env = _stub_env(
        lookup_lease_ip=lambda path, mac, d: _ip_for_mac(mac),
        probe_bmc_kind=_probe,
        ssh_uptime=lambda *a, **k: "ok",
    )
    port_worker_one_iter(port_state, switch_facts,
                         emit_event=[].append, env=env)
    assert bmc_probe_calls["n"] == 1  # first cycle probes once

    port_worker_one_iter(port_state, switch_facts,
                         emit_event=[].append, env=env)
    assert bmc_probe_calls["n"] == 1, (
        "steady-state cycle must reuse bmc_kind_cached, not re-probe"
    )
    assert port_state["bmc_mac"] == "98:03:9b:97:e4:08"


_EXPIRED_TS = "2000-01-01T00:00:00Z"


def _two_mac_openbmc_facts():
    return {("rabbit-lorax", "Et6/1"): {
        "linkstate": "connected",
        "macs": ["98:03:9b:97:e4:06", "98:03:9b:97:e4:08"],
        "lldp_neighbors": [],
        "access_vid": 26,
    }}


def _sequenced_probe_env(results):
    calls = {"n": 0}

    def _probe(*a, **k):
        calls["n"] += 1
        return dict(results[min(calls["n"], len(results)) - 1])

    env = _stub_env(
        lookup_lease_ip=lambda path, mac, d: _ip_for_mac(mac),
        probe_bmc_kind=_probe,
        ssh_uptime=lambda *a, **k: "ok",
    )
    return env, calls


def test_missing_product_name_is_reprobed_after_retry_window():
    # A timed-out FRU read must not latch product_name=None for the chassis's lifetime.
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    facts = _two_mac_openbmc_facts()
    env, calls = _sequenced_probe_env([
        {"kind": "openbmc", "product_name": None, "creds_used": ("root", "pw")},
        {"kind": "openbmc", "product_name": "Type6 Tioga Pass Single Side",
         "creds_used": ("root", "pw")},
    ])
    port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)
    port_state["bmc_kind_cached"]["probed_at"] = _EXPIRED_TS
    port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)

    assert calls["n"] == 2
    assert port_state["product_name"] == "Type6 Tioga Pass Single Side"


def test_missing_product_name_is_not_reprobed_within_retry_window():
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    facts = _two_mac_openbmc_facts()
    env, calls = _sequenced_probe_env([
        {"kind": "openbmc", "product_name": None, "creds_used": ("root", "pw")},
    ])
    port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)
    port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)

    assert calls["n"] == 1


def test_redfish_kind_is_reprobed_after_retry_window():
    # Measured 2026-09-14 on eindhoven: an OpenBMC probed while it was still
    # booting answered only 443, classified 'redfish' ("Ami Redfish"), and kept
    # that label for the whole link session.
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    facts = _two_mac_openbmc_facts()
    env, calls = _sequenced_probe_env([
        {"kind": "redfish", "product_name": "Ami Redfish", "creds_used": None,
         "redfish_version": "1.17.0"},
        {"kind": "openbmc", "product_name": "Type6 Tioga Pass Single Side",
         "creds_used": ("root", "pw")},
    ])
    port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)
    port_state["bmc_kind_cached"]["probed_at"] = _EXPIRED_TS
    port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)

    assert calls["n"] == 2
    assert port_state["bmc_kind_cached"]["kind"] == "openbmc"
    assert port_state["product_name"] == "Type6 Tioga Pass Single Side"


def test_redfish_kind_is_not_reprobed_within_retry_window():
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    facts = _two_mac_openbmc_facts()
    env, calls = _sequenced_probe_env([
        {"kind": "redfish", "product_name": "Ami Redfish", "creds_used": None},
    ])
    port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)
    port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)

    assert calls["n"] == 1


def test_redfish_reprobe_reuses_the_cached_redfish_identity():
    # The re-probe only re-asks ssh/IPMI; authed Redfish reads on the AMI boards
    # can hang, so it must not repeat them every retry window.
    from flax_observe.state_machine import port_worker_one_iter

    kwargs_seen = []

    def _probe(*a, **k):
        kwargs_seen.append(k)
        return {"kind": "redfish", "product_name": "Ami Redfish",
                "creds_used": None, "redfish_version": "1.17.0"}

    env = _stub_env(
        lookup_lease_ip=lambda path, mac, d: _ip_for_mac(mac),
        probe_bmc_kind=_probe,
        ssh_uptime=lambda *a, **k: "ok",
    )
    port_state = _initial_port_state()
    facts = _two_mac_openbmc_facts()
    port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)
    port_state["bmc_kind_cached"]["probed_at"] = _EXPIRED_TS
    port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)

    assert len(kwargs_seen) == 2
    reuse = kwargs_seen[1].get("redfish_probe")
    assert reuse is not None
    assert reuse("172.17.7.101", []) == {
        "is_bmc": True, "product_name": "Ami Redfish", "redfish_version": "1.17.0"}


def _fresh_openbmc_cache(**overrides):
    """A cache that, on every OTHER axis, would NOT be due for re-probe:
    kind=openbmc (not redfish -> no _kind_retry_due), product_name present
    (no _product_name_retry_due), probed_at recent."""
    cache = {"kind": "openbmc", "product_name": "Type6 Tioga Pass Single Side",
              "creds_used": ("root", "pw"), "probed_at": "2026-09-18T00:00:00Z"}
    cache.update(overrides)
    return cache


def test_taxonomy_stale_true_when_stamp_missing():
    from flax_observe import bmc_vendor as _bv
    from flax_observe.state_machine import _taxonomy_stale, _reprobe_due

    cache = _fresh_openbmc_cache()  # no taxonomy_version key at all
    assert _taxonomy_stale(cache) is True
    assert _reprobe_due(cache) is True, (
        "a cache with no taxonomy stamp must re-probe by design, even though "
        "product_name/kind retry checks would otherwise say no"
    )


def test_taxonomy_stale_true_when_stamp_is_old():
    from flax_observe import bmc_vendor as _bv
    from flax_observe.state_machine import _taxonomy_stale, _reprobe_due

    cache = _fresh_openbmc_cache(taxonomy_version=_bv.TAXONOMY_VERSION - 1)
    assert _taxonomy_stale(cache) is True
    assert _reprobe_due(cache) is True


def test_taxonomy_stale_false_when_stamp_is_current():
    from flax_observe import bmc_vendor as _bv
    from flax_observe.state_machine import _taxonomy_stale, _reprobe_due

    cache = _fresh_openbmc_cache(taxonomy_version=_bv.TAXONOMY_VERSION)
    assert _taxonomy_stale(cache) is False
    assert _reprobe_due(cache) is False, (
        "a current-stamp cache that is otherwise not due must NOT re-probe"
    )


def test_stale_taxonomy_stamp_reprobes_once_then_cache_is_reused_with_vendor():
    """End-to-end (spec section 8 / brief Step 5 contract), through
    port_worker_one_iter rather than the bare predicates: a cache written
    before the current taxonomy (no stamp) re-probes EXACTLY ONCE, and the
    re-probed vendor then survives every subsequent cache-reuse cycle.

    The second half exercises Deviation 1 from task-3-report.md: the
    _bmc_kind_for reuse-dict must carry 'vendor' forward, or the unconditional
    bmc_kind_cached prime (role-confirmation block) would reset vendor to
    UNKNOWN on every reuse cycle even though no re-probe fired.
    """
    from flax_observe import bmc_vendor as _bv
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    facts = _two_mac_openbmc_facts()
    env, calls = _sequenced_probe_env([
        {"kind": "openbmc", "vendor": _bv.PHOSPHOR,
         "product_name": "Type6 Tioga Pass Single Side",
         "creds_used": ("root", "pw")},
    ])

    # Cycle 1: cold start -> one probe, cache primed + stamped + vendor'd.
    port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)
    assert calls["n"] == 1
    assert port_state["bmc_kind_cached"]["vendor"] == _bv.PHOSPHOR
    n_after_first = calls["n"]

    # Simulate a cache written by an OLDER taxonomy: no stamp, no vendor.
    del port_state["bmc_kind_cached"]["taxonomy_version"]
    del port_state["bmc_kind_cached"]["vendor"]

    # Several more cycles: the stale cache must re-probe EXACTLY ONCE (the
    # cycle right after the stamp is stripped), then be reused every cycle
    # after that.
    for _ in range(4):
        port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)

    assert calls["n"] == n_after_first + 1, (
        "a stale-stamp cache must re-probe exactly once, then be reused"
    )
    assert port_state["bmc_kind_cached"]["vendor"] == _bv.PHOSPHOR, (
        "vendor must survive the cache-reuse path across repeated reuse "
        "cycles, not just the single cycle that re-probed it"
    )


def test_chassis_swap_clears_latched_chassis_sn():
    """Regression: a different chassis swapped into the SAME switch port (the
    visible BMC mac changes between cycles) must NOT carry the prior chassis's
    latched serial over. The role-confirmation block primes bmc_kind_cached
    with the CURRENT bmc_mac before the downstream power/serial block runs, so
    the downstream `mac_changed` comparison (cache.for_mac != bmc_mac) was
    always False and the chassis-swap clear never fired. After the fix,
    mac_changed reflects the prior cycle's cache, so the swap is detected and
    the stale serial is cleared / re-acquired.
    """
    from flax_observe.state_machine import port_worker_one_iter

    # Cycle 1: chassis A on the port (single visible BMC mac).
    mac_a = "aa:bb:cc:dd:ee:08"
    # Cycle 2: chassis B swapped in (a DIFFERENT visible BMC mac).
    mac_b = "aa:bb:cc:dd:ee:0a"

    ip_a = _ip_for_mac(mac_a)
    ip_b = _ip_for_mac(mac_b)

    def _serial_openbmc(host, *a, **k):
        # Chassis A serial on A's reach; B's read transiently fails (None) so a
        # carried-over "SN-AAA" would be unmistakable in the assertion.
        if host == ip_a:
            return "SN-AAA"
        return None

    env = _stub_env(
        lookup_lease_ip=lambda path, mac, d: _ip_for_mac(mac),
        probe_bmc_kind=lambda host, *a, **k: {
            "kind": "openbmc", "product_name": "WEDGE100S12V",
            "creds_used": ("root", "test-obmc-pw")},
        chassis_serial_openbmc=_serial_openbmc,
        # No IPMI fallback path for the missing openbmc serial.
        chassis_serial_traditional=lambda *a, **k: None,
        ssh_uptime=lambda *a, **k: "ok",
        ping_host=lambda *a, **k: "ok",
    )

    # ---- Cycle 1: chassis A establishes the latched serial. ----
    facts_a = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "connected",
        "macs": [mac_a],
        "lldp_neighbors": [],
        "access_vid": 26,
    }}
    port_worker_one_iter(port_state := _initial_port_state(), facts_a,
                         emit_event=[].append, env=env)
    assert port_state["bmc_mac"] == mac_a
    assert port_state["bmc_kind_cached"]["for_mac"] == mac_a
    assert port_state["chassis_sn"] == "SN-AAA"

    # ---- Cycle 2: chassis B swapped onto the SAME port. ----
    facts_b = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "connected",
        "macs": [mac_b],
        "lldp_neighbors": [],
        "access_vid": 26,
    }}
    port_worker_one_iter(port_state, facts_b,
                         emit_event=[].append, env=env)

    # Part 2 / B superseded the immediate clear: chassis A now has a latched
    # anchor (bmc_mac_latched == mac_a) from cycle 1, so a different mac on
    # the port goes through the hold/serial-confirm decision, not the old
    # unconditional "mac changed -> clear" path. B's serial read returns
    # None (unreadable) -> HELD, not an immediate swap.
    assert port_state["bmc_mac"] == mac_a
    assert port_state["role_source"] == "held"
    assert port_state["chassis_sn"] == "SN-AAA"  # kept for the tile (R3)

    # ---- Cycle 3: the hold has expired with an unconfirmed new mac (B) still
    # on the port -> swap (forget, then first-sight). The stale chassis-A
    # serial must NOT be carried into chassis B.
    port_state["bmc_mac_held_since"] = _iso_ago(901)
    port_worker_one_iter(port_state, facts_b,
                         emit_event=[].append, env=env)
    assert port_state["chassis_sn"] != "SN-AAA", (
        "stale chassis-A serial was carried over to a swapped-in chassis B"
    )
    assert port_state["chassis_sn"] is None


# ---------------------------------------------------------------------------
# Inventory-latch debounce: "can't reach switch" != "link down".
#
# Regression for the 2026-06-21 incident where a single switch-sense eAPI
# read-timeout (reachable=false -> whole switch dropped from observe's cache)
# de-latched inventory found->notfound on every port whose worker happened to
# sample the cache during the ~10s gap. Requirements:
#   (A) cache miss (fact is None) is NON-INFORMATION: hold last-known linkstate,
#       never advance the inventory boundary.
#   (B) a REAL switch-reported down de-latches inventory only once it has
#       persisted >= DOWN_DEBOUNCE_SECS (default 30s); sub-threshold flaps keep
#       inventory latched. linkstate display stays honest (flips to nolink now).
# The inventory boundary is port_state["link_session_since"], distinct from the
# cosmetic linkstate.since fed to the UI.
# ---------------------------------------------------------------------------

import datetime as _dt


def _iso_ago(secs):
    return (_dt.datetime.utcnow() - _dt.timedelta(seconds=secs)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _up_port_state(**over):
    """A port that is currently up, inventory latched 'found'."""
    ps = _initial_port_state()
    ps["vars"]["linkstate"] = {"value": "link", "since": _iso_ago(3600)}
    ps["vars"]["inventory"] = {"value": "found", "since": _iso_ago(3600)}
    ps["link_session_since"] = _iso_ago(3600)
    ps["nic_mac"] = "aa:bb:cc:00:11:22"
    ps["chassis_sn"] = "SN-LATCHED"
    ps.update(over)
    return ps


def test_cache_miss_holds_last_known_linkstate():
    """(A) fact is None -> linkstate is HELD, not forced to 'unknown'."""
    from flax_observe.state_machine import port_worker_one_iter

    ps = _up_port_state()
    before_since = ps["vars"]["linkstate"]["since"]
    before_session = ps["link_session_since"]
    events = []
    # Empty switch_facts == switch not in cache (reachable=false / unreachable).
    port_worker_one_iter(ps, {}, lambda e: events.append(e), _stub_env())

    assert ps["vars"]["linkstate"]["value"] == "link", (
        "cache miss must hold last-known linkstate, not flip to unknown")
    assert ps["vars"]["linkstate"]["since"] == before_since, (
        "cache miss must not bump linkstate.since")
    assert ps["link_session_since"] == before_session, (
        "cache miss must not advance the inventory boundary")
    assert not any(e.get("var") == "linkstate" for e in events), (
        "cache miss must not emit a linkstate transition event")


def test_inventory_status_is_fed_link_session_since_not_linkstate_since():
    """(B-wiring) inventory_status gets link_session_since, not linkstate.since."""
    from flax_observe.state_machine import port_worker_one_iter

    ps = _up_port_state()
    # Diverge the two timestamps so we can tell which one is passed.
    ps["vars"]["linkstate"]["since"] = _iso_ago(10)     # recently bumped (cosmetic)
    ps["link_session_since"] = _iso_ago(3600)           # stable inventory boundary
    captured = {}

    def spy_inventory_status(nodes_root, nic_mac, link_ts):
        captured["link_ts"] = link_ts
        return "found"

    env = _stub_env(inventory_status=spy_inventory_status)
    # A single observed mac classifies to a nic_mac (bmc-2), so the boot
    # pipeline reaches the inventory stage and calls inventory_status. (With
    # macs=[] there is no nic_mac and the node stages are all unknown.)
    switch_facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "connected", "macs": ["aa:bb:cc:00:11:24"],
        "lldp_neighbors": []}}
    port_worker_one_iter(ps, switch_facts, lambda e: None, env)

    assert captured["link_ts"] == ps["link_session_since"], (
        "inventory_status must be fed link_session_since (stable boundary), "
        "not the cosmetic linkstate.since")


def test_transient_real_down_keeps_inventory_latched():
    """(B) a real nolink under the threshold does NOT advance the boundary."""
    from flax_observe.state_machine import port_worker_one_iter

    ps = _up_port_state()
    boundary_before = ps["link_session_since"]
    # First cycle of a real down: switch reports nolink.
    switch_facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "notconnect", "macs": [], "lldp_neighbors": []}}
    port_worker_one_iter(ps, switch_facts, lambda e: None, _stub_env())

    assert ps["vars"]["linkstate"]["value"] == "nolink", (
        "linkstate display must be honest and show nolink immediately")
    assert ps["link_session_since"] == boundary_before, (
        "a sub-threshold down must not advance the inventory boundary")
    assert ps["vars"]["inventory"]["value"] == "found", (
        "inventory must stay latched 'found' through a transient down")
    assert ps["chassis_sn"] == "SN-LATCHED", (
        "chassis_sn latch must survive a transient down")


def test_sustained_real_down_advances_inventory_boundary():
    """(B) a real nolink held >= threshold de-latches inventory."""
    from flax_observe.state_machine import port_worker_one_iter

    ps = _up_port_state()
    ps["bmc_mac"] = "aa:bb:cc:00:11:24"  # seed so the wipe assertion is non-trivial
    boundary_before = ps["link_session_since"]
    # Simulate a down that started 40s ago (already past the 30s threshold).
    ps["link_down_since"] = _iso_ago(40)
    switch_facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "notconnect", "macs": [], "lldp_neighbors": []}}
    port_worker_one_iter(ps, switch_facts, lambda e: None, _stub_env())

    assert ps["link_session_since"] != boundary_before, (
        "a >=30s real down must advance the inventory boundary (new session)")
    assert ps["vars"]["inventory"]["value"] == "notfound", (
        "inventory must de-latch to notfound once the down is committed")
    assert ps["chassis_sn"] is None, (
        "chassis_sn latch must clear on a committed link-session break")
    assert ps["bmc_mac"] is None, "committed link-down must null the bmc_mac"
    assert ps["nic_mac"] is None, "committed link-down must null the nic_mac"


def test_forget_identity_full_wipe():
    """_forget_identity supersedes the old partial-wipe function: now also
    clears the MAC/IP scalars and blanks the BMC + node reachability vars,
    so a removed/swapped chassis leaves no cached identity behind."""
    from flax_observe.state_machine import _forget_identity

    port_state = _initial_port_state()
    port_state["vars"]["inventory"] = {"value": "found", "since": "2026-06-20T00:00:00Z"}
    port_state["vars"]["chassissn"] = {"value": "found", "since": "2026-06-20T00:00:00Z"}
    port_state["vars"]["bmcip"] = {"value": "found", "since": "2026-06-20T00:00:00Z"}
    port_state["vars"]["nodeip"] = {"value": "found", "since": "2026-06-20T00:00:00Z"}
    port_state["link_session_since"] = "2026-06-20T00:00:00Z"
    port_state["chassis_sn"] = "ABC123"
    port_state["product_name"] = "Leopard"
    port_state["bmc_kind_cached"] = {"for_mac": "aa:bb:cc:dd:ee:ff"}
    port_state["bmc_mac"] = "aa:bb:cc:dd:ee:ff"
    port_state["nic_mac"] = "aa:bb:cc:dd:ee:01"
    port_state["bmc_ip"] = "172.16.7.2"
    port_state["nic_ip"] = "172.16.7.3"
    port_state["nic_macs"] = ["aa:bb:cc:dd:ee:01"]

    events = []
    _forget_identity(port_state, events.append)

    # Latch clears (same as before the rename):
    assert port_state["vars"]["inventory"]["value"] == "notfound"
    assert port_state["vars"]["chassissn"]["value"] == "unknown"
    assert port_state["chassis_sn"] is None
    assert port_state["product_name"] is None
    assert "bmc_kind_cached" not in port_state
    assert port_state["link_session_since"] != "2026-06-20T00:00:00Z"
    # NEW: scalar wipe
    assert port_state["bmc_mac"] is None
    assert port_state["nic_mac"] is None
    assert port_state["bmc_ip"] is None
    assert port_state["nic_ip"] is None
    assert port_state["nic_macs"] == []
    # NEW: reachability vars blanked to unknown
    assert port_state["vars"]["bmcip"]["value"] == "unknown"
    assert port_state["vars"]["nodeip"]["value"] == "unknown"
    assert port_state["bmc_power"] == "?"
    kinds = {e.get("var") for e in events}
    assert {"inventory", "chassissn", "bmcip", "nodeip"} <= kinds


def test_hydrated_link_fact_preserves_inventory_boundary():
    """The mechanism: a port_state hydrated with linkstate=link meeting a fresh
    'link' fact makes NO transition, so linkstate.since and link_session_since
    are preserved and inventory_status (against the OLD boundary) stays found.
    This is what stops a restart from blanking inventory."""
    from flax_observe.state_machine import port_worker_one_iter

    T0 = "2026-06-20T00:00:00Z"
    port_state = _initial_port_state(switch="rabbit-lorax", port="Et6/1")
    port_state["vars"]["linkstate"] = {"value": "link", "since": T0}
    port_state["vars"]["inventory"] = {"value": "found", "since": T0}
    port_state["link_session_since"] = T0

    # A +/-2 pair so the classifier yields a nic_mac, and the upstream boot
    # stages are satisfied, so the gated inventory var reflects inventory_status
    # end-to-end (not just the boundary mechanism in isolation).
    switch_facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "link",
        "macs": ["52:54:00:11:22:33", "52:54:00:11:22:35"], "lldp_neighbors": []}}
    # inventory_status returns found only when called with the preserved boundary
    env = _stub_env(
        lookup_lease_ip=lambda *a, **k: "172.16.7.2",
        kea_lease_fresh=lambda mac, boundary: True,
        ping_host=lambda ip: "ok",
        nginx_pxe_seen=lambda *a, **k: "found",
        ssh_uptime=lambda *a, **k: "ok",
        inventory_status=lambda root, mac, ts: "found" if ts == T0 else "notfound")

    events = []
    port_worker_one_iter(port_state, switch_facts, emit_event=events.append, env=env)

    assert port_state["vars"]["linkstate"]["since"] == T0  # no spurious transition
    assert port_state["link_session_since"] == T0          # boundary preserved
    # boundary preserved + chain satisfied -> inventory_status (against the
    # preserved boundary) reports found, so the gated inventory var is found.
    assert port_state["vars"]["inventory"]["value"] == "found"


def test_advance_inventory_boundary_preserves_chassis_identity():
    """_advance_inventory_boundary bumps the inventory boundary but, UNLIKE
    _forget_identity, leaves chassis identity intact (a power cycle is the
    same chassis)."""
    from flax_observe.state_machine import _advance_inventory_boundary

    ps = _initial_port_state()
    ps["link_session_since"] = "2026-06-20T00:00:00Z"
    ps["chassis_sn"] = "SN123"
    ps["product_name"] = "Leopard"
    ps["bmc_kind_cached"] = {"for_mac": "aa:bb:cc:dd:ee:ff"}

    _advance_inventory_boundary(ps)

    assert ps["link_session_since"] != "2026-06-20T00:00:00Z"   # advanced
    assert ps["chassis_sn"] == "SN123"                          # preserved
    assert ps["product_name"] == "Leopard"                      # preserved
    assert ps["bmc_kind_cached"] == {"for_mac": "aa:bb:cc:dd:ee:ff"}  # preserved


# ---------------------------------------------------------------------------
# Task 2: bmcpower off->on triggers inventory boundary reset
# ---------------------------------------------------------------------------

def _poweron_port_state(prior_power, T0="2026-06-20T00:00:00Z"):
    """A linked, inventoried port_state seeded with a prior bmcpower value and a
    fixed inventory boundary T0. linkstate is pre-seeded to 'link' so the link-up
    path does NOT reset the boundary before the power trigger runs."""
    ps = _initial_port_state()
    ps["vars"]["linkstate"] = {"value": "link", "since": T0}
    ps["vars"]["inventory"] = {"value": "found", "since": T0}
    ps["vars"]["bmcpower"] = {"value": prior_power, "since": T0}
    ps["vars"]["chassissn"] = {"value": "found", "since": T0}
    ps["link_session_since"] = T0
    ps["chassis_sn"] = "SN123"
    ps["product_name"] = "Leopard"
    return ps


def _poweron_env(T0="2026-06-20T00:00:00Z"):
    """Env that drives the BMC probe to read power 'on' and makes inventory_status
    return 'found' ONLY against the pre-trigger boundary T0 (so an advanced
    boundary surfaces as notfound)."""
    return _stub_env(
        lookup_lease_ip=lambda *a, **k: "192.168.88.50",
        probe_bmc_kind=lambda *a, **k: {
            "kind": "traditional", "product_name": "Leopard",
            "creds_used": ("admin", "admin")},
        bmc_power_and_sdr_traditional=lambda *a, **k: ("on", "120 W"),
        inventory_status=lambda root, mac, ts: "found" if ts == T0 else "notfound",
        kea_lease_fresh=lambda mac, boundary: True,
        ping_host=lambda ip: "ok",
        nginx_pxe_seen=lambda *a, **k: "found",
        ssh_uptime=lambda *a, **k: "ok",
    )


def _power_env(probe_result, T0="2026-06-20T00:00:00Z"):
    """Like _poweron_env but with the BMC power+sdr probe result injected, so a
    test can drive a specific (pwr, watts) pair through the power block."""
    return _stub_env(
        lookup_lease_ip=lambda *a, **k: "192.168.88.50",
        probe_bmc_kind=lambda *a, **k: {
            "kind": "traditional", "product_name": "Leopard",
            "creds_used": ("admin", "admin")},
        bmc_power_and_sdr_traditional=lambda *a, **k: probe_result,
        inventory_status=lambda root, mac, ts: "found" if ts == T0 else "notfound",
        kea_lease_fresh=lambda mac, boundary: True,
        ping_host=lambda ip: "ok",
        nginx_pxe_seen=lambda *a, **k: "found",
        ssh_uptime=lambda *a, **k: "ok",
    )


def test_bmc_power_keeps_real_watts_when_off():
    """A powered-off host's standby draw must be reported, NOT masked to '0 W':
    bmc_power carries the real watts while the bmcpower var conveys 'off'."""
    from flax_observe.state_machine import port_worker_one_iter
    ps = _poweron_port_state("off")                 # prior off -> off (no reset)
    facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "link", "macs": ["aa:bb:cc:dd:ee:ff"], "lldp_neighbors": []}}
    port_worker_one_iter(ps, facts, emit_event=[].append,
                         env=_power_env(("off", "11 W")))
    assert ps["vars"]["bmcpower"]["value"] == "off"
    assert ps["bmc_power"] == "11 W"


def test_bmc_power_unknown_marker_when_on_without_watts():
    """On but no parseable watts -> bmc_power '?' (no number to show), never a
    stale leftover. The bmcpower var still reports 'on' so the UI shows On."""
    from flax_observe.state_machine import port_worker_one_iter
    ps = _poweron_port_state("on")                  # prior on -> on (no reset)
    ps["bmc_power"] = "999 W"                        # stale leftover from before
    facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "link", "macs": ["aa:bb:cc:dd:ee:ff"], "lldp_neighbors": []}}
    port_worker_one_iter(ps, facts, emit_event=[].append,
                         env=_power_env(("on", None)))
    assert ps["vars"]["bmcpower"]["value"] == "on"
    assert ps["bmc_power"] == "?"


def test_poweron_off_to_on_resets_inventory():
    """bmcpower off->on advances the inventory boundary, inventory recomputes to
    notfound, and an inventory_reset_on_power_on event is emitted."""
    from flax_observe.state_machine import port_worker_one_iter
    T0 = "2026-06-20T00:00:00Z"
    ps = _poweron_port_state("off", T0)
    facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "link", "macs": ["aa:bb:cc:dd:ee:ff"], "lldp_neighbors": []}}
    events = []
    port_worker_one_iter(ps, facts, emit_event=events.append, env=_poweron_env(T0))

    assert ps["link_session_since"] != T0                       # boundary advanced
    assert ps["vars"]["inventory"]["value"] == "notfound"
    reset = [e for e in events if e.get("kind") == "inventory_reset_on_power_on"]
    assert len(reset) == 1
    # payload identifies the port + the BMC that booted (classified from the fact)
    assert reset[0]["switch"] == "rabbit-lorax"
    assert reset[0]["port"] == "Et6/1"
    assert reset[0]["bmc_mac"] == "aa:bb:cc:dd:ee:ff"


def test_poweron_off_to_on_preserves_chassis_identity():
    """The power-on reset must NOT clear chassis identity (same chassis)."""
    from flax_observe.state_machine import port_worker_one_iter
    T0 = "2026-06-20T00:00:00Z"
    ps = _poweron_port_state("off", T0)
    facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "link", "macs": ["aa:bb:cc:dd:ee:ff"], "lldp_neighbors": []}}
    port_worker_one_iter(ps, facts, emit_event=[].append, env=_poweron_env(T0))

    assert ps["chassis_sn"] == "SN123"
    assert ps["vars"]["chassissn"]["value"] == "found"
    assert ps["product_name"] == "Leopard"


def test_poweron_unknown_to_on_does_not_reset():
    """unknown->on must NOT trigger -- this is what stops a flax-observe restart
    (bmcpower starts unknown) from wiping the hydrated inventory latch."""
    from flax_observe.state_machine import port_worker_one_iter
    T0 = "2026-06-20T00:00:00Z"
    ps = _poweron_port_state("unknown", T0)
    facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "link", "macs": ["aa:bb:cc:dd:ee:ff"], "lldp_neighbors": []}}
    events = []
    port_worker_one_iter(ps, facts, emit_event=events.append, env=_poweron_env(T0))

    assert ps["link_session_since"] == T0                       # boundary unchanged
    assert ps["vars"]["inventory"]["value"] == "found"          # latch held
    assert not any(e.get("kind") == "inventory_reset_on_power_on" for e in events)


def test_poweron_on_to_on_is_noop():
    """Steady on->on does not touch the boundary."""
    from flax_observe.state_machine import port_worker_one_iter
    T0 = "2026-06-20T00:00:00Z"
    ps = _poweron_port_state("on", T0)
    facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "link", "macs": ["aa:bb:cc:dd:ee:ff"], "lldp_neighbors": []}}
    events = []
    port_worker_one_iter(ps, facts, emit_event=events.append, env=_poweron_env(T0))

    assert ps["link_session_since"] == T0
    assert ps["vars"]["inventory"]["value"] == "found"
    assert not any(e.get("kind") == "inventory_reset_on_power_on" for e in events)


# ---------------------------------------------------------------------------
# Node-side vars: each set from its OWN signal (NO monotonic cross-gating).
# inventory stays latched; nodeip/nodepxe session-relative; ping/ssh live.
# ---------------------------------------------------------------------------

def _pipeline_env(**overrides):
    """Env driving a fully-satisfiable node chain; override to exercise a stage."""
    env = _stub_env(
        lookup_lease_ip=lambda *a, **k: "172.16.7.2",   # nic_ip resolves
        kea_lease_fresh=lambda mac, boundary: True,      # nodeip: DHCP'd this session
        ping_host=lambda ip: "ok",                       # nodeping: live
        nginx_pxe_seen=lambda *a, **k: "found",          # nodepxe: session-relative
        ssh_uptime=lambda *a, **k: "ok",                 # nodessh: live
        inventory_status=lambda *a, **k: "found",        # inventory: latched
    )
    for k, v in overrides.items():
        setattr(env, k, v)
    return env


def _pipeline_port_state(T0="2026-06-20T00:00:00Z"):
    ps = _initial_port_state()
    ps["vars"]["linkstate"] = {"value": "link", "since": T0}
    ps["link_session_since"] = T0
    return ps


_FACT_ONE_MAC = {("rabbit-lorax", "Et6/1"): {
    "linkstate": "link", "macs": ["aa:bb:cc:dd:ee:ff"], "lldp_neighbors": []}}


def test_node_full_chain_all_found():
    from flax_observe.state_machine import port_worker_one_iter
    ps = _pipeline_port_state()
    port_worker_one_iter(ps, _FACT_ONE_MAC, lambda e: None, _pipeline_env())
    assert ps["vars"]["nodeip"]["value"] == "found"
    assert ps["vars"]["nodeping"]["value"] == "ok"
    assert ps["vars"]["nodepxe"]["value"] == "found"
    assert ps["vars"]["nodessh"]["value"] == "ok"
    assert ps["vars"]["inventory"]["value"] == "found"


def test_node_idle_off_keeps_inventory_latched():
    """THE regression guard: a provisioned node gone idle/off (no fresh DHCP
    lease, not pingable, no ssh) keeps inventory=found -- inventory is its own
    latched fact, never gated behind the live node chain (0.9.37/0.9.38 bug)."""
    from flax_observe.state_machine import port_worker_one_iter
    ps = _pipeline_port_state()
    env = _pipeline_env(
        kea_lease_fresh=lambda m, b: False,    # off: no fresh lease
        ping_host=lambda ip: "fail",           # off: not pingable
        ssh_uptime=lambda *a, **k: "fail",     # off: no ssh
        # nginx_pxe_seen + inventory_status persist (log/file) -> still found
    )
    port_worker_one_iter(ps, _FACT_ONE_MAC, lambda e: None, env)
    assert ps["vars"]["inventory"]["value"] == "found"   # latched, NOT un-latched
    assert ps["vars"]["nodeip"]["value"] == "notfound"   # no fresh lease
    assert ps["vars"]["nodeping"]["value"] == "fail"     # live, off
    assert ps["vars"]["nodessh"]["value"] == "fail"      # live, off


def test_node_no_cross_gating_inventory_found_despite_nodeping_fail():
    """Explicit: inventory is NOT forced pending by an upstream failing stage."""
    from flax_observe.state_machine import port_worker_one_iter
    ps = _pipeline_port_state()
    env = _pipeline_env(ping_host=lambda ip: "fail")   # nodeping fail
    port_worker_one_iter(ps, _FACT_ONE_MAC, lambda e: None, env)
    assert ps["vars"]["nodeping"]["value"] == "fail"
    assert ps["vars"]["inventory"]["value"] == "found"   # NOT gated by nodeping


def test_node_session_relative_stages_reset_after_poweron():
    """nodeip (fresh lease) + nodepxe (this-session fetch) reset to notfound
    after a power-on (no fresh signal yet); inventory notfound when the post
    file predates the advanced boundary."""
    from flax_observe.state_machine import port_worker_one_iter
    ps = _pipeline_port_state()
    env = _pipeline_env(
        kea_lease_fresh=lambda m, b: False,
        nginx_pxe_seen=lambda *a, **k: "notfound",
        inventory_status=lambda *a, **k: "notfound",
        ping_host=lambda ip: "fail", ssh_uptime=lambda *a, **k: "fail",
    )
    port_worker_one_iter(ps, _FACT_ONE_MAC, lambda e: None, env)
    assert ps["vars"]["nodeip"]["value"] == "notfound"
    assert ps["vars"]["nodepxe"]["value"] == "notfound"
    assert ps["vars"]["inventory"]["value"] == "notfound"


def test_node_nic_ip_resolves_while_nodeip_notfound():
    from flax_observe.state_machine import port_worker_one_iter
    ps = _pipeline_port_state()
    env = _pipeline_env(kea_lease_fresh=lambda mac, boundary: False)
    port_worker_one_iter(ps, _FACT_ONE_MAC, lambda e: None, env)
    assert ps["nic_ip"] == "172.16.7.2"
    assert ps["vars"]["nodeip"]["value"] == "notfound"


def test_node_no_nic_mac_all_unknown():
    from flax_observe.state_machine import port_worker_one_iter
    ps = _pipeline_port_state()
    facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "link", "macs": [], "lldp_neighbors": []}}
    port_worker_one_iter(ps, facts, lambda e: None, _pipeline_env())
    for stage in ["nodeip", "nodeping", "nodepxe", "nodessh", "inventory"]:
        assert ps["vars"][stage]["value"] == "unknown", stage


# ---------------------------------------------------------------------------
# forget-port sentinel reader tests
# ---------------------------------------------------------------------------

def test_forget_port_requested_consumes_sentinel(tmp_path):
    """Presence == request; the reader unlinks the file (consume-once) and
    returns True, then False on the next read."""
    from flax_observe.state_machine import _forget_port_requested

    d = tmp_path / "forget-port"
    d.mkdir()
    (d / "et6b1").write_text("")

    assert _forget_port_requested("et6b1", forget_port_dir=str(d)) is True
    assert not (d / "et6b1").exists(), "must unlink (consume-once)"
    assert _forget_port_requested("et6b1", forget_port_dir=str(d)) is False


def test_forget_port_requested_absent_is_false(tmp_path):
    from flax_observe.state_machine import _forget_port_requested
    assert _forget_port_requested("et9b9", forget_port_dir=str(tmp_path)) is False


# ---------------------------------------------------------------------------
# Vendor-routed power/watts transport (spec 2026-09-18 phase 2, task 6).
#
# THE BUG THIS FIXES: a Phosphor board classified 'openbmc' took the ssh
# branch, which reads power state and never the SDR -- so watts stayed None and
# the Triage power button showed '?' on 33 of 33 eindhoven ports (2026-09-18).
# Phosphor speaks IPMI (measured: MB HSC In Power | 141.60 Watts), so power +
# watts route on the vendor capability. The SERIAL source does NOT change: a
# vendor with ssh keeps the ssh FRU-0 chain (ipmitool `fru` over LAN reads all
# FRU devices and can pick a non-baseboard Product Serial).
# ---------------------------------------------------------------------------

def test_power_transport_truth_table():
    from flax_observe.state_machine import _power_transport
    from flax_observe import bmc_vendor as _bv

    # A known vendor routes on its capability; kind is irrelevant.
    for kind in ("openbmc", "traditional", "redfish", "unknown"):
        assert _power_transport(_bv.PHOSPHOR, kind) == "ipmi", kind
        assert _power_transport(_bv.AMI_LEGACY, kind) == "ipmi", kind
        assert _power_transport(_bv.FACEBOOK, kind) == "ssh", kind

    # UNKNOWN / missing vendor: legacy fallback on the frozen kind label, so a
    # board whose vendor could not be read keeps today's exact behaviour.
    for vendor in (_bv.UNKNOWN, None, "some-future-value"):
        assert _power_transport(vendor, "openbmc") == "ssh", vendor
        assert _power_transport(vendor, "traditional") == "ipmi", vendor
        assert _power_transport(vendor, "redfish") is None, vendor
        assert _power_transport(vendor, "unknown") is None, vendor
        assert _power_transport(vendor, None) is None, vendor


def test_serial_via_ssh_truth_table():
    from flax_observe.state_machine import _serial_via_ssh
    from flax_observe import bmc_vendor as _bv

    for kind in ("openbmc", "traditional"):
        assert _serial_via_ssh(_bv.PHOSPHOR, kind) is True
        assert _serial_via_ssh(_bv.FACEBOOK, kind) is True
        assert _serial_via_ssh(_bv.AMI_LEGACY, kind) is False
    for vendor in (_bv.UNKNOWN, None):
        assert _serial_via_ssh(vendor, "openbmc") is True
        assert _serial_via_ssh(vendor, "traditional") is False
        assert _serial_via_ssh(vendor, "redfish") is False


_VENDOR_MAC = "aa:bb:cc:dd:ee:ff"
_VENDOR_BMC_IP = "172.17.6.104"


def _vendor_facts():
    return {("rabbit-lorax", "Et6/1"): {
        "linkstate": "connected", "macs": [_VENDOR_MAC],
        "lldp_neighbors": []}}


def _vendor_env(kind, vendor, calls, ipmi=("on", "141.60 W"),
                ssh_pwr="on", ssh_sn="SSH-SN", ipmi_sn="LAN-SN",
                sol_active=False, creds=("root", "x")):
    """Env whose readers record every call into `calls` (a list of names)."""
    probe = {"kind": kind, "product_name": "Type6 Tioga Pass Single Side",
             "creds_used": creds}
    if vendor is not None:
        probe["vendor"] = vendor

    def _rec(name, value):
        def _f(*a, **k):
            calls.append(name)
            return value
        return _f

    return _stub_env(
        lookup_lease_ip=lambda *a, **k: _VENDOR_BMC_IP,
        ping_host=lambda *a, **k: "ok",
        probe_bmc_kind=lambda *a, **k: dict(probe),
        bmc_power_and_sdr_traditional=_rec("ipmi_power", ipmi),
        bmc_power_status_openbmc=_rec("ssh_power", ssh_pwr),
        chassis_serial_openbmc=_rec("ssh_serial", ssh_sn),
        chassis_serial_traditional=_rec("ipmi_serial", ipmi_sn),
        sol_session_active=lambda *a, **k: sol_active,
    )


def test_phosphor_reads_power_and_watts_over_ipmi_serial_over_ssh():
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv

    calls = []
    ps = _initial_port_state()
    port_worker_one_iter(ps, _vendor_facts(), emit_event=[].append,
                         env=_vendor_env("openbmc", _bv.PHOSPHOR, calls))

    assert "ipmi_power" in calls
    assert "ssh_power" not in calls
    assert ps["vars"]["bmcpower"]["value"] == "on"
    assert ps["bmc_power"] == "141.60 W"
    # Serial source unchanged: the ssh FRU-0 chain, not LAN `ipmitool fru`.
    assert "ssh_serial" in calls
    assert "ipmi_serial" not in calls
    assert ps["chassis_sn"] == "SSH-SN"


def test_phosphor_serial_falls_back_to_lan_walk_when_ssh_fru_empty():
    """The openbmc branch's bmc_creds IPMI serial walk is kept for phosphor."""
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv

    calls = []
    env = _vendor_env("openbmc", _bv.PHOSPHOR, calls, ssh_sn=None)
    env.bmc_credentials = [{"bmcuser": "admin", "bmcpass": "y"}]
    ps = _initial_port_state()
    port_worker_one_iter(ps, _vendor_facts(), emit_event=[].append, env=env)

    assert calls.count("ipmi_serial") == 1
    assert ps["chassis_sn"] == "LAN-SN"


def test_phosphor_sol_active_keeps_its_short_ipmi_session():
    """Spec 2026-09-28 §3.4: after the SDR cache a phosphor read is one short
    IPMI session, so a live SOL no longer moves it to ssh power / no watts."""
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv

    calls, events = [], []
    ps = _initial_port_state()
    port_worker_one_iter(ps, _vendor_facts(), emit_event=events.append,
                         env=_vendor_env("openbmc", _bv.PHOSPHOR, calls, sol_active=True))
    assert "ipmi_power" in calls
    assert "ssh_power" not in calls
    assert ps["bmc_power"] == "141.60 W"
    assert not any(e.get("kind") == "sol_active_skip" for e in events)
    assert ps["chassis_sn"] == "SSH-SN"


def test_phosphor_link_local_only_reads_power_over_ssh_not_ipmi():
    """No IPv4 lease (bmc_ip None), reach is IPv6 link-local: IPMI has no
    target, so phosphor reads power over ssh on the LL probe_host."""
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv

    calls = []
    power_hosts = []
    env = _vendor_env("openbmc", _bv.PHOSPHOR, calls, ssh_pwr="on", sol_active=True)
    env.lookup_lease_ip = lambda *a, **k: None
    env.vlan_parents = {26: "eth0"}
    env.ping6_reachable = lambda addr, timeout=2: True

    def _ssh_power(host, *a, **k):
        calls.append("ssh_power")
        power_hosts.append(host)
        return "on"
    env.bmc_power_status_openbmc = _ssh_power

    facts = {("rabbit-lorax", "Et6/1"): {
        "linkstate": "connected", "macs": [_LL_MAC],
        "lldp_neighbors": [], "access_vid": 26}}
    ps = _initial_port_state()
    port_worker_one_iter(ps, facts, emit_event=[].append, env=env)

    assert ps["bmc_ip"] is None
    assert "ipmi_power" not in calls
    assert power_hosts == [_LL_ADDR + "%eth0.26"]
    assert ps["vars"]["bmcpower"]["value"] == "on"


def _ami_sol_env(calls, now, ipmi=("off", None)):
    from flax_observe import bmc_vendor as _bv
    env = _vendor_env("traditional", _bv.AMI_LEGACY, calls, ipmi=ipmi, sol_active=True)
    env.monotonic = lambda: now[0]
    return env


def test_ami_legacy_sol_active_skips_within_the_refresh_window():
    """ami_legacy has no ssh: SOL active -> skip (event, no reader, bmcpower
    held) while the last SOL-live power refresh is < 180 s old."""
    from flax_observe.state_machine import port_worker_one_iter

    calls, events, now = [], [], [1000.0]
    ps = _initial_port_state()
    ps["vars"]["bmcpower"] = {"value": "on", "since": "2026-01-01T00:00:00Z"}
    ps["sol_power_refreshed_at"] = 1000.0 - 179
    port_worker_one_iter(ps, _vendor_facts(), emit_event=events.append,
                         env=_ami_sol_env(calls, now))

    assert calls == []
    assert ps["vars"]["bmcpower"]["value"] == "on"
    assert any(e.get("kind") == "sol_active_skip" for e in events)


def test_ami_legacy_sol_active_refreshes_power_every_180s():
    """operator 2026-10-27: triage holds SOL on every Leopard tile, so the skip
    froze bmcpower for ever (powered off, UI said on). One power read per
    180 s even while SOL is live -- session exhaustion is acceptable on
    ami_legacy."""
    from flax_observe.state_machine import port_worker_one_iter

    calls, events, now = [], [], [1000.0]
    ps = _initial_port_state()
    ps["vars"]["bmcpower"] = {"value": "on", "since": "2026-01-01T00:00:00Z"}
    env = _ami_sol_env(calls, now)

    port_worker_one_iter(ps, _vendor_facts(), emit_event=events.append, env=env)
    assert calls == ["ipmi_power"]
    assert ps["vars"]["bmcpower"]["value"] == "off"
    assert any(e.get("kind") == "sol_active_power_refresh" for e in events)

    now[0] += 60
    port_worker_one_iter(ps, _vendor_facts(), emit_event=events.append, env=env)
    assert calls == ["ipmi_power"]

    now[0] += 120
    port_worker_one_iter(ps, _vendor_facts(), emit_event=events.append, env=env)
    assert calls == ["ipmi_power", "ipmi_power"]


def test_unknown_vendor_traditional_sol_active_keeps_the_skip():
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv

    calls = []
    events = []
    ps = _initial_port_state()
    ps["vars"]["bmcpower"] = {"value": "off", "since": "2026-01-01T00:00:00Z"}
    port_worker_one_iter(ps, _vendor_facts(), emit_event=events.append,
                         env=_vendor_env("traditional", _bv.UNKNOWN, calls,
                                         sol_active=True))

    assert calls == []
    assert ps["vars"]["bmcpower"]["value"] == "off"
    assert any(e.get("kind") == "sol_active_skip" for e in events)


def test_facebook_reads_power_over_ssh_no_ipmi_no_watts():
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv

    calls = []
    ps = _initial_port_state()
    port_worker_one_iter(ps, _vendor_facts(), emit_event=[].append,
                         env=_vendor_env("openbmc", _bv.FACEBOOK, calls,
                                         ssh_pwr="off"))

    assert "ssh_power" in calls
    assert "ipmi_power" not in calls
    assert ps["vars"]["bmcpower"]["value"] == "off"
    assert ps["bmc_power"] == "?"          # watts None -> '?'
    assert "ssh_serial" in calls
    assert ps["chassis_sn"] == "SSH-SN"


def test_ami_legacy_reads_ipmi_power_and_traditional_serial_with_latch():
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv

    calls = []
    env = _vendor_env("traditional", _bv.AMI_LEGACY, calls,
                      ipmi=("on", "120 W"))
    ps = _initial_port_state()
    port_worker_one_iter(ps, _vendor_facts(), emit_event=[].append, env=env)

    assert calls.count("ipmi_power") == 1
    assert calls.count("ipmi_serial") == 1
    assert "ssh_power" not in calls
    assert "ssh_serial" not in calls
    assert ps["bmc_power"] == "120 W"
    assert ps["chassis_sn"] == "LAN-SN"

    # Second cycle: serial latched -> no refetch (Mitigation 2).
    del calls[:]
    port_worker_one_iter(ps, _vendor_facts(), emit_event=[].append, env=env)
    assert calls.count("ipmi_power") == 1
    assert "ipmi_serial" not in calls
    assert ps["chassis_sn"] == "LAN-SN"


def test_unknown_vendor_openbmc_kind_keeps_legacy_ssh_path():
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv

    calls = []
    ps = _initial_port_state()
    port_worker_one_iter(ps, _vendor_facts(), emit_event=[].append,
                         env=_vendor_env("openbmc", _bv.UNKNOWN, calls))

    assert "ssh_power" in calls
    assert "ipmi_power" not in calls
    assert "ssh_serial" in calls
    assert ps["vars"]["bmcpower"]["value"] == "on"


def test_unknown_vendor_redfish_kind_reads_nothing():
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv

    calls = []
    ps = _initial_port_state()
    port_worker_one_iter(ps, _vendor_facts(), emit_event=[].append,
                         env=_vendor_env("redfish", _bv.UNKNOWN, calls))

    assert calls == []
    assert ps["vars"]["bmcpower"]["value"] == "unknown"


def test_phosphor_without_creds_reads_nothing():
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv

    calls = []
    ps = _initial_port_state()
    port_worker_one_iter(ps, _vendor_facts(), emit_event=[].append,
                         env=_vendor_env("openbmc", _bv.PHOSPHOR, calls,
                                         creds=None))

    assert calls == []
    assert ps["vars"]["bmcpower"]["value"] == "unknown"


# --- SDR-stall fallback (fix round 2; et7b2 2026-09-18) --------------------

def test_phosphor_ipmi_unknown_falls_back_to_ssh_power():
    """et7b2: the combined power+SDR exec stalls in `sdr` past the runner
    timeout -> ("unknown", None). Phosphor has ssh, so power is re-read there;
    watts stay None."""
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv

    calls = []
    ps = _initial_port_state()
    port_worker_one_iter(ps, _vendor_facts(), emit_event=[].append,
                         env=_vendor_env("openbmc", _bv.PHOSPHOR, calls,
                                         ipmi=("unknown", None), ssh_pwr="on"))

    assert calls.count("ipmi_power") == 1
    assert calls.count("ssh_power") == 1
    assert ps["vars"]["bmcpower"]["value"] == "on"
    assert ps["bmc_power"] == "?"          # watts None


def test_phosphor_ipmi_success_makes_no_ssh_power_call():
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv

    calls = []
    ps = _initial_port_state()
    port_worker_one_iter(ps, _vendor_facts(), emit_event=[].append,
                         env=_vendor_env("openbmc", _bv.PHOSPHOR, calls,
                                         ipmi=("on", "141.60 W")))

    assert "ssh_power" not in calls
    assert ps["bmc_power"] == "141.60 W"


def test_ami_legacy_ipmi_unknown_has_no_ssh_fallback():
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv

    calls = []
    ps = _initial_port_state()
    port_worker_one_iter(ps, _vendor_facts(), emit_event=[].append,
                         env=_vendor_env("traditional", _bv.AMI_LEGACY, calls,
                                         ipmi=("unknown", None)))

    assert "ssh_power" not in calls
    assert ps["vars"]["bmcpower"]["value"] == "unknown"


# --- FRU ID 0 only (spec 2026-09-14-fru-id0-only §6.2) ------------------

def _events(kind, events):
    return [e for e in events if e.get("kind") == kind]


def test_fru0_ssh_no_serial_clears_the_latch_without_a_lan_fallback():
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv
    calls, events = [], []
    env = _vendor_env("openbmc", _bv.PHOSPHOR, calls, ssh_sn=(None, "no_serial"))
    env.bmc_credentials = [{"bmcuser": "admin", "bmcpass": "y"}]
    ps = _initial_port_state()
    ps["chassis_sn"] = "WTH-OLD"
    port_worker_one_iter(ps, _vendor_facts(), emit_event=events.append, env=env)
    assert "ipmi_serial" not in calls
    assert ps["chassis_sn"] is None and ps["vars"]["chassissn"]["value"] == "notfound"
    assert [(e["old"], e["state"]) for e in _events("chassis_sn_cleared", events)] == [("WTH-OLD", "no_serial")]


def test_fru0_ssh_absent_falls_back_to_the_lan_fru0_read():
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv
    calls = []
    env = _vendor_env("openbmc", _bv.PHOSPHOR, calls, ssh_sn=(None, "absent"),
                      ipmi_sn=("WTH49152ZGM1A", "ok"))
    env.bmc_credentials = [{"bmcuser": "admin", "bmcpass": "y"}]
    ps = _initial_port_state()
    port_worker_one_iter(ps, _vendor_facts(), emit_event=[].append, env=env)
    assert calls.count("ipmi_serial") == 1 and ps["chassis_sn"] == "WTH49152ZGM1A"


def test_fru0_ssh_and_lan_absent_keep_the_latch():
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv
    calls, events = [], []
    env = _vendor_env("openbmc", _bv.PHOSPHOR, calls, ssh_sn=(None, "absent"), ipmi_sn=(None, "absent"))
    env.bmc_credentials = [{"bmcuser": "admin", "bmcpass": "y"}]
    ps = _initial_port_state()
    ps["chassis_sn"] = "WTH-KEEP"
    port_worker_one_iter(ps, _vendor_facts(), emit_event=events.append, env=env)
    assert ps["chassis_sn"] == "WTH-KEEP" and ps["vars"]["chassissn"]["value"] == "found"
    assert not _events("chassis_sn_cleared", events)


def test_fru0_nic_only_lan_dump_never_becomes_chassis_sn(monkeypatch):
    """End to end through the real LAN reader: ssh says absent, LAN answers a
    dump whose only serials are the NIC's and the M.2 carrier's."""
    import pathlib
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_probe, bmc_vendor as _bv
    text = (pathlib.Path(__file__).parent / "fixtures" / "fru" / "id0_absent_limit_nic_ava.txt").read_text()
    monkeypatch.setattr(bmc_probe, "_sleep", lambda s: None)
    monkeypatch.setattr(bmc_probe, "_default_ipmi_runner", lambda *a, **k: text)
    monkeypatch.setattr(bmc_probe._fm_live, "current", lambda path=None: {})
    calls = []
    env = _vendor_env("openbmc", _bv.PHOSPHOR, calls, ssh_sn=(None, "absent"))
    env.chassis_serial_traditional = bmc_probe.chassis_serial_traditional
    env.bmc_credentials = [{"bmcuser": "admin", "bmcpass": "y"}]
    ps = _initial_port_state()
    port_worker_one_iter(ps, _vendor_facts(), emit_event=[].append, env=env)
    assert ps["chassis_sn"] is None


def test_fru0_ssh_relatch_emits_an_event():
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv
    events = []
    ps = _initial_port_state()
    ps["chassis_sn"] = "1I02270LSS"                      # a NIC serial latched by the old walk
    port_worker_one_iter(ps, _vendor_facts(), emit_event=events.append,
                         env=_vendor_env("openbmc", _bv.PHOSPHOR, [], ssh_sn=("WTH49152ZGM1A", "ok")))
    assert ps["chassis_sn"] == "WTH49152ZGM1A"
    assert [(e["old"], e["new"]) for e in _events("chassis_sn_relatched", events)] == [("1I02270LSS", "WTH49152ZGM1A")]


def test_fru0_ipmi_latch_is_reverified_once_per_start_then_skipped():
    """D8: a latch hydrated at start is read once; a different value relatches."""
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv
    calls, events = [], []
    env = _vendor_env("traditional", _bv.AMI_LEGACY, calls, ipmi_sn=("QCFF0617260650", "ok"))
    ps = _initial_port_state()
    ps["chassis_sn"] = "1I02270LPB"                      # hydrated from observe_state, no flag
    port_worker_one_iter(ps, _vendor_facts(), emit_event=events.append, env=env)
    assert calls.count("ipmi_serial") == 1
    assert ps["chassis_sn"] == "QCFF0617260650" and ps["chassis_sn_verified"] is True
    assert len(_events("chassis_sn_relatched", events)) == 1
    del calls[:]
    port_worker_one_iter(ps, _vendor_facts(), emit_event=events.append, env=env)
    assert "ipmi_serial" not in calls                    # verified latch -> Mitigation 2 skip
    assert len(_events("chassis_sn_relatched", events)) == 1


def test_fru0_ipmi_same_value_sets_the_flag_without_an_event():
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv
    events = []
    ps = _initial_port_state()
    ps["chassis_sn"] = "QCFF0617260650"
    port_worker_one_iter(ps, _vendor_facts(), emit_event=events.append,
                         env=_vendor_env("traditional", _bv.AMI_LEGACY, [], ipmi_sn=("QCFF0617260650", "ok")))
    assert ps["chassis_sn_verified"] is True and not _events("chassis_sn_relatched", events)


def test_fru0_ipmi_no_serial_clears_and_sets_the_flag():
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv
    events = []
    ps = _initial_port_state()
    ps["chassis_sn"] = "1I02270LPB"
    port_worker_one_iter(ps, _vendor_facts(), emit_event=events.append,
                         env=_vendor_env("traditional", _bv.AMI_LEGACY, [], ipmi_sn=(None, "no_serial")))
    assert ps["chassis_sn"] is None and ps["chassis_sn_verified"] is True
    assert len(_events("chassis_sn_cleared", events)) == 1


def test_fru0_ipmi_absent_or_error_keeps_the_latch_and_retries_next_cycle():
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv
    for result in ((None, "absent"), (None, "error"), None):
        calls = []
        env = _vendor_env("traditional", _bv.AMI_LEGACY, calls, ipmi_sn=result)
        ps = _initial_port_state()
        ps["chassis_sn"] = "QCFF0617260650"
        port_worker_one_iter(ps, _vendor_facts(), emit_event=[].append, env=env)
        port_worker_one_iter(ps, _vendor_facts(), emit_event=[].append, env=env)
        assert ps["chassis_sn"] == "QCFF0617260650"
        assert not ps.get("chassis_sn_verified")
        assert calls.count("ipmi_serial") == 2, result


def test_fru0_ipmi_sol_active_reads_nothing_and_leaves_the_flag_unset():
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv
    calls = []
    ps = _initial_port_state()
    ps["chassis_sn"] = "QCFF0617260650"
    port_worker_one_iter(ps, _vendor_facts(), emit_event=[].append,
                         env=_vendor_env("traditional", _bv.AMI_LEGACY, calls, sol_active=True,
                                         ipmi_sn=("OTHER", "ok")))
    # SOL live: no FRU 0 serial read (latch held, not re-verified). The one
    # power read per 180 s (operator 2026-10-27) is allowed and is not a serial read.
    assert "ipmi_serial" not in calls
    assert calls in ([], ["ipmi_power"])
    assert ps["chassis_sn"] == "QCFF0617260650" and not ps.get("chassis_sn_verified")


def test_fru0_verified_flag_is_not_persisted(monkeypatch):
    """Behavioural (pre-flight F7): drive the real PortWorker._persist with the
    D8 flag set and assert nothing it hands to upsert_observe_state carries it.
    The flag is in-memory only, so every restart re-verifies a hydrated latch."""
    import json
    import types
    from flax_observe import port_worker as pw
    env = types.SimpleNamespace()
    cache = types.SimpleNamespace(get_port=lambda *a, **k: {})
    w = pw.PortWorker("rabbit-lorax", "et6b1", "20", cache, env,
                      cycle_secs=10.0, prior=None)
    w.port_state["chassis_sn"] = "QCFF0617260650"
    w.port_state["chassis_sn_verified"] = True
    captured = {}

    def fake_upsert(**kw):
        captured.update(kw)
        return 1

    monkeypatch.setattr(pw, "upsert_observe_state", fake_upsert)
    w._persist()
    assert captured["resolved"]["chassis_sn"] == "QCFF0617260650"   # the latch IS written
    assert "chassis_sn_verified" not in json.dumps(captured, default=str)


# ---------------------------------------------------------------------------
# Part 2 / B: identity hold + serial confirmation
# ---------------------------------------------------------------------------
HOLD_OLD = "b8:59:9f:dd:ea:2a"
HOLD_NEW = "b8:59:9f:dd:ea:2b"
HOLD_NIC_OLD = "b8:59:9f:dd:ea:28"


def _hold_env(serials, kinds=None, **over):
    """serials: {target: (serial, state)}; target is the IPv4 from
    _ip_for_mac (the reach path when ping answers)."""
    kinds = kinds or {}
    calls = {"serial": [], "kind": []}

    def _kind(host, *a, **k):
        calls["kind"].append(host)
        return kinds.get(host, {"kind": "openbmc", "vendor": "phosphor",
                                "product_name": "Tioga Pass",
                                "creds_used": ("root", "t")})

    def _sn(host, *a, **k):
        calls["serial"].append(host)
        return serials.get(host, (None, "error"))

    env = _stub_env(
        lookup_lease_ip=lambda path, mac, d: _ip_for_mac(mac),
        probe_bmc_kind=_kind,
        chassis_serial_openbmc=_sn,
        chassis_serial_traditional=lambda *a, **k: (None, "error"),
        ping_host=lambda *a, **k: "ok",
        bmc_power_status_openbmc=lambda *a, **k: "on",
    )
    for k, v in over.items():
        setattr(env, k, v)
    return env, calls


def _facts(macs, vid=17):
    return {("rabbit-lorax", "Et6/1"): {"linkstate": "connected", "macs": macs,
                                        "lldp_neighbors": [], "access_vid": vid}}


def _latched_state(env):
    """One live cycle with the old BMC -> identity latched."""
    from flax_observe.state_machine import port_worker_one_iter
    ps = _initial_port_state()
    port_worker_one_iter(ps, _facts([HOLD_OLD]), [].append, env)
    assert ps["bmc_mac"] == HOLD_OLD and ps["chassis_sn"] == "SN1"
    assert ps["bmc_mac_latched"] == HOLD_OLD
    return ps


def test_empty_fdb_holds_identity():
    from flax_observe.state_machine import port_worker_one_iter
    env, calls = _hold_env({_ip_for_mac(HOLD_OLD): ("SN1", "ok")})
    ps = _latched_state(env)
    nic_before = ps["nic_mac"]
    events = []
    port_worker_one_iter(ps, _facts([]), events.append, env)
    assert ps["bmc_mac"] == HOLD_OLD
    assert ps["nic_mac"] == nic_before
    assert ps["chassis_sn"] == "SN1"
    assert ps["role_source"] == "held"
    assert ps["vars"]["bmcmac"]["value"] == "held"
    assert ps["bmc_mac_held_since"] is not None
    assert ps["bmc_kind_cached"]["for_mac"] == HOLD_OLD   # not popped
    # R6: no BMC probe while held
    n = len(calls["serial"])
    port_worker_one_iter(ps, _facts([]), [].append, env)
    assert len(calls["serial"]) == n
    assert ps["vars"]["chassissn"]["value"] == "found"


def test_hold_expiry_is_todays_path():
    from flax_observe.state_machine import port_worker_one_iter
    env, _ = _hold_env({_ip_for_mac(HOLD_OLD): ("SN1", "ok")})
    ps = _latched_state(env)
    port_worker_one_iter(ps, _facts([]), [].append, env)
    ps["bmc_mac_held_since"] = _iso_ago(901)
    port_worker_one_iter(ps, _facts([]), [].append, env)
    assert ps["bmc_mac"] is None
    assert ps["vars"]["bmcmac"]["value"] == "notfound"
    assert ps["chassis_sn"] == "SN1"          # R3: kept for the tile
    assert ps["bmc_mac_latched"] == HOLD_OLD  # R4 anchor survives


def test_same_mac_returns_live_and_clears_hold():
    from flax_observe.state_machine import port_worker_one_iter
    env, _ = _hold_env({_ip_for_mac(HOLD_OLD): ("SN1", "ok")})
    ps = _latched_state(env)
    port_worker_one_iter(ps, _facts([]), [].append, env)
    port_worker_one_iter(ps, _facts([HOLD_OLD]), [].append, env)
    assert ps["vars"]["bmcmac"]["value"] == "found"
    assert ps["bmc_mac_held_since"] is None
    assert ps["role_source"] != "held"


def test_flip_same_serial_adopts_new_mac_and_rederives_nic():
    from flax_observe.state_machine import port_worker_one_iter
    env, _ = _hold_env({_ip_for_mac(HOLD_OLD): ("SN1", "ok"),
                        _ip_for_mac(HOLD_NEW): ("SN1", "ok")})
    ps = _latched_state(env)
    events = []
    port_worker_one_iter(ps, _facts([HOLD_NEW]), events.append, env)
    assert ps["bmc_mac"] == HOLD_NEW
    assert ps["bmc_mac_latched"] == HOLD_NEW
    assert ps["chassis_sn"] == "SN1"
    assert ps["nic_mac"] != HOLD_NIC_OLD          # re-derived, never carried
    assert ps["nic_mac"] == "b8:59:9f:dd:ea:29"   # single-MAC path: new - 2
    flips = [e for e in events if e.get("kind") == "bmc_mac_flipped"]
    assert flips == [{"kind": "bmc_mac_flipped", "switch": "rabbit-lorax",
                      "port": "Et6/1", "old": HOLD_OLD, "new": HOLD_NEW,
                      "serial": "SN1"}]
    assert ps["bmc_mac_held_since"] is None


def test_swap_different_serial_forgets_then_first_sight():
    from flax_observe.state_machine import port_worker_one_iter
    env, _ = _hold_env({_ip_for_mac(HOLD_OLD): ("SN1", "ok"),
                        _ip_for_mac(HOLD_NEW): ("SN2", "ok")})
    ps = _latched_state(env)
    events = []
    port_worker_one_iter(ps, _facts([HOLD_NEW]), events.append, env)
    assert ps["bmc_mac"] == HOLD_NEW
    assert ps["chassis_sn"] == "SN2"
    assert [e["kind"] for e in events if e.get("kind") == "bmc_identity_swapped"] \
        == ["bmc_identity_swapped"]


def test_unreadable_new_mac_holds_then_swaps_at_expiry():
    from flax_observe.state_machine import port_worker_one_iter
    env, calls = _hold_env({_ip_for_mac(HOLD_OLD): ("SN1", "ok")})
    ps = _latched_state(env)
    port_worker_one_iter(ps, _facts([HOLD_NEW]), [].append, env)
    assert ps["bmc_mac"] == HOLD_OLD and ps["role_source"] == "held"
    ps["bmc_mac_held_since"] = _iso_ago(901)
    events = []
    port_worker_one_iter(ps, _facts([HOLD_NEW]), events.append, env)
    assert ps["bmc_mac"] == HOLD_NEW
    assert ps["chassis_sn"] != "SN1"
    assert any(e.get("kind") == "bmc_identity_swapped" for e in events)


def test_host_mac_alone_during_bmc_reboot_keeps_holding():
    """Review Focus 1: the host NIC stays up while the BMC reboots; its MAC is
    the only FDB entry and the single-MAC path labels it the BMC. It is not a
    confirmed BMC, so no serial is read and it is never adopted."""
    from flax_observe.state_machine import port_worker_one_iter
    host = HOLD_NIC_OLD
    env, calls = _hold_env({_ip_for_mac(HOLD_OLD): ("SN1", "ok")},
                           kinds={_ip_for_mac(host): {"kind": "unknown",
                                                      "creds_used": None}})
    ps = _latched_state(env)
    n = len(calls["serial"])
    port_worker_one_iter(ps, _facts([host]), [].append, env)
    assert ps["bmc_mac"] == HOLD_OLD and ps["role_source"] == "held"
    assert len(calls["serial"]) == n
    port_worker_one_iter(ps, _facts([HOLD_OLD, host]), [].append, env)
    assert ps["bmc_mac"] == HOLD_OLD and ps["vars"]["bmcmac"]["value"] == "found"
    assert ps["chassis_sn"] == "SN1"


def test_host_mac_alone_past_hold_expiry_is_never_adopted():
    """lavergne 2026-10-28 (et29b1 et al): the BMC stayed mute for the whole
    inventory pass (> 900 s) with only the host NIC on the port. The hold
    expired and the host NIC was swapped in as the BMC, so classify moved the
    BMC reservation onto the host. Past expiry it must still be held."""
    from flax_observe.state_machine import port_worker_one_iter
    host = HOLD_NIC_OLD
    env, calls = _hold_env({_ip_for_mac(HOLD_OLD): ("SN1", "ok")},
                           kinds={_ip_for_mac(host): {"kind": "unknown",
                                                      "creds_used": None}})
    ps = _latched_state(env)
    nic_before = ps["nic_mac"]
    port_worker_one_iter(ps, _facts([host]), [].append, env)
    ps["bmc_mac_held_since"] = _iso_ago(3600)
    events = []
    port_worker_one_iter(ps, _facts([host]), events.append, env)
    assert ps["bmc_mac"] == HOLD_OLD and ps["role_source"] == "held"
    assert ps["nic_mac"] == nic_before
    assert ps["chassis_sn"] == "SN1" and ps["bmc_mac_latched"] == HOLD_OLD
    assert not any(e.get("kind") == "bmc_identity_swapped" for e in events)
    # the BMC comes back: live again, hold cleared
    port_worker_one_iter(ps, _facts([HOLD_OLD, host]), [].append, env)
    assert ps["bmc_mac"] == HOLD_OLD and ps["vars"]["bmcmac"]["value"] == "found"
    assert ps["bmc_mac_held_since"] is None


def test_dup_mac_on_other_port_is_not_probed_or_adopted():
    """Review Focus 3."""
    from flax_observe.state_machine import port_worker_one_iter
    env, calls = _hold_env({_ip_for_mac(HOLD_OLD): ("SN1", "ok"),
                            _ip_for_mac(HOLD_NEW): ("SN1", "ok")},
                           mac_ports=lambda mac: ({("rabbit-lorax", "Ethernet6/1"),
                                                   ("rabbit-lorax", "Ethernet7/1")}
                                                  if mac == HOLD_NEW else set()))
    ps = _latched_state(env)
    n = len(calls["serial"])
    port_worker_one_iter(ps, _facts([HOLD_NEW]), [].append, env)
    assert ps["bmc_mac"] == HOLD_OLD and ps["role_source"] == "held"
    assert len(calls["serial"]) == n


def test_committed_link_down_clears_hold_keys():
    from flax_observe.state_machine import _forget_identity
    ps = _initial_port_state()
    ps["bmc_mac_latched"] = HOLD_OLD
    ps["bmc_mac_held_since"] = _iso_ago(10)
    ps["identity_probe"] = {"mac": HOLD_NEW, "at": _iso_ago(5), "serial": None}
    _forget_identity(ps, [].append)
    assert ps["bmc_mac_latched"] is None
    assert ps["bmc_mac_held_since"] is None
    assert ps["identity_probe"] is None


def test_traditional_mac_change_on_legacy_row_rereads_serial():
    """Review Focus 5 / R4 (latent bug, report §7.4): a port hydrated from a
    pre-Part-2 row -- serial latched, bmc_mac None (the old code nulled it on
    an empty FDB), no bmc_mac_latched, no kind cache. A DIFFERENT traditional
    BMC appears whose serial read fails. The old role_mac_changed compared
    against the (absent) cache -> False -> the latch branch kept 'SN1' for a
    different chassis. R4: no anchor + a latched serial = re-read."""
    from flax_observe.state_machine import port_worker_one_iter
    trad = {"kind": "traditional", "vendor": "ami_legacy", "product_name": "X",
            "creds_used": ("admin", "t")}
    env, _ = _hold_env({}, kinds={_ip_for_mac(HOLD_NEW): trad},
                       chassis_serial_traditional=lambda ip, c, **k: (None, "error"),
                       bmc_power_and_sdr_traditional=lambda *a, **k: ("on", "100 W"))
    ps = _initial_port_state()
    ps["vars"]["linkstate"] = {"value": "link", "since": _iso_ago(3600)}
    ps["link_session_since"] = _iso_ago(3600)
    ps["chassis_sn"] = "SN1"
    ps["bmc_mac_latched"] = None
    port_worker_one_iter(ps, _facts([HOLD_NEW]), [].append, env)
    assert ps["bmc_mac"] == HOLD_NEW
    assert ps["chassis_sn"] != "SN1", "old chassis serial carried to a new BMC"


# ---------------------------------------------------------------------------
# CLEANUP 3: a BMC kind probed right after the BMC appears is provisional.
#
# Live 2026-09-28 (et8b3): a re-seated phosphor OpenBMC answered IPMI before
# its ssh was up, observe's first probe cached traditional/ami_legacy, and a
# traditional kind with a product_name was never re-probed -> bmc_fw faulted
# the board as legacy/OEM. The early probe is now marked and re-probed once.
# ---------------------------------------------------------------------------

def _early_env(results):
    env, calls = _sequenced_probe_env(results)
    env.chassis_serial_openbmc = lambda *a, **k: "SN-EARLY"
    env.chassis_serial_traditional = lambda *a, **k: "SN-EARLY"
    return env, calls


_EARLY_AMI = {"kind": "traditional", "vendor": "ami_legacy",
              "product_name": "Type6 Tioga Pass Single Side",
              "creds_used": ("root", "pw")}
_SETTLED_PHOSPHOR = {"kind": "openbmc", "vendor": "phosphor",
                     "product_name": "Type6 Tioga Pass Single Side",
                     "creds_used": ("root", "pw")}


def _settle(port_state, secs=301):
    # The BMC has been visible for `secs`, and the cached probe is that old.
    port_state["vars"]["bmcmac"]["since"] = _iso_ago(secs)
    port_state["link_session_since"] = _iso_ago(secs)
    port_state["bmc_kind_cached"]["probed_at"] = _iso_ago(secs)


def test_early_bmc_kind_probe_is_reprobed_once_it_settles():
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    facts = _two_mac_openbmc_facts()
    env, calls = _early_env([_EARLY_AMI, _SETTLED_PHOSPHOR])
    port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)
    assert calls["n"] == 1
    cache = port_state["bmc_kind_cached"]
    assert (cache["kind"], cache["vendor"]) == ("traditional", "ami_legacy")
    assert cache.get("early") is True
    sn = port_state["chassis_sn"]
    bmc_mac = port_state["bmc_mac"]

    _settle(port_state)
    port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)

    assert calls["n"] == 2
    cache = port_state["bmc_kind_cached"]
    assert (cache["kind"], cache["vendor"]) == ("openbmc", "phosphor")
    assert cache.get("early") is False
    assert cache["for_mac"] == bmc_mac
    assert port_state["bmc_mac"] == bmc_mac
    assert port_state["chassis_sn"] == sn

    # Settled: no further probes.
    port_state["bmc_kind_cached"]["probed_at"] = _iso_ago(3600)
    port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)
    assert calls["n"] == 2


def test_late_bmc_kind_probe_is_not_reprobed():
    from flax_observe.state_machine import port_worker_one_iter

    port_state = _initial_port_state()
    port_state["vars"]["linkstate"] = {"value": "link", "since": _iso_ago(3600)}
    port_state["vars"]["bmcmac"] = {"value": "found", "since": _iso_ago(3600)}
    port_state["link_session_since"] = _iso_ago(3600)
    facts = _two_mac_openbmc_facts()
    env, calls = _early_env([_EARLY_AMI, _SETTLED_PHOSPHOR])
    port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)
    assert calls["n"] == 1
    assert port_state["bmc_kind_cached"].get("early") is False

    for _ in range(3):
        port_state["bmc_kind_cached"]["probed_at"] = _iso_ago(3600)
        port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)
    assert calls["n"] == 1
    assert port_state["bmc_kind_cached"]["kind"] == "traditional"


def test_early_bmc_kind_reprobe_clause_is_load_bearing(monkeypatch):
    # Mutation check: with the early clause disabled the early AMI label latches.
    import flax_observe.state_machine as sm

    monkeypatch.setattr(sm, "_early_reprobe_due", lambda cache: False)
    port_state = _initial_port_state()
    facts = _two_mac_openbmc_facts()
    env, calls = _early_env([_EARLY_AMI, _SETTLED_PHOSPHOR])
    sm.port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)
    _settle(port_state)
    sm.port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)
    assert calls["n"] == 1
    assert port_state["bmc_kind_cached"]["kind"] == "traditional"


_LATE_MISSED_SSH = dict(_EARLY_AMI, ssh_inconclusive=True)


def _late_port_state():
    port_state = _initial_port_state()
    port_state["vars"]["linkstate"] = {"value": "link", "since": _iso_ago(3600)}
    port_state["vars"]["bmcmac"] = {"value": "found", "since": _iso_ago(3600)}
    port_state["link_session_since"] = _iso_ago(3600)
    return port_state


def test_missed_ssh_login_fallback_is_unknown_and_reasked_until_it_answers():
    # et25b1 2026-10-08: a BMC reboot long after insertion (so not "early"),
    # ssh login missed, IPMI answered as root -> ami_legacy with no re-check.
    from flax_observe.state_machine import (
        INCONCLUSIVE_RETRY_SECS, port_worker_one_iter)

    port_state = _late_port_state()
    facts = _two_mac_openbmc_facts()
    env, calls = _early_env([_LATE_MISSED_SSH, _LATE_MISSED_SSH,
                             _SETTLED_PHOSPHOR])
    port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)
    cache = port_state["bmc_kind_cached"]
    assert calls["n"] == 1
    assert (cache["kind"], cache["vendor"], cache["inconclusive"]) == (
        "traditional", "unknown", 1)

    port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)
    assert calls["n"] == 1                      # not before the retry interval

    port_state["bmc_kind_cached"]["probed_at"] = _iso_ago(INCONCLUSIVE_RETRY_SECS + 1)
    port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)
    assert calls["n"] == 2
    assert port_state["bmc_kind_cached"]["inconclusive"] == 2
    assert port_state["bmc_kind_cached"]["vendor"] == "unknown"

    port_state["bmc_kind_cached"]["probed_at"] = _iso_ago(INCONCLUSIVE_RETRY_SECS + 1)
    port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)
    cache = port_state["bmc_kind_cached"]
    assert calls["n"] == 3
    assert (cache["kind"], cache["vendor"], cache["inconclusive"]) == (
        "openbmc", "phosphor", 0)

    for _ in range(2):                          # identified: asked no more
        port_state["bmc_kind_cached"]["probed_at"] = _iso_ago(3600)
        port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)
    assert calls["n"] == 3


def test_missed_ssh_login_retry_clause_is_load_bearing(monkeypatch):
    # Mutation check: without the retry clause the fallback is never re-asked.
    import flax_observe.state_machine as sm

    monkeypatch.setattr(sm, "_inconclusive_retry_due", lambda cache: False)
    port_state = _late_port_state()
    facts = _two_mac_openbmc_facts()
    env, calls = _early_env([_LATE_MISSED_SSH, _SETTLED_PHOSPHOR])
    sm.port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)
    port_state["bmc_kind_cached"]["probed_at"] = _iso_ago(3600)
    sm.port_worker_one_iter(port_state, facts, emit_event=[].append, env=env)
    assert calls["n"] == 1
    assert port_state["bmc_kind_cached"]["kind"] == "traditional"


# ---------------------------------------------------------------------------
# Task 5: SOL heartbeat by slot; phosphor keeps its IPMI session
# ---------------------------------------------------------------------------

def test_sol_heartbeat_fresh_file_is_live(tmp_path):
    from flax_observe.state_machine import _sol_session_active
    (tmp_path / "et8b3.active").write_text("")
    assert _sol_session_active("et8b3", sol_active_dir=str(tmp_path)) is True


def test_sol_heartbeat_stale_file_is_not_live_and_not_deleted(tmp_path):
    import os
    from flax_observe.state_machine import _sol_session_active
    p = tmp_path / "et8b3.active"
    p.write_text("")
    os.utime(p, (1000.0, 1000.0))
    assert _sol_session_active("et8b3", sol_active_dir=str(tmp_path), now=1181.0) is False
    assert p.exists()                               # read-only mount: never unlink


def test_sol_heartbeat_missing_file_is_not_live(tmp_path):
    from flax_observe.state_machine import _sol_session_active
    assert _sol_session_active("et8b3", sol_active_dir=str(tmp_path)) is False
    assert _sol_session_active("", sol_active_dir=str(tmp_path)) is False


def test_internal_port():
    from flax_observe.state_machine import _internal_port
    assert _internal_port("Et6/1") == "et6b1"
    assert _internal_port("Ethernet28/4") == "et28b4"
    assert _internal_port("et6b1") == "et6b1"


def test_sol_sentinel_is_looked_up_by_internal_port():
    # ami_legacy: the only vendor that still consults the sentinel (phosphor
    # never asks once it has an IPv4 bmc_ip).
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv
    asked = []
    env = _vendor_env("traditional", _bv.AMI_LEGACY, [])
    env.sol_session_active = lambda port, *a, **k: asked.append(port) or False
    port_worker_one_iter(_initial_port_state(), _vendor_facts(), emit_event=[].append, env=env)
    assert asked and set(asked) == {"et6b1"}


_PAIR_CREDS = [{"bmcuser": "USERID", "bmcpass": "a"}, {"bmcuser": "root", "bmcpass": "b"}]


def _kw_env(kind, vendor, seen, **kw):
    env = _vendor_env(kind, vendor, [], **kw)
    env.bmc_credentials = list(_PAIR_CREDS)

    def power(ip, login, **k):
        seen.append(("power", login, k.get("cipher"), k.get("bmc_mac"), k.get("port")))
        return ("on", "141.60 W")

    def lan_sn(ip, login, *a, **k):
        seen.append(("lan_sn", login, k.get("cipher")))
        return ("LAN-SN", "ok")
    env.bmc_power_and_sdr_traditional = power
    env.chassis_serial_traditional = lan_sn
    return env


def test_phosphor_power_uses_root_with_cipher_17_and_the_cache_keys():
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv
    seen = []
    ps = _initial_port_state()
    port_worker_one_iter(ps, _vendor_facts(), emit_event=[].append,
                         env=_kw_env("openbmc", _bv.PHOSPHOR, seen))
    p = [s for s in seen if s[0] == "power"][0]
    assert p[1] == ("root", "b") and p[2] == 17
    assert p[3] == _VENDOR_MAC and p[4] == "et6b1"


def test_ami_legacy_power_and_serial_use_userid_with_cipher_3():
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv
    seen = []
    # The kind probe realistically finds AMI legacy's working login via
    # USERID (paired user), same user as the pairing table -- so pairing
    # still fires (finding 1: pairing only substitutes when the probe's
    # creds_used agrees with the paired user, or is absent).
    env = _kw_env("traditional", _bv.AMI_LEGACY, seen, creds=("USERID", "x"))
    port_worker_one_iter(_initial_port_state(), _vendor_facts(), emit_event=[].append,
                         env=env)
    assert ("power", ("USERID", "a"), 3, _VENDOR_MAC, "et6b1") in seen
    assert ("lan_sn", ("USERID", "a"), 3) in seen


def test_reordered_creds_fall_back_to_the_probe_login_and_no_cipher():
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv
    seen = []
    env = _kw_env("openbmc", _bv.PHOSPHOR, seen)
    env.bmc_credentials = list(reversed(_PAIR_CREDS))
    port_worker_one_iter(_initial_port_state(), _vendor_facts(), emit_event=[].append, env=env)
    p = [s for s in seen if s[0] == "power"][0]
    assert p[1] == ("root", "x") and p[2] is None       # creds_used from the probe; walk


def test_phosphor_ssh_serial_is_read_once_then_latched():
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv
    calls = []
    ps = _initial_port_state()
    env = _vendor_env("openbmc", _bv.PHOSPHOR, calls)
    for _ in range(4):
        port_worker_one_iter(ps, _vendor_facts(), emit_event=[].append, env=env)
    assert calls.count("ssh_serial") == 1
    assert ps["chassis_sn"] == "SSH-SN"


def test_phosphor_ssh_serial_rereads_after_forget():
    from flax_observe.state_machine import port_worker_one_iter, _forget_identity
    from flax_observe import bmc_vendor as _bv
    calls = []
    ps = _initial_port_state()
    env = _vendor_env("openbmc", _bv.PHOSPHOR, calls)
    port_worker_one_iter(ps, _vendor_facts(), emit_event=[].append, env=env)
    _forget_identity(ps, [].append)
    port_worker_one_iter(ps, _vendor_facts(), emit_event=[].append, env=env)
    assert calls.count("ssh_serial") == 2


def test_phosphor_paired_login_used_when_probe_creds_agree(finding=1):
    # probe creds_used=("root","b") matches the paired user ("root") exactly
    # -> the paired (user, password, cipher) is used, same as today.
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv
    seen = []
    env = _kw_env("openbmc", _bv.PHOSPHOR, seen, creds=("root", "b"))
    port_worker_one_iter(_initial_port_state(), _vendor_facts(), emit_event=[].append, env=env)
    p = [s for s in seen if s[0] == "power"][0]
    assert p[1] == ("root", "b") and p[2] == 17


def test_phosphor_probe_creds_disagree_with_paired_user_skips_pairing(finding=1):
    # probe creds_used=("admin","y") is a DIFFERENT user than the paired
    # login's "root" -- a BMC whose working login is admin/oper must not be
    # silently overridden by the paired root credential. Use creds_used with
    # cipher None (today's pre-pairing behaviour), not the paired login.
    from flax_observe.state_machine import port_worker_one_iter
    from flax_observe import bmc_vendor as _bv
    seen = []
    env = _kw_env("openbmc", _bv.PHOSPHOR, seen, creds=("admin", "y"))
    port_worker_one_iter(_initial_port_state(), _vendor_facts(), emit_event=[].append, env=env)
    p = [s for s in seen if s[0] == "power"][0]
    assert p[1] == ("admin", "y") and p[2] is None


def test_hydrated_link_row_forgets_after_committed_down():
    """A worker hydrated from a row saying link + found (the departed blade)
    whose port is now down keeps the identity through a sub-debounce down and
    forgets it once the down is committed (the edam et7b1 case, 2026-11-06)."""
    import types
    from flax_observe import port_worker as pw
    from flax_observe.state_machine import port_worker_one_iter
    row = {"vars": {"linkstate": {"value": "link", "since": "2026-10-30T13:07:19Z"},
                    "inventory": {"value": "found", "since": "2026-10-30T13:07:19Z"}},
           "resolved": {"bmc_mac": "0c:42:a1:fb:78:ba", "nic_mac": "0c:42:a1:fb:78:b8",
                        "chassis_sn": "QTFCKQ0340B9F", "bmc_mac_latched": "0c:42:a1:fb:78:ba",
                        "link_session_since": "2026-10-30T13:07:19Z"}}
    w = pw.PortWorker("rabbit-edam", "et7b1", "", types.SimpleNamespace(get_port=lambda *a, **k: {}),
                      types.SimpleNamespace(), cycle_secs=10.0, prior=row)
    ps = w.port_state
    assert ps["chassis_sn"] == "QTFCKQ0340B9F"
    facts = {("rabbit-edam", "et7b1"): {"linkstate": "nolink", "macs": [], "lldp_neighbors": []}}
    port_worker_one_iter(ps, facts, lambda e: None, _stub_env())
    assert ps["chassis_sn"] == "QTFCKQ0340B9F"          # sub-debounce: kept
    ps["link_down_since"] = _iso_ago(31)
    port_worker_one_iter(ps, facts, lambda e: None, _stub_env())
    assert ps["bmc_mac"] is None and ps["chassis_sn"] is None
    assert ps["bmc_mac_latched"] is None


# --- pair-based swap + AMI over a pool lease (2026-11-06) -------------------
PAIR_NEW_BMC = "b8:59:9f:dd:fb:12"
PAIR_NEW_NIC = "b8:59:9f:dd:fb:10"
_OFFSET = {"scheme": "offset", "bmc_offset": 2, "host_offsets": [0]}
# An AMI BMC with no IPv4 yet, and a host NIC: neither answers a BMC probe.
_NO_BMC = {"kind": "unknown", "product_name": None, "creds_used": None}


def _pair_latched(env):
    from flax_observe.state_machine import port_worker_one_iter
    ps = _initial_port_state()
    port_worker_one_iter(ps, _facts([HOLD_NIC_OLD, HOLD_OLD], vid=25), [].append, env)
    assert ps["bmc_mac_latched"] == HOLD_OLD and ps["chassis_sn"] == "SN1"
    return ps


def test_macmath_pair_swap_forgets_without_serial_read():
    from flax_observe.state_machine import port_worker_one_iter
    env, _ = _hold_env({_ip_for_mac(HOLD_OLD): ("SN1", "ok")},
                       kinds={_ip_for_mac(PAIR_NEW_BMC): _NO_BMC, _ip_for_mac(PAIR_NEW_NIC): _NO_BMC},
                       macmath_by_vid={25: _OFFSET})
    ps = _pair_latched(env)
    events = []
    port_worker_one_iter(ps, _facts([PAIR_NEW_NIC, PAIR_NEW_BMC], vid=25), events.append, env)
    assert "bmc_identity_swapped" in [e.get("kind") for e in events]
    assert ps["chassis_sn"] != "SN1"
    assert ps["bmc_mac"] == PAIR_NEW_BMC


def test_new_pair_with_old_bmc_still_visible_keeps_holding():
    from flax_observe.state_machine import port_worker_one_iter
    env, _ = _hold_env({_ip_for_mac(HOLD_OLD): ("SN1", "ok")},
                       kinds={_ip_for_mac(PAIR_NEW_BMC): _NO_BMC, _ip_for_mac(PAIR_NEW_NIC): _NO_BMC},
                       macmath_by_vid={25: _OFFSET})
    ps = _pair_latched(env)
    events = []
    port_worker_one_iter(ps, _facts([PAIR_NEW_NIC, PAIR_NEW_BMC, HOLD_OLD], vid=25), events.append, env)
    assert "bmc_identity_swapped" not in [e.get("kind") for e in events]
    assert ps["chassis_sn"] == "SN1"


def test_ami_new_bmc_on_pool_ipv4_with_new_serial_swaps():
    from flax_observe.state_machine import port_worker_one_iter
    pool_ip = _ip_for_mac(HOLD_NEW)
    env, _ = _hold_env({_ip_for_mac(HOLD_OLD): ("SN1", "ok")},
                       kinds={pool_ip: {"kind": "traditional", "vendor": "ami_legacy",
                                        "product_name": "Leopard ORv2-DDR4",
                                        "creds_used": ("USERID", "x")}},
                       chassis_serial_traditional=lambda host, *a, **k:
                           ("SN2", "ok") if host == pool_ip else (None, "error"))
    ps = _latched_state(env)
    events = []
    port_worker_one_iter(ps, _facts([HOLD_NEW]), events.append, env)
    assert "bmc_identity_swapped" in [e.get("kind") for e in events]
    assert ps["chassis_sn"] == "SN2"


def test_bmc_ip_change_pool_to_reservation_is_not_a_swap():
    from flax_observe.state_machine import port_worker_one_iter
    ips = {HOLD_OLD: "172.24.0.60"}
    env, _ = _hold_env({"172.24.0.60": ("SN1", "ok"), "172.24.7.101": ("SN1", "ok")},
                       lookup_lease_ip=lambda path, mac, d: ips.get(mac))
    from flax_observe.state_machine import port_worker_one_iter as it
    ps = _initial_port_state()
    it(ps, _facts([HOLD_OLD]), [].append, env)
    assert ps["chassis_sn"] == "SN1"
    ips[HOLD_OLD] = "172.24.7.101"
    events = []
    it(ps, _facts([HOLD_OLD]), events.append, env)
    assert "bmc_identity_swapped" not in [e.get("kind") for e in events]
    assert ps["role_source"] != "held" and ps["bmc_mac"] == HOLD_OLD
