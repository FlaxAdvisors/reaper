"""Tests for flax_observe.port_worker boot hydration + latch guard."""


def _hydrated_state(nic_mac="52:54:00:11:22:33", chassis_sn="ABC123"):
    """A port_state as it looks right after the first post-hydration cycle:
    linkstate=link, inventory=found, with the freshly re-observed nic_mac /
    chassis_sn already written by port_worker_one_iter."""
    from flax_observe.state_machine import STATE_VARS
    ps = {
        "switch": "rabbit-lorax", "port": "et6b1", "ou": "20",
        "vars": {v: {"value": "unknown", "since": None} for v in STATE_VARS},
        "nic_mac": nic_mac, "bmc_mac": "52:54:00:11:22:35",
        "chassis_sn": chassis_sn, "product_name": "Leopard",
        "link_session_since": "2026-06-20T00:00:00Z",
    }
    ps["vars"]["linkstate"] = {"value": "link", "since": "2026-06-20T00:00:00Z"}
    ps["vars"]["inventory"] = {"value": "found", "since": "2026-06-20T00:00:00Z"}
    ps["vars"]["chassissn"] = {"value": "found", "since": "2026-06-20T00:00:00Z"}
    return ps


def test_boot_validate_keeps_latch_when_invariants_hold():
    from flax_observe.port_worker import boot_validate_latch
    ps = _hydrated_state()
    events = []
    outcome = boot_validate_latch(
        ps, prior_nic_mac="52:54:00:11:22:33",
        prior_chassis_sn="ABC123", emit_event=events.append)
    assert outcome == "restored"
    assert ps["vars"]["inventory"]["value"] == "found"
    assert ps["link_session_since"] == "2026-06-20T00:00:00Z"
    assert any(e.get("kind") == "inventory_latch_restored" for e in events)


def test_boot_validate_drops_latch_on_nic_mac_change():
    from flax_observe.port_worker import boot_validate_latch
    ps = _hydrated_state(nic_mac="52:54:00:99:99:99")  # fresh nic differs
    events = []
    outcome = boot_validate_latch(
        ps, prior_nic_mac="52:54:00:11:22:33",
        prior_chassis_sn="ABC123", emit_event=events.append)
    assert outcome == "dropped"
    assert ps["vars"]["inventory"]["value"] == "notfound"
    assert ps["link_session_since"] != "2026-06-20T00:00:00Z"
    ev = [e for e in events if e.get("kind") == "inventory_latch_dropped"][0]
    assert ev["reason"] == "nic_mac_changed"
    assert ps["nic_mac"] is None, "a swap-detected drop must null the stale nic_mac"
    assert ps["bmc_mac"] is None
    assert ps["bmc_ip"] is None
    assert ps["nic_ip"] is None


def test_boot_validate_drops_latch_on_confirmed_chassis_mismatch():
    from flax_observe.port_worker import boot_validate_latch
    ps = _hydrated_state(chassis_sn="ZZZ999")  # fresh successful read differs
    events = []
    outcome = boot_validate_latch(
        ps, prior_nic_mac="52:54:00:11:22:33",
        prior_chassis_sn="ABC123", emit_event=events.append)
    assert outcome == "dropped"
    ev = [e for e in events if e.get("kind") == "inventory_latch_dropped"][0]
    assert ev["reason"] == "chassis_sn_changed"


def test_boot_validate_holds_latch_on_transient_chassis_read_failure():
    """A transient BMC probe failure leaves chassis_sn at the latched value
    (== prior); the soft rule must NOT treat that as a mismatch."""
    from flax_observe.port_worker import boot_validate_latch
    ps = _hydrated_state(chassis_sn="ABC123")  # held == prior
    events = []
    outcome = boot_validate_latch(
        ps, prior_nic_mac="52:54:00:11:22:33",
        prior_chassis_sn="ABC123", emit_event=events.append)
    assert outcome == "restored"
    assert ps["vars"]["inventory"]["value"] == "found"


def test_boot_validate_ignores_unobserved_signals():
    """fresh nic_mac / chassis_sn None (not re-observed this cycle) is 'couldn't
    observe', not 'differs' -> latch held."""
    from flax_observe.port_worker import boot_validate_latch
    ps = _hydrated_state()
    ps["nic_mac"] = None
    ps["chassis_sn"] = None
    events = []
    outcome = boot_validate_latch(
        ps, prior_nic_mac="52:54:00:11:22:33",
        prior_chassis_sn="ABC123", emit_event=events.append)
    assert outcome == "restored"
    assert ps["vars"]["inventory"]["value"] == "found"


