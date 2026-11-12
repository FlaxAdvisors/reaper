def _facts():
    return {"rabbit-edam": {"ports": {
        "Ethernet6/1": {"link": "link", "mask": "access", "access_vid": 25,
                        "bmc_mac": "98:03:9b:75:29:ce",
                        "nic_macs": ["98:03:9b:75:29:cc"]},
        "Ethernet1/1": {"link": "link", "mask": "trunk", "access_vid": None,
                        "bmc_mac": "b8:ce:f6:3e:56:c3", "nic_macs": []},
        "Ethernet9/9": {"link": "nolink", "mask": "access", "access_vid": 25,
                        "bmc_mac": None, "nic_macs": []},
        "Management1": {"link": "link", "bmc_mac": None, "nic_macs": []},
    }}}


def test_derive_post_targets_bmc_and_host():
    from flax_classify.post_reserve import derive_post_targets
    out = derive_post_targets(_facts(), "rabbit-edam")
    assert {(t["kind"], t["mac"], t["vid"], t["port_token"]) for t in out} == {
        ("bmc",  "98:03:9b:75:29:ce", 25, "et6b1"),
        ("host", "98:03:9b:75:29:cc", 25, "et6b1"),
    }


def test_derive_post_targets_skips_trunk_nolink_mgmt_and_unknown_switch():
    from flax_classify.post_reserve import derive_post_targets
    assert derive_post_targets(_facts(), "rabbit-edam"), "edam yields targets"
    assert derive_post_targets(_facts(), "rabbit-nope") == []   # unknown switch
    ports = {t["port"] for t in derive_post_targets(_facts(), "rabbit-edam")}
    assert ports == {"Ethernet6/1"}   # trunk/nolink/mgmt excluded


def _resolved():
    from flax_classify.post_reserve import derive_post_targets, resolve
    return resolve(derive_post_targets(_facts(), "rabbit-edam"), prefix="rr003-")


def test_resolve_computes_post_addresses_and_names():
    by = {(t["kind"]): t for t in _resolved()}
    assert by["bmc"]["ipv4"] == "172.25.6.101"
    assert by["bmc"]["ipv6"] == "fd00:25::6:101"
    assert by["bmc"]["hostname"] == "rr003-et6b1-bmc"
    assert by["host"]["ipv4"] == "172.25.6.1"
    assert by["host"]["hostname"] == "rr003-et6b1"


def test_render_isc_matches_makereservations_format():
    from flax_classify.post_reserve import render_isc
    norm = [" ".join(l.split()) for l in render_isc(_resolved()).splitlines()]
    assert "host rr003-et6b1 {hardware ethernet 98:03:9b:75:29:cc; fixed-address 172.25.6.1;} #et6b1" in norm
    assert any("rr003-et6b1-bmc" in l and "172.25.6.101;}" in l for l in norm)


def test_render_node_macs_is_bmc_mac_and_token():
    from flax_classify.post_reserve import derive_post_targets, render_node_macs
    out = render_node_macs(derive_post_targets(_facts(), "rabbit-edam"))
    assert out.strip() == "98039b7529ce et6b1"


def test_derive_post_targets_skips_unparseable_port_name():
    from flax_classify.post_reserve import derive_post_targets
    facts = {"rabbit-edam": {"ports": {
        "PortChannel7": {"link": "link", "mask": "access", "access_vid": 25,
                         "bmc_mac": "aa:bb:cc:dd:ee:ff", "nic_macs": []},
        "Ethernet6/1": {"link": "link", "mask": "access", "access_vid": 25,
                        "bmc_mac": "98:03:9b:75:29:ce", "nic_macs": []},
    }}}
    out = derive_post_targets(facts, "rabbit-edam")
    assert [t["port"] for t in out] == ["Ethernet6/1"]   # bad name skipped, good kept