def _make_worker(prior=None):
    import types
    from flax_observe.port_worker import PortWorker
    env = types.SimpleNamespace()
    cache = types.SimpleNamespace(get_port=lambda *a, **k: {})
    return PortWorker("rabbit-lorax", "et6b1", "20", cache, env,
                      cycle_secs=10.0, prior=prior)


def test_init_cold_start_is_blank_and_unarmed():
    w = _make_worker(prior=None)
    assert w.port_state["vars"]["linkstate"]["value"] == "unknown"
    assert w.port_state["nic_mac"] is None
    assert w._boot_checked is True  # nothing to validate


def test_init_hydrates_vars_scalars_and_boundary():
    prior = {
        "vars": {
            "linkstate": {"value": "link", "since": "2026-06-20T00:00:00Z"},
            "inventory": {"value": "found", "since": "2026-06-20T00:00:00Z"},
        },
        "resolved": {
            "nic_mac": "52:54:00:11:22:33", "bmc_mac": "52:54:00:11:22:35",
            "chassis_sn": "ABC123", "product_name": "Leopard",
            "link_session_since": "2026-06-20T00:00:00Z",
        },
    }
    w = _make_worker(prior=prior)
    assert w.port_state["vars"]["linkstate"]["value"] == "link"
    assert w.port_state["vars"]["inventory"]["value"] == "found"
    assert w.port_state["nic_mac"] == "52:54:00:11:22:33"
    assert w.port_state["chassis_sn"] == "ABC123"
    assert w.port_state["link_session_since"] == "2026-06-20T00:00:00Z"
    # found + link latch -> guard armed with the prior identity stashed
    assert w._boot_checked is False
    assert w._boot_prior_nic_mac == "52:54:00:11:22:33"
    assert w._boot_prior_chassis_sn == "ABC123"


def test_hydrate_does_not_restore_bmcpower():
    """bmcpower must NOT be hydrated to its persisted value. Otherwise the first
    post-restart cycle could manufacture a spurious off->on and reset inventory
    for a node that booted (and inventoried) during the downtime. Leaving it
    unknown means the power-on inventory reset only fires on a LIVE-observed
    off->on. Other vars (linkstate, inventory) must still hydrate."""
    prior = {
        "vars": {
            "linkstate": {"value": "link", "since": "2026-06-20T00:00:00Z"},
            "inventory": {"value": "found", "since": "2026-06-20T00:00:00Z"},
            "bmcpower": {"value": "off", "since": "2026-06-20T00:00:00Z"},
        },
        "resolved": {"nic_mac": "52:54:00:11:22:33",
                     "link_session_since": "2026-06-20T00:00:00Z"},
    }
    w = _make_worker(prior=prior)
    assert w.port_state["vars"]["bmcpower"]["value"] == "unknown"   # NOT hydrated
    assert w.port_state["vars"]["linkstate"]["value"] == "link"      # still hydrated
    assert w.port_state["vars"]["inventory"]["value"] == "found"     # still hydrated


def test_init_does_not_arm_guard_when_prior_inventory_not_found():
    prior = {
        "vars": {
            "linkstate": {"value": "link", "since": "2026-06-20T00:00:00Z"},
            "inventory": {"value": "notfound", "since": "2026-06-20T00:00:00Z"},
        },
        "resolved": {"nic_mac": "52:54:00:11:22:33"},
    }
    w = _make_worker(prior=prior)
    assert w.port_state["nic_mac"] == "52:54:00:11:22:33"  # still hydrated
    assert w._boot_checked is True  # but no latch to protect -> unarmed


def _run_one_cycle(monkeypatch, prior, fact, inventory_status):
    """Drive a single _cycle_once with a stub cache+env; capture the resolved
    dict passed to upsert_observe_state. Returns (worker, captured)."""
    import types
    from flax_observe import port_worker as pw

    captured = {}

    def fake_upsert(*, switch, port, vars, last_polled, resolved=None):
        captured["resolved"] = resolved
        captured["vars"] = vars
        return 1

    monkeypatch.setattr(pw, "upsert_observe_state", fake_upsert)
    monkeypatch.setattr(pw, "emit_audit_event", lambda **k: None)

    env = types.SimpleNamespace()
    env.probe_bmc_kind = lambda *a, **k: {"kind": "unknown", "product_name": None,
                                          "creds_used": None}
    env.bmc_power_and_sdr_traditional = lambda *a, **k: ("unknown", None)
    env.chassis_serial_traditional = lambda *a, **k: None
    env.chassis_serial_openbmc = lambda *a, **k: None
    env.lookup_lease_ip = lambda *a, **k: None
    env.kea_lease_fresh = lambda mac, boundary: True
    env.ping_host = lambda ip: "ok"
    env.nginx_pxe_seen = lambda *a, **k: "found"
    env.ssh_uptime = lambda *a, **k: "ok"
    env.inventory_status = inventory_status
    env.intentional_flap_active = lambda *a, **k: False
    env.credentials = {}
    env.bmc_credentials = []
    env.host_credentials = []
    env.nodes_root = "/export/nodes"

    cache = types.SimpleNamespace(get_port=lambda *a, **k: fact)
    w = pw.PortWorker("rabbit-lorax", "et6b1", "20", cache, env,
                      cycle_secs=10.0, prior=prior)
    w._cycle_once()
    return w, captured


# A real +/-2 BMC/NIC pair: classify_macs([..33, ..35]) -> nic=..33, bmc=..35,
# so the freshly classified nic_mac equals the hydrated prior one (no spurious
# de-latch from the classifier inventing a different pair member).
_PAIR_FACT = {"linkstate": "link",
              "macs": ["52:54:00:11:22:33", "52:54:00:11:22:35"],
              "lldp_neighbors": []}


def test_cycle_persists_link_session_since_in_resolved(monkeypatch):
    """_cycle_once must write link_session_since into the resolved dict it
    upserts, and must run the boot guard exactly once (flag flips to True)."""
    prior = {
        "vars": {"linkstate": {"value": "link", "since": "2026-06-20T00:00:00Z"},
                 "inventory": {"value": "found", "since": "2026-06-20T00:00:00Z"}},
        "resolved": {"nic_mac": "52:54:00:11:22:33", "bmc_mac": "52:54:00:11:22:35",
                     "chassis_sn": "ABC123",
                     "link_session_since": "2026-06-20T00:00:00Z"},
    }
    inv = lambda root, mac, ts: "found" if ts == "2026-06-20T00:00:00Z" else "notfound"
    w, captured = _run_one_cycle(monkeypatch, prior, _PAIR_FACT, inv)

    assert "link_session_since" in captured["resolved"]
    # The latch is KEPT: the preserved boundary is persisted unchanged (not
    # reset to "now") and inventory stays found across the restart.
    assert captured["resolved"]["link_session_since"] == "2026-06-20T00:00:00Z"
    assert captured["vars"]["inventory"]["value"] == "found"
    assert w._boot_checked is True  # guard ran once


def test_cycle_cold_start_seeds_boundary_into_resolved(monkeypatch):
    """No prior row -> a fresh boundary is seeded and still persisted in
    resolved; the guard is never armed (nothing to validate)."""
    inv = lambda root, mac, ts: "notfound"
    w, captured = _run_one_cycle(monkeypatch, None, _PAIR_FACT, inv)
    assert captured["resolved"]["link_session_since"] is not None
    assert w._boot_checked is True


def test_cycle_persists_role_source_in_resolved(monkeypatch):
    """_persist MUST write the probe verdict source (port_state['role_source'])
    into resolved['source'], so the post lane (flax_classify.post_reserve) can
    key its probe-confirmed reservation on it. Regression: resolved carried
    bmc_mac/bmc_kind but NOT source, so post_reserve's probe-override was dead
    code and +3 blades never reserved. _PAIR_FACT has no probe evidence -> the
    verdict is the heuristic fallback, so source == 'heuristic'."""
    inv = lambda root, mac, ts: "notfound"
    w, captured = _run_one_cycle(monkeypatch, None, _PAIR_FACT, inv)
    assert "source" in captured["resolved"], \
        "resolved must carry the probe verdict source for post_reserve"
    assert captured["resolved"]["source"] == "heuristic"