def test_main_dry_run_emits_isc_without_writing(monkeypatch, capsys):
    import flax_classify.post_reserve as pr
    monkeypatch.setattr(pr, "_pool_and_facts",
                        lambda conninfo=None: (None, _facts()))
    wrote = []
    monkeypatch.setattr(pr, "upsert_desired",
                        lambda *a, **k: wrote.append(k))
    rc = pr.main(["--switch", "rabbit-edam", "--prefix", "rr003-",
                  "--dry-run", "--emit", "isc"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "rr003-et6b1-bmc" in out and "172.25.6.101;}" in out
    assert wrote == []   # dry-run writes nothing


def test_main_reserve_writes_desired_with_owner_role_post(monkeypatch, capsys):
    import flax_classify.post_reserve as pr
    monkeypatch.setattr(pr, "_pool_and_facts",
                        lambda conninfo=None: (object(), _facts()))
    calls = []
    monkeypatch.setattr(pr, "upsert_desired", lambda pool, **k: calls.append(k))
    rc = pr.main(["--switch", "rabbit-edam", "--prefix", "rr003-"])
    assert rc == 0
    assert all(c["owner_role"] == "post" for c in calls)
    assert {c["hostname"] for c in calls} == {"rr003-et6b1", "rr003-et6b1-bmc"}
    assert {c["ipv4"] for c in calls} == {"172.25.6.1", "172.25.6.101"}


# ---- node_macs.txt override seam (donum 33-edit -> 36 honors it) ----

def _facts_collapsed():
    # et26b2: real BMC (1a:46) is QUIET -> absent from the raw FDB. The port
    # shows a single mac (the host 1a:44); the classified split wrongly tags
    # that lone mac as the BMC and invents a phantom nic at 1a:42.
    return {"rabbit-edam": {"ports": {
        "Ethernet26/2": {"link": "link", "mask": "access", "access_vid": 25,
                         "macs": ["98:03:9b:59:1a:44"],
                         "bmc_mac": "98:03:9b:59:1a:44",
                         "nic_macs": ["98:03:9b:59:1a:42"]},
    }}}


def test_parse_node_macs_hex_and_token():
    from flax_classify.post_reserve import parse_node_macs
    assert parse_node_macs("98039b591a46 et26b2\n") == \
        [("98:03:9b:59:1a:46", "et26b2")]
    # blank lines and # comments are ignored
    assert parse_node_macs("\n# note\n98039b591a46 et26b2\n") == \
        [("98:03:9b:59:1a:46", "et26b2")]


def test_derive_targets_from_node_macs_bmc_from_file_host_from_raw():
    from flax_classify.post_reserve import derive_targets_from_node_macs
    # operator corrected the BMC to the quiet 1a:46 (NOT in the FDB)
    out = derive_targets_from_node_macs([("98:03:9b:59:1a:46", "et26b2")],
                                        _facts_collapsed(), "rabbit-edam")
    assert {(t["kind"], t["mac"]) for t in out} == {
        ("bmc",  "98:03:9b:59:1a:46"),      # authoritative from node_macs
        ("host", "98:03:9b:59:1a:44"),      # raw FDB mac minus the BMC
    }
    assert all(t["vid"] == 25 and t["port_token"] == "et26b2"
               and t["switch"] == "rabbit-edam"
               and t["port"] == "Ethernet26/2" for t in out)


def test_derive_targets_from_node_macs_excludes_phantom_nic():
    from flax_classify.post_reserve import derive_targets_from_node_macs
    # 1a:42 lives only in the classified nic_macs, never the raw FDB -> never
    # a host target.
    out = derive_targets_from_node_macs([("98:03:9b:59:1a:46", "et26b2")],
                                        _facts_collapsed(), "rabbit-edam")
    assert "98:03:9b:59:1a:42" not in {t["mac"] for t in out}


def test_derive_targets_from_node_macs_skips_unknown_port():
    from flax_classify.post_reserve import derive_targets_from_node_macs
    out = derive_targets_from_node_macs([("98:03:9b:59:1a:46", "et9b9")],
                                        _facts_collapsed(), "rabbit-edam")
    assert out == []   # et9b9 not in switch_facts -> skipped, no crash


def test_order_prefix_rule():
    from flax_classify.post_reserve import order_prefix
    assert order_prefix("RRORD-003") == "rr003"
    assert order_prefix("rrord-003") == "rr003"
    assert order_prefix("AB-7") == "ab7"            # <3 digits: take what's there
    assert order_prefix("ZZORD-123456") == "zz456"  # last three digits
    assert order_prefix("") == ""
    assert order_prefix(None) == ""


def test_post_hostname_owns_the_hyphen():
    from flax_classify.post_reserve import post_hostname
    # bare prefix (the new lane path)
    assert post_hostname("rr003", "Ethernet6/1", "host") == "rr003-et6b1"
    assert post_hostname("rr003", "Ethernet6/1", "bmc") == "rr003-et6b1-bmc"
    # trailing-dash prefix (legacy CLI/donum path) yields the SAME thing
    assert post_hostname("rr003-", "Ethernet6/1", "bmc") == "rr003-et6b1-bmc"
    # rack tag glued onto the order-prefix
    assert post_hostname("rr003b", "Ethernet6/1", "host") == "rr003b-et6b1"


def test_main_from_node_macs_stdin_overrides_bmc(monkeypatch):
    import io
    import flax_classify.post_reserve as pr
    monkeypatch.setattr(pr, "_pool_and_facts",
                        lambda conninfo=None: (object(), _facts_collapsed()))
    calls = []
    monkeypatch.setattr(pr, "upsert_desired", lambda pool, **k: calls.append(k))
    monkeypatch.setattr("sys.stdin", io.StringIO("98039b591a46 et26b2\n"))
    rc = pr.main(["--switch", "rabbit-edam", "--prefix", "rr003-",
                  "--from-node-macs", "-"])
    assert rc == 0
    by_kind = {c["kind"]: c for c in calls}
    assert by_kind["bmc"]["mac"] == "98:03:9b:59:1a:46"
    assert by_kind["bmc"]["ipv4"] == "172.25.26.102"
    assert by_kind["bmc"]["hostname"] == "rr003-et26b2-bmc"
    assert by_kind["host"]["mac"] == "98:03:9b:59:1a:44"
    assert by_kind["host"]["ipv4"] == "172.25.26.2"
    assert by_kind["host"]["hostname"] == "rr003-et26b2"
    assert all(c["owner_role"] == "post" for c in calls)


# ---- observe comms-confirmed-BMC guard ----

def _facts_lldp():
    # et6b1: BMC ...ac is LLDP-confirmed (tiogapass eth0); host ...aa is the nic.
    return {"rabbit-edam": {"ports": {
        "Ethernet6/1": {"link": "link", "mask": "access", "access_vid": 25,
                        "bmc_mac": "98:03:9b:a6:fd:ac",
                        "nic_macs": ["98:03:9b:a6:fd:aa"],
                        "lldp_disagreement": False,
                        "lldp_neighbors": [
                            {"mac": "9803.9ba6.fdac", "sysname": "tiogapass",
                             "port_description": "eth0"}]},
        # et7b1: BMC dark, only a PXEing host mac visible -- NO observe comms-confirm
        # row for this port -> unconfirmed, regardless of LLDP.
        "Ethernet7/1": {"link": "link", "mask": "access", "access_vid": 25,
                        "bmc_mac": "98:03:9b:75:2d:16",
                        "nic_macs": ["98:03:9b:75:2d:14"],
                        "lldp_disagreement": False, "lldp_neighbors": []},
        # et8b1: no observe comms-confirm row for this port -> unconfirmed, even
        # though LLDP neighbor data exists (LLDP is no longer the gate).
        "Ethernet8/1": {"link": "link", "mask": "access", "access_vid": 25,
                        "bmc_mac": "98:03:9b:00:00:02",
                        "nic_macs": ["98:03:9b:00:00:00"],
                        "lldp_disagreement": True,
                        "lldp_neighbors": [
                            {"mac": "9803.9b00.0002", "port_description": "eth0"}]},
    }}}


def _observed_et6b1():
    # observe positively comms-confirmed et6b1's BMC (redfish/ipmi/openbmc) --
    # what require_confirmed_bmc=True needs to reserve it.
    return {("rabbit-edam", "et6b1"):
            {"bmc_mac": "98:03:9b:a6:fd:ac", "nic_mac": "98:03:9b:a6:fd:aa",
             "source": "probe_confirmed"}}


def test_derive_post_targets_requires_comms_confirmed():
    from flax_classify.post_reserve import derive_post_targets
    observed = {("rabbit-edam", "et6b1"):
                {"bmc_mac": "98:03:9b:a6:fd:ac", "nic_mac": "98:03:9b:a6:fd:aa",
                 "source": "probe_confirmed"}}
    out = derive_post_targets(_facts_lldp(), "rabbit-edam",
                              require_confirmed_bmc=True, observed=observed)
    ports = {t["port"] for t in out}
    assert ports == {"Ethernet6/1"}    # only the comms-confirmed BMC port
    kinds = {(t["kind"], t["mac"]) for t in out}
    assert kinds == {("bmc", "98:03:9b:a6:fd:ac"), ("host", "98:03:9b:a6:fd:aa")}


def test_derive_post_targets_default_unchanged():
    # require_confirmed_bmc defaults False -> existing callers/tests keep all ports
    from flax_classify.post_reserve import derive_post_targets
    out = derive_post_targets(_facts_lldp(), "rabbit-edam")
    assert {t["port"] for t in out} == {"Ethernet6/1", "Ethernet7/1", "Ethernet8/1"}


def test_read_post_racks_from_geometry(tmp_path):
    import json
    from flax_classify.post_reserve import read_post_racks
    p = tmp_path / "post-geometry.json"
    # REAL post-geometry.json format: racks is a LIST of {switch, tag, label}.
    p.write_text(json.dumps({"racks": [
        {"switch": "rabbit-edam", "tag": "", "label": "Eindhoven Post"},
        {"switch": "rabbit-edam-b", "tag": "b", "label": "Post B"}]}))
    assert sorted(read_post_racks(str(p))) == [("rabbit-edam", ""), ("rabbit-edam-b", "b")]
    assert read_post_racks(str(tmp_path / "nope.json")) == []   # missing file -> []
    # malformed JSON / missing racks key -> [] (lane no-ops, never crashes)
    bad = tmp_path / "bad.json"; bad.write_text("{ not json")
    assert read_post_racks(str(bad)) == []
    p.write_text(json.dumps({"slots": []}))
    assert read_post_racks(str(p)) == []


def test_run_post_reservations_noop_without_order(monkeypatch):
    import flax_classify.post_reserve as pr
    calls = []
    monkeypatch.setattr(pr, "upsert_desired", lambda pool, **k: calls.append(k))
    out = pr.run_post_reservations(object(), order_no=None,
                                   racks=[("rabbit-edam", "")], facts=_facts_lldp())
    assert out == {"written": 0, "purged": 0}
    assert calls == []                                  # NO default order -> writes nothing


def test_run_post_reservations_writes_confirmed_and_holds_unconfirmed(monkeypatch):
    import flax_classify.post_reserve as pr
    calls, purges = [], []
    monkeypatch.setattr(pr, "upsert_desired", lambda pool, **k: calls.append(k))
    monkeypatch.setattr(pr, "delete_desired_slot",
                        lambda pool, **k: purges.append(k) or 0)
    out = pr.run_post_reservations(object(), order_no="RRORD-003",
                                   racks=[("rabbit-edam", "")], facts=_facts_lldp(),
                                   observed=_observed_et6b1())
    # only the comms-confirmed et6b1 yields rows (et7b1 BMC-dark, et8b1 disagree held)
    assert out["written"] == 2
    by_kind = {c["kind"]: c for c in calls}
    assert by_kind["bmc"]["hostname"] == "rr003-et6b1-bmc"
    assert by_kind["bmc"]["ipv4"] == "172.25.6.101"
    assert by_kind["host"]["hostname"] == "rr003-et6b1"
    assert by_kind["host"]["ipv4"] == "172.25.6.1"
    assert all(c["owner_role"] == "post" for c in calls)
    # a sticky slot purge per written occupant, scoped to (port, kind, keep_mac)
    assert {(p["port"], p["kind"], p["keep_mac"]) for p in purges} == {
        ("et6b1", "bmc", "98:03:9b:a6:fd:ac"),
        ("et6b1", "host", "98:03:9b:a6:fd:aa")}
    assert all(p["owner_role"] == "post" for p in purges)


def test_run_post_reservations_returns_derived_macs(monkeypatch):
    """Final-review fix: run_post_reservations reports (normalised lowercase)
    every mac it successfully upserted into desired_reservations this cycle --
    reconcile_post_reservations' keep-set pass needs this set to avoid
    clobbering a freshly-derived row with the stale actual-kea-row echo."""
    import flax_classify.post_reserve as pr
    monkeypatch.setattr(pr, "upsert_desired", lambda pool, **k: None)
    monkeypatch.setattr(pr, "delete_desired_slot", lambda pool, **k: 0)
    out = pr.run_post_reservations(object(), order_no="RRORD-003",
                                   racks=[("rabbit-edam", "")], facts=_facts_lldp(),
                                   observed=_observed_et6b1())
    assert out["derived_macs"] == frozenset(
        {"98:03:9b:a6:fd:ac", "98:03:9b:a6:fd:aa"})


def test_run_post_reservations_derived_macs_excludes_failed_dual_write(monkeypatch):
    """A mac whose upsert_desired call raised must NOT appear in derived_macs --
    it did not actually get a fresh desired row this cycle."""
    import flax_classify.post_reserve as pr
    monkeypatch.setattr(pr, "delete_desired_slot", lambda pool, **k: 0)

    def _boom(pool, **k):
        if k["mac"] == "98:03:9b:a6:fd:ac":
            raise RuntimeError("boom")
    monkeypatch.setattr(pr, "upsert_desired", _boom)
    out = pr.run_post_reservations(object(), order_no="RRORD-003",
                                   racks=[("rabbit-edam", "")], facts=_facts_lldp(),
                                   observed=_observed_et6b1())
    assert out["derived_macs"] == frozenset({"98:03:9b:a6:fd:aa"})


def test_run_post_reservations_no_op_summary_has_no_derived_macs_key(monkeypatch):
    """The order_no-falsy no-op summary shape stays exactly {'written': 0,
    'purged': 0} -- no derived_macs key (run_post_lane defaults it via .get)."""
    import flax_classify.post_reserve as pr
    out = pr.run_post_reservations(object(), order_no=None,
                                   racks=[("rabbit-edam", "")], facts=_facts_lldp())
    assert out == {"written": 0, "purged": 0}


def test_run_post_reservations_applies_rack_tag(monkeypatch):
    import flax_classify.post_reserve as pr
    calls = []
    monkeypatch.setattr(pr, "upsert_desired", lambda pool, **k: calls.append(k))
    monkeypatch.setattr(pr, "delete_desired_slot", lambda pool, **k: 0)
    pr.run_post_reservations(object(), order_no="RRORD-003",
                             racks=[("rabbit-edam", "b")], facts=_facts_lldp(),
                             observed=_observed_et6b1())
    assert {c["hostname"] for c in calls} == {"rr003b-et6b1", "rr003b-et6b1-bmc"}


def test_run_post_reservations_keeps_host_slot_when_nic_absent(monkeypatch):
    """et6b1 regression (spec 'the et6b1 regression test'): a confirmed BMC whose
    host NIC has transiently dropped from nic_macs (host powered off mid-flash)
    yields ONLY a bmc write — the host slot is never written, so it is never
    purged, so an existing host reservation survives. Stickiness end-to-end."""
    import flax_classify.post_reserve as pr
    facts = {"rabbit-edam": {"ports": {
        "Ethernet6/1": {"link": "link", "mask": "access", "access_vid": 25,
                        "bmc_mac": "98:03:9b:a6:fd:ac", "nic_macs": [],
                        "lldp_disagreement": False,
                        "lldp_neighbors": [
                            {"mac": "9803.9ba6.fdac", "sysname": "tiogapass",
                             "port_description": "eth0"}]}}}}
    # observe comms-confirmed the BMC but has no nic_mac of its own (host
    # transiently unreachable) -- falls back to the port's nic_macs, which is
    # empty here, so no host target is derived.
    observed = {("rabbit-edam", "et6b1"):
                {"bmc_mac": "98:03:9b:a6:fd:ac", "source": "probe_confirmed"}}
    calls, purges = [], []
    monkeypatch.setattr(pr, "upsert_desired", lambda pool, **k: calls.append(k))
    monkeypatch.setattr(pr, "delete_desired_slot",
                        lambda pool, **k: purges.append(k) or 0)
    out = pr.run_post_reservations(object(), order_no="RRORD-003",
                                   racks=[("rabbit-edam", "")], facts=facts,
                                   observed=observed)
    assert out["written"] == 1                       # bmc only; no host target derived
    assert [c["kind"] for c in calls] == ["bmc"]
    assert {p["kind"] for p in purges} == {"bmc"}    # host slot NEVER purged -> sticky


def test_run_post_reservations_writes_desired_with_expected_values(monkeypatch):
    """Per written target, upsert_desired is called with owner_role="post"
    and the derived mac/kind/vid/switch/port/hostname/ipv4/ipv6 values."""
    import flax_classify.post_reserve as pr
    desired_writes = []
    monkeypatch.setattr(pr, "upsert_desired",
                        lambda pool, **k: desired_writes.append(k))
    monkeypatch.setattr(pr, "delete_desired_slot", lambda pool, **k: 0)

    pr.run_post_reservations(object(), order_no="RRORD-003",
                             racks=[("rabbit-edam", "")], facts=_facts_lldp(),
                             observed=_observed_et6b1())

    assert len(desired_writes) == 2
    by_mac = {w["mac"]: w for w in desired_writes}
    bmc = by_mac["98:03:9b:a6:fd:ac"]
    assert bmc["owner_role"] == "post"
    assert bmc["kind"] == "bmc"
    assert bmc["vid"] == 25
    assert bmc["switch"] == "rabbit-edam"
    assert bmc["port"] == "et6b1"
    assert bmc["hostname"] == "rr003-et6b1-bmc"
    assert bmc["ipv4"] == "172.25.6.101"


def test_run_post_reservations_slot_purge_uses_same_slot_and_keep_mac(monkeypatch):
    """delete_desired_slot is called with owner_role="post" and the exact
    switch/port/kind/keep_mac of the target just upserted."""
    import flax_classify.post_reserve as pr
    monkeypatch.setattr(pr, "upsert_desired", lambda pool, **k: None)
    purges = []
    monkeypatch.setattr(pr, "delete_desired_slot",
                        lambda pool, **k: purges.append(k) or 0)

    pr.run_post_reservations(object(), order_no="RRORD-003",
                             racks=[("rabbit-edam", "")], facts=_facts_lldp(),
                             observed=_observed_et6b1())

    assert len(purges) == 2
    by_kind = {p["kind"]: p for p in purges}
    assert by_kind["bmc"]["owner_role"] == "post"
    assert by_kind["bmc"]["switch"] == "rabbit-edam"
    assert by_kind["bmc"]["port"] == "et6b1"
    assert by_kind["bmc"]["keep_mac"] == "98:03:9b:a6:fd:ac"
    assert by_kind["host"]["keep_mac"] == "98:03:9b:a6:fd:aa"


def test_run_post_reservations_write_failure_isolated_per_target(monkeypatch):
    """If upsert_desired (or delete_desired_slot) raises for one target, the
    post lane still processes ALL targets -- the failure is contained
    per-call-site, never aborting the remaining targets."""
    import flax_classify.post_reserve as pr

    def _boom_upsert(pool, **k):
        raise RuntimeError("desired_reservations table missing (pre-migration)")

    def _boom_delete(pool, **k):
        raise RuntimeError("desired_reservations table missing (pre-migration)")

    monkeypatch.setattr(pr, "upsert_desired", _boom_upsert)
    monkeypatch.setattr(pr, "delete_desired_slot", _boom_delete)

    out = pr.run_post_reservations(object(), order_no="RRORD-003",
                                   racks=[("rabbit-edam", "")], facts=_facts_lldp(),
                                   observed=_observed_et6b1())

    assert out["written"] == 2   # both targets processed regardless
    assert out["purged"] == 0    # every purge call raised -> nothing counted
    assert out["derived_macs"] == frozenset()  # every upsert raised too


def test_main_reserve_write_failure_isolated(monkeypatch):
    """A raising upsert_desired in the CLI path must not stop processing of
    the remaining targets nor crash main()."""
    import flax_classify.post_reserve as pr
    monkeypatch.setattr(pr, "_pool_and_facts",
                        lambda conninfo=None: (object(), _facts()))

    def _boom(pool, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(pr, "upsert_desired", _boom)

    rc = pr.main(["--switch", "rabbit-edam", "--prefix", "rr003-"])
    assert rc == 0


# ── Task 4: probe-confirmed BMC identity overrides switch_facts arithmetic ──

def _facts_probe(port="Ethernet10/4", bmc="98:03:9b:c6:4f:13", nic=None, lldp=False):
    info = {"link": "link", "mask": "access", "access_vid": 25,
            "bmc_mac": bmc, "nic_macs": [nic] if nic else []}
    if lldp:
        info["lldp_neighbors"] = [{"port_description": "eth0", "mac": bmc}]
    return {"rabbit-edam": {"ports": {port: info}}}


def test_probe_confirmed_overrides_arithmetic_bmc():
    # switch_facts has the +3-masked wrong bmc; observe confirms the true one.
    from flax_classify.post_reserve import derive_post_targets
    facts = _facts_probe(bmc="98:03:9b:c6:4f:13")          # arithmetic junk
    observed = {("rabbit-edam", "et10b4"):
                {"bmc_mac": "98:03:9b:c6:4f:16", "nic_mac": "98:03:9b:c6:4f:14",
                 "source": "probe_promote_bmc"}}
    out = derive_post_targets(facts, "rabbit-edam", require_confirmed_bmc=True, observed=observed)
    bmc = [t for t in out if t["kind"] == "bmc"][0]
    assert bmc["mac"] == "98:03:9b:c6:4f:16"          # probe value wins
    host = [t for t in out if t["kind"] == "host"][0]
    assert host["mac"] == "98:03:9b:c6:4f:14"


def test_probe_confirmed_bypasses_switch_facts_gate():
    # No LLDP evidence at all, but probe-confirmed -> still reservable (+3 fix).
    from flax_classify.post_reserve import derive_post_targets
    facts = _facts_probe(bmc="98:03:9b:c6:4f:13", lldp=False)
    observed = {("rabbit-edam", "et10b4"):
                {"bmc_mac": "98:03:9b:c6:4f:16", "source": "probe_confirmed"}}
    out = derive_post_targets(facts, "rabbit-edam", require_confirmed_bmc=True, observed=observed)
    assert any(t["kind"] == "bmc" and t["mac"] == "98:03:9b:c6:4f:16" for t in out)


def test_no_observe_row_skipped_when_confirmed_required():
    from flax_classify.post_reserve import derive_post_targets
    facts = _facts_probe(bmc="98:03:9b:c6:4f:16", lldp=True)   # LLDP-confirmed, no observe row
    out = derive_post_targets(facts, "rabbit-edam", require_confirmed_bmc=True, observed={})
    assert out == []   # no observe row -> not comms-confirmed -> skipped


def test_non_probe_source_does_not_override():
    # heuristic is NOT comms-confirmed (only probe_confirmed/probe_promote_bmc
    # are) -- under require_confirmed_bmc the port must be skipped entirely.
    from flax_classify.post_reserve import derive_post_targets
    facts = _facts_probe(bmc="98:03:9b:c6:4f:16", lldp=True)
    observed = {("rabbit-edam", "et10b4"):
                {"bmc_mac": "aa:bb:cc:dd:ee:ff", "source": "heuristic"}}  # not comms-confirmed
    out = derive_post_targets(facts, "rabbit-edam", require_confirmed_bmc=True, observed=observed)
    assert out == []


def test_probe_flip_host_skips_port():
    # et10b4 double-fault: BMC dark -> single_mac collapse mislabels the lone
    # host MAC as bmc; host ifname reverted to eth0 so it advertises LLDP eth0.
    # observe reached it and flipped it to host -> the port must be SKIPPED.
    from flax_classify.post_reserve import derive_post_targets
    host = "98:03:9b:a8:f1:a6"
    facts = {"rabbit-edam": {"ports": {"Ethernet10/4": {
        "link": "link", "mask": "access", "access_vid": 25,
        "bmc_mac": host, "nic_macs": ["98:03:9b:a8:f1:a4"],
        "lldp_disagreement": False, "classification_source": "single_mac",
        "lldp_neighbors": [{"mac": "9803.9ba8.f1a6", "sysname": "fl001-et10b4",
                            "port_description": "eth0"}]}}}}
    observed = {("rabbit-edam", "et10b4"): {"source": "probe_flip_host", "bmc_mac": None}}
    out = derive_post_targets(facts, "rabbit-edam",
                              require_confirmed_bmc=True, observed=observed)
    assert out == []                                  # skipped: no comms-confirmed BMC


def test_probe_promoted_bmc_never_emitted_as_host():
    """+3 case: probe promotes a mac that switch_facts STILL lists in nic_macs.
    It must not also be emitted as a host target -- desired_reservations upserts
    ON CONFLICT (mac) and host is appended after bmc, so a host row for bmc_mac
    would clobber the BMC's own reservation."""
    from flax_classify.post_reserve import derive_post_targets, _lldp_mac_norm
    facts = {"rabbit-edam": {"ports": {"Ethernet10/1": {
        "link": "link", "mask": "access", "access_vid": 25,
        "bmc_mac": "98:03:9b:c6:4f:13",                       # arithmetic junk
        "nic_macs": ["98:03:9b:c6:4f:16", "98:03:9b:c6:4f:14"]}}}}  # true bmc still here
    observed = {("rabbit-edam", "et10b1"):
                {"bmc_mac": "98:03:9b:c6:4f:16", "source": "probe_promote_bmc"}}
    out = derive_post_targets(facts, "rabbit-edam", require_confirmed_bmc=True, observed=observed)
    bmcs = [t for t in out if t["kind"] == "bmc"]
    hosts = [t for t in out if t["kind"] == "host"]
    assert len(bmcs) == 1 and bmcs[0]["mac"] == "98:03:9b:c6:4f:16"
    assert all(_lldp_mac_norm(h["mac"]) != _lldp_mac_norm("98:03:9b:c6:4f:16") for h in hosts)
    assert any(_lldp_mac_norm(h["mac"]) == _lldp_mac_norm("98:03:9b:c6:4f:14") for h in hosts)


# ── Post-3b demolition: no kea_hosts writer calls, ever ─────────────────────

def test_run_post_reservations_and_main_never_call_kea_writers(monkeypatch):
    """Belt-and-suspenders: even patching the kea_hosts module's writer
    functions to raise if called, a full run_post_reservations pass AND a
    full CLI main() pass complete without ever touching them -- proof no
    code path (direct or deferred import) still reaches kea.hosts."""
    import flax_classify.kea_hosts as kea_hosts
    import flax_classify.post_reserve as pr

    def _must_not_be_called(*a, **k):
        raise AssertionError("legacy kea writer must never be called post-3b")

    # raising=False on the purge fns: Task 3 deletes the dead ones outright,
    # which is an even stronger pass than "present but never called".
    monkeypatch.setattr(kea_hosts, "upsert_kea_host", _must_not_be_called)
    monkeypatch.setattr(kea_hosts, "purge_superseded_port_hosts",
                        _must_not_be_called, raising=False)
    monkeypatch.setattr(kea_hosts, "purge_superseded_slot_hosts",
                        _must_not_be_called, raising=False)

    monkeypatch.setattr(pr, "upsert_desired", lambda pool, **k: None)
    monkeypatch.setattr(pr, "delete_desired_slot", lambda pool, **k: 0)

    out = pr.run_post_reservations(object(), order_no="RRORD-003",
                                   racks=[("rabbit-edam", "")], facts=_facts_lldp(),
                                   observed=_observed_et6b1())
    assert out["written"] == 2

    monkeypatch.setattr(pr, "_pool_and_facts",
                        lambda conninfo=None: (object(), _facts()))
    rc = pr.main(["--switch", "rabbit-edam", "--prefix", "rr003-"])
    assert rc == 0


# --- shared-switch slot scoping (braintree: post + triage on rabbit-lorax) ---

def _shared_facts():
    """rabbit-lorax with a post slot (et5b1, vlan24) and a co-located triage
    port (et6b1, vlan16) — both access, linked, with BMC+NIC macs."""
    return {"rabbit-lorax": {"ports": {
        "Ethernet5/1": {"link": "link", "mask": "access", "access_vid": 24,
                        "bmc_mac": "aa:bb:cc:00:00:01",
                        "nic_macs": ["aa:bb:cc:00:00:02"]},
        "Ethernet6/1": {"link": "link", "mask": "access", "access_vid": 16,
                        "bmc_mac": "aa:bb:cc:00:00:03",
                        "nic_macs": ["aa:bb:cc:00:00:04"]},
    }}}


def test_derive_post_targets_scopes_to_allow_tokens():
    """On a shared switch, allow_tokens (the post-geometry slots) restricts
    derivation: a co-located triage port (et6b1) is excluded even though it is
    access + has a BMC. Without allow_tokens, both ports derive (control)."""
    from flax_classify.post_reserve import derive_post_targets
    facts = _shared_facts()
    both = {t["port_token"] for t in derive_post_targets(facts, "rabbit-lorax")}
    assert both == {"et5b1", "et6b1"}, f"control: both derive, got {both}"
    scoped = {t["port_token"] for t in
              derive_post_targets(facts, "rabbit-lorax", allow_tokens={"et5b1"})}
    assert scoped == {"et5b1"}, f"triage et6b1 must be excluded, got {scoped}"


def test_read_post_slots_groups_tokens_by_switch(tmp_path):
    """read_post_slots reads post-geometry.json's slots list into
    {switch: {port_token, ...}} for the run lane's allowlist."""
    import json
    from flax_classify.post_reserve import read_post_slots
    p = tmp_path / "post-geometry.json"
    p.write_text(json.dumps({"racks": [{"switch": "rabbit-lorax", "tag": ""}],
                             "slots": [
        {"port": "et5b1", "switch": "rabbit-lorax"},
        {"port": "et5b2", "switch": "rabbit-lorax"},
        {"port": "et9b1", "switch": "rabbit-lorax"}]}))
    assert read_post_slots(str(p)) == {"rabbit-lorax": {"et5b1", "et5b2", "et9b1"}}
    assert read_post_slots(str(tmp_path / "missing.json")) == {}


def test_run_post_reservations_scopes_to_slots(monkeypatch):
    """Shared-switch (braintree): run_post_reservations restricts derivation to
    the per-switch post-slot allowlist. A co-located triage port (et6b1, access
    vlan16, comms-confirmed BMC) on the SAME rack switch is NOT written because
    it is not a declared post slot."""
    import flax_classify.post_reserve as pr
    calls = []
    monkeypatch.setattr(pr, "upsert_desired", lambda pool, **k: calls.append(k))
    monkeypatch.setattr(pr, "delete_desired_slot", lambda pool, **k: 0)
    facts = {"rabbit-lorax": {"ports": {
        "Ethernet5/1": {"link": "link", "mask": "access", "access_vid": 24,
                        "bmc_mac": "aa:bb:cc:00:00:01",
                        "nic_macs": ["aa:bb:cc:00:00:02"]},
        "Ethernet6/1": {"link": "link", "mask": "access", "access_vid": 16,
                        "bmc_mac": "aa:bb:cc:00:00:03",
                        "nic_macs": ["aa:bb:cc:00:00:04"]},
    }}}
    observed = {
        ("rabbit-lorax", "et5b1"): {"source": "probe_confirmed",
                                    "bmc_mac": "aa:bb:cc:00:00:01",
                                    "nic_mac": "aa:bb:cc:00:00:02"},
        ("rabbit-lorax", "et6b1"): {"source": "probe_confirmed",
                                    "bmc_mac": "aa:bb:cc:00:00:03",
                                    "nic_mac": "aa:bb:cc:00:00:04"},
    }
    out = pr.run_post_reservations(object(), order_no="RRORD-003",
                                   racks=[("rabbit-lorax", "")], facts=facts,
                                   observed=observed,
                                   slots={"rabbit-lorax": {"et5b1"}})
    tokens = {c["port"] for c in calls}
    assert tokens == {"et5b1"}, f"triage et6b1 must be excluded, got {tokens}"


# --- sticky-slot purge must stick: purged_macs (et28b3, 2026-09-19) ---

def _slot_purge_reporting(victims_by_kind):
    """Fake delete_desired_slot that 'deletes' victims_by_kind[kind] and
    reports them through the deleted_macs out-param, like the real one."""
    def _fake(pool, *, owner_role, switch, port, kind, keep_mac,
              deleted_macs=None):
        victims = victims_by_kind.get(kind, [])
        if deleted_macs is not None:
            deleted_macs.update(victims)
        return len(victims)
    return _fake


def test_run_post_reservations_returns_purged_macs(monkeypatch):
    import flax_classify.post_reserve as pr
    monkeypatch.setattr(pr, "upsert_desired", lambda pool, **k: None)
    monkeypatch.setattr(pr, "delete_desired_slot", _slot_purge_reporting(
        {"bmc": ["98:03:9b:75:2a:b2"]}))
    out = pr.run_post_reservations(object(), order_no="RRORD-003",
                                   racks=[("rabbit-edam", "")], facts=_facts_lldp(),
                                   observed=_observed_et6b1())
    assert out["purged"] == 1
    assert out["purged_macs"] == frozenset({"98:03:9b:75:2a:b2"})


def test_run_post_reservations_purged_macs_empty_when_nothing_deleted(monkeypatch):
    import flax_classify.post_reserve as pr
    monkeypatch.setattr(pr, "upsert_desired", lambda pool, **k: None)
    monkeypatch.setattr(pr, "delete_desired_slot", _slot_purge_reporting({}))
    out = pr.run_post_reservations(object(), order_no="RRORD-003",
                                   racks=[("rabbit-edam", "")], facts=_facts_lldp(),
                                   observed=_observed_et6b1())
    assert out["purged"] == 0
    assert out["purged_macs"] == frozenset()


def test_run_post_reservations_purged_macs_excludes_failed_purge(monkeypatch):
    import flax_classify.post_reserve as pr
    monkeypatch.setattr(pr, "upsert_desired", lambda pool, **k: None)

    def _boom(pool, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(pr, "delete_desired_slot", _boom)
    out = pr.run_post_reservations(object(), order_no="RRORD-003",
                                   racks=[("rabbit-edam", "")], facts=_facts_lldp(),
                                   observed=_observed_et6b1())
    assert out["purged_macs"] == frozenset()


def test_held_identity_is_not_comms_confirmed():
    """Part 2 / B: observe writes resolved.source='held' while it holds a
    slot's identity through an empty FDB. The post lane must never treat a
    held MAC as a comms-confirmed BMC (spec 2026-09-27 §4 contracts)."""
    from flax_classify.post_reserve import _COMMS_CONFIRMED
    assert "held" not in _COMMS_CONFIRMED


# --- per-node order prefix (post_node_assignment), 2026-11-06 ----------------
def _two_confirmed():
    ports = {}
    observed = {}
    for p, tok, b, n in (("Ethernet7/1", "et7b1", "b8:59:9f:ab:af:0e", "b8:59:9f:ab:af:0c"),
                         ("Ethernet8/1", "et8b1", "0c:42:a1:f3:c4:fa", "0c:42:a1:f3:c4:f8")):
        ports[p] = {"link": "link", "mask": "access", "access_vid": 24, "bmc_mac": b, "nic_macs": [n]}
        observed[("rabbit-edam", tok)] = {"bmc_mac": b, "nic_mac": n, "source": "probe_confirmed"}
    return {"rabbit-edam": {"ports": ports}}, observed


def _run(monkeypatch, order_no, node_orders):
    import flax_classify.post_reserve as pr
    calls = []
    monkeypatch.setattr(pr, "upsert_desired", lambda pool, **k: calls.append(k))
    monkeypatch.setattr(pr, "delete_desired_slot", lambda pool, **k: 0)
    facts, observed = _two_confirmed()
    pr.run_post_reservations(object(), order_no=order_no, racks=[("rabbit-edam", "")],
                             facts=facts, observed=observed, node_orders=node_orders)
    return sorted(c["hostname"] for c in calls)


def test_run_post_reservations_per_node_prefix(monkeypatch):
    assert _run(monkeypatch, "RRORD-100", {"b8599fabaf0e": "RRORD-1033"}) == [
        "rr033-et7b1", "rr033-et7b1-bmc", "rr100-et8b1", "rr100-et8b1-bmc"]


def test_unassigned_and_unknown_bmc_use_header_prefix(monkeypatch):
    assert _run(monkeypatch, "RRORD-100", {}) == [
        "rr100-et7b1", "rr100-et7b1-bmc", "rr100-et8b1", "rr100-et8b1-bmc"]


def test_node_without_any_order_gets_no_reservation(monkeypatch):
    assert _run(monkeypatch, None, {"b8599fabaf0e": "RRORD-1033"}) == ["rr033-et7b1", "rr033-et7b1-bmc"]