def test_iter_consumes_forget_port_sentinel(tmp_path, monkeypatch):
    """A forget-port sentinel is consumed and logged (forget_port_ignored) and the normal iteration runs -- no identity wipe (Part 2 R7)."""
    import types
    import flax_observe.port_worker as pw_mod
    from flax_observe import port_worker as pw
    from tests.test_flax_observe_state_machine import _stub_env

    audit_events = []

    def fake_upsert(*, switch, port, vars, last_polled, resolved=None):
        return 1

    monkeypatch.setattr(pw, "upsert_observe_state", fake_upsert)
    monkeypatch.setattr(
        pw, "emit_audit_event",
        lambda kind, switch, port, mac, payload: audit_events.append(payload),
    )

    iter_calls = []
    monkeypatch.setattr(pw_mod, "port_worker_one_iter",
                        lambda *a, **k: iter_calls.append(a))

    d = tmp_path / "forget-port"
    d.mkdir()
    (d / "et6b1").write_text("")
    env = _stub_env(forget_port_dir=str(d))
    cache = types.SimpleNamespace(get_port=lambda *a, **k: {})
    w = pw.PortWorker("rabbit-gouda", "et6b1", "20", cache, env, cycle_secs=10.0)
    ps = w.port_state
    ps["bmc_mac"] = "aa:bb:cc:00:11:24"
    ps["nic_mac"] = "aa:bb:cc:00:11:22"
    ps["chassis_sn"] = "OLDSN"

    w._cycle_once()

    # Part 2 R7: the identity hold confirms MAC changes by serial, so the
    # sentinel no longer forgets anything -- it is consumed and logged, and
    # the normal iteration runs this same cycle.
    assert len(iter_calls) == 1
    assert ps["bmc_mac"] == "aa:bb:cc:00:11:24"
    assert ps["chassis_sn"] == "OLDSN"
    assert not (d / "et6b1").exists(), "sentinel must be unlinked (consume-once)"
    assert [e["kind"] for e in audit_events if e.get("kind") == "forget_port_ignored"] \
        == ["forget_port_ignored"]


def test_persist_carries_bmc_vendor_from_the_cache(monkeypatch):
    """The vendor must reach the API row the same way bmc_kind does, or
    nothing downstream can consume it. bmc_kind stays frozen (still emitted
    unchanged) during the additive migration."""
    from flax_observe import bmc_vendor as _bv
    from flax_observe import port_worker as pw

    w = _make_worker()
    w.port_state["bmc_kind_cached"] = {
        "kind": "openbmc",
        "vendor": _bv.PHOSPHOR,
        "taxonomy_version": _bv.TAXONOMY_VERSION,
        "creds_used": ("u", "p"),
        "product_name": "Type6 Tioga Pass Single Side",
        "for_mac": "aa:bb:cc:dd:ee:ff",
    }

    captured = {}

    def fake_upsert(*, switch, port, vars, last_polled, resolved=None):
        captured["resolved"] = resolved
        return 1

    monkeypatch.setattr(pw, "upsert_observe_state", fake_upsert)
    w._persist()

    resolved = captured["resolved"]
    assert resolved["bmc_vendor"] == _bv.PHOSPHOR
    assert resolved["bmc_kind"] == "openbmc"       # frozen, still emitted
    assert "creds_used" not in resolved


def test_persist_calls_vendor_export_update_with_cached_vendor(monkeypatch):
    """Task 4b: _persist must forward the cached vendor + bmc_ip/bmc_mac to
    env.vendor_export.update(switch, port, ...) so /etc/flax/bmc_vendor.json
    stays current for ghost's host-side bins."""
    import types
    from flax_observe import port_worker as pw

    def fake_upsert(*, switch, port, vars, last_polled, resolved=None):
        return 1

    monkeypatch.setattr(pw, "upsert_observe_state", fake_upsert)

    calls = []

    class FakeVendorExport:
        def update(self, switch, port, *, vendor, bmc_ip, bmc_mac):
            calls.append((switch, port, vendor, bmc_ip, bmc_mac))

    env = types.SimpleNamespace(vendor_export=FakeVendorExport())
    cache = types.SimpleNamespace(get_port=lambda *a, **k: {})
    w = pw.PortWorker("rabbit-lorax", "et6b1", "20", cache, env, cycle_secs=10.0)
    w.port_state["bmc_kind_cached"] = {"kind": "openbmc", "vendor": "phosphor"}
    w.port_state["bmc_ip"] = "172.24.6.101"
    w.port_state["bmc_mac"] = "52:54:00:11:22:35"

    w._persist()

    assert calls == [("rabbit-lorax", "et6b1", "phosphor",
                      "172.24.6.101", "52:54:00:11:22:35")]


def test_persist_without_vendor_export_still_persists(monkeypatch):
    """A worker whose env has no vendor_export attribute (e.g. make_env's
    plain output, or an older test env) must persist normally -- the export
    is optional wiring, never a hard dependency of _persist."""
    import types
    from flax_observe import port_worker as pw

    captured = {}

    def fake_upsert(*, switch, port, vars, last_polled, resolved=None):
        captured["resolved"] = resolved
        return 1

    monkeypatch.setattr(pw, "upsert_observe_state", fake_upsert)

    env = types.SimpleNamespace()  # no vendor_export attribute at all
    cache = types.SimpleNamespace(get_port=lambda *a, **k: {})
    w = pw.PortWorker("rabbit-lorax", "et6b1", "20", cache, env, cycle_secs=10.0)

    w._persist()

    assert "resolved" in captured


def test_persist_survives_vendor_export_raising(monkeypatch):
    """Fix round 1, finding 2: _persist's call into vendor_export.update must
    be guarded -- ANY exception escaping it (not just OSError, which
    VendorExport already catches internally) must not propagate out of
    _persist. Otherwise a side-channel export failure would abort the core
    persistence path and, one level up in _cycle_once, skip the
    emit_audit_event loop that follows _persist()."""
    import types
    from flax_observe import port_worker as pw

    captured = {}

    def fake_upsert(*, switch, port, vars, last_polled, resolved=None):
        captured["resolved"] = resolved
        return 42

    monkeypatch.setattr(pw, "upsert_observe_state", fake_upsert)

    class RaisingVendorExport:
        def update(self, *a, **k):
            raise RuntimeError("disk full")

    env = types.SimpleNamespace(vendor_export=RaisingVendorExport())
    cache = types.SimpleNamespace(get_port=lambda *a, **k: {})
    w = pw.PortWorker("rabbit-lorax", "et6b1", "20", cache, env, cycle_secs=10.0)

    w._persist()  # must not raise

    assert w.last_generation == 42
    assert "resolved" in captured


def test_cycle_once_survives_vendor_export_raising(monkeypatch):
    """Fix round 1, finding 2: a raising vendor_export must not prevent the
    rest of _cycle_once (the audit-event emission loop that runs right
    after _persist()) from completing."""
    import types
    from flax_observe import port_worker as pw
    from tests.test_flax_observe_state_machine import _stub_env

    monkeypatch.setattr(pw, "upsert_observe_state", lambda **k: 1)
    audit_calls = []
    monkeypatch.setattr(pw, "emit_audit_event",
                        lambda **k: audit_calls.append(k))

    class RaisingVendorExport:
        def update(self, *a, **k):
            raise RuntimeError("boom")

    env = _stub_env(vendor_export=RaisingVendorExport())
    fact = {"linkstate": "link", "macs": [], "lldp_neighbors": []}
    cache = types.SimpleNamespace(get_port=lambda *a, **k: fact)
    w = pw.PortWorker("rabbit-lorax", "et6b1", "20", cache, env, cycle_secs=10.0)

    w._cycle_once()  # must not raise

    assert w.last_error is None
    # linkstate unknown -> link is a real transition; its audit event must
    # still have been emitted despite vendor_export blowing up mid-cycle.
    assert any(c.get("switch") == "rabbit-lorax" for c in audit_calls)


def test_hydrate_restores_latched_mac_and_hold_start():
    """Review Focus 2: a restart resumes a hold at its ORIGINAL start."""
    prior = {"vars": {"linkstate": {"value": "link", "since": "2026-06-20T00:00:00Z"}},
             "resolved": {"bmc_mac": "b8:59:9f:dd:ea:2a", "chassis_sn": "SN1",
                          "bmc_mac_latched": "b8:59:9f:dd:ea:2a",
                          "bmc_mac_held_since": "2026-06-20T00:05:00Z"}}
    w = _make_worker(prior=prior)
    assert w.port_state["bmc_mac_latched"] == "b8:59:9f:dd:ea:2a"
    assert w.port_state["bmc_mac_held_since"] == "2026-06-20T00:05:00Z"


def test_hydrate_old_row_falls_back_to_bmc_mac_as_latched():
    prior = {"vars": {"linkstate": {"value": "link", "since": "2026-06-20T00:00:00Z"}},
             "resolved": {"bmc_mac": "b8:59:9f:dd:ea:2a", "chassis_sn": "SN1"}}
    w = _make_worker(prior=prior)
    assert w.port_state["bmc_mac_latched"] == "b8:59:9f:dd:ea:2a"
    assert w.port_state.get("bmc_mac_held_since") is None


def test_persist_writes_latched_and_hold_keys(monkeypatch):
    from flax_observe import port_worker as pw
    captured = {}
    monkeypatch.setattr(pw, "upsert_observe_state",
                        lambda **kw: captured.update(kw) or 1)
    w = _make_worker()
    w.port_state["bmc_mac_latched"] = "b8:59:9f:dd:ea:2a"
    w.port_state["bmc_mac_held_since"] = "2026-06-20T00:05:00Z"
    w.port_state["role_source"] = "held"
    w._persist()
    r = captured["resolved"]
    assert r["bmc_mac_latched"] == "b8:59:9f:dd:ea:2a"
    assert r["bmc_mac_held_since"] == "2026-06-20T00:05:00Z"
    assert r["source"] == "held"


def _serial_rewritten_worker(tmp_path, monkeypatch, sentinel=True):
    """A PortWorker with a latched identity and (optionally) a
    serial-rewritten sentinel; the iteration itself is stubbed."""
    import types
    import flax_observe.port_worker as pw_mod
    from flax_observe import port_worker as pw
    from tests.test_flax_observe_state_machine import _stub_env

    audit, iters = [], []
    monkeypatch.setattr(pw, "upsert_observe_state",
                        lambda *, switch, port, vars, last_polled, resolved=None: 1)
    monkeypatch.setattr(pw, "emit_audit_event",
                        lambda kind, switch, port, mac, payload: audit.append(payload))
    monkeypatch.setattr(pw_mod, "port_worker_one_iter", lambda *a, **k: iters.append(a))
    d = tmp_path / "serial-rewritten"
    d.mkdir()
    if sentinel:
        (d / "et6b1").write_text("")
    fp = tmp_path / "forget-port"
    fp.mkdir()
    env = _stub_env(forget_port_dir=str(fp), serial_rewritten_dir=str(d))
    cache = types.SimpleNamespace(get_port=lambda *a, **k: {})
    w = pw.PortWorker("rabbit-gouda", "et6b1", "20", cache, env, cycle_secs=10.0)
    ps = w.port_state
    ps["bmc_mac"] = "aa:bb:cc:00:11:24"
    ps["nic_mac"] = "aa:bb:cc:00:11:22"
    ps["chassis_sn"] = "OLDSN"
    ps["link_session_since"] = "2026-06-20T00:00:00Z"
    return w, ps, d, audit, iters


def test_serial_rewritten_sentinel_forgets_once(tmp_path, monkeypatch):
    """fru_fw rewrote a latched serial (spec 2026-10-06): the port's identity
    is forgotten exactly as for a pulled blade, once, and the iteration runs."""
    w, ps, d, audit, iters = _serial_rewritten_worker(tmp_path, monkeypatch)
    w._cycle_once()
    assert ps["chassis_sn"] is None
    assert ps["link_session_since"] != "2026-06-20T00:00:00Z"
    assert not (d / "et6b1").exists(), "sentinel must be unlinked (consume-once)"
    ev = [e for e in audit if e.get("kind") == "serial_rewritten_forget"]
    assert len(ev) == 1
    assert ev[0]["prior_chassis_sn"] == "OLDSN"
    assert ev[0]["prior_bmc_mac"] == "aa:bb:cc:00:11:24"
    assert len(iters) == 1

    ps["chassis_sn"] = "NEWSN"                    # re-read on a later cycle
    boundary = ps["link_session_since"]
    w._cycle_once()                               # no sentinel: nothing forgotten
    assert ps["chassis_sn"] == "NEWSN" and ps["link_session_since"] == boundary
    assert len([e for e in audit if e.get("kind") == "serial_rewritten_forget"]) == 1


def test_no_serial_rewritten_sentinel_no_forget(tmp_path, monkeypatch):
    w, ps, d, audit, iters = _serial_rewritten_worker(tmp_path, monkeypatch, sentinel=False)
    w._cycle_once()
    assert ps["chassis_sn"] == "OLDSN"
    assert ps["link_session_since"] == "2026-06-20T00:00:00Z"
    assert not [e for e in audit if e.get("kind") == "serial_rewritten_forget"]


def test_forget_port_dir_is_not_the_serial_rewritten_dir():
    """The BMC flash bin still drops forget-port on every flash; it must stay
    ignored, so the two sentinels may never share a directory."""
    from flax_observe import state_machine as sm
    assert sm.SERIAL_REWRITTEN_DIR == "/run/flax/serial-rewritten"
    assert sm.SERIAL_REWRITTEN_DIR != sm.FORGET_PORT_DIR
