"""Tests for flax_classify.cycle.run_one_cycle (orchestrator-level)."""

import pytest


@pytest.fixture(autouse=True)
def _stub_write_hosts_file(monkeypatch):
    """Isolate EVERY cycle test from the real dnsmasq hosts writer.

    run_one_cycle defaults dns_hosts_path to /etc/dnsmasq.hosts/flax-triage-devices
    and calls write_hosts_file unconditionally. On a host where that dir
    exists (a bang host, a CI bind-mount) an unstubbed test would os.replace a
    REAL dnsmasq config file as a side-effect. Default every test to a no-op
    writer; tests that assert on the DNS write (or simulate its failure) set
    their own monkeypatch.setattr after this fixture, which overrides the
    no-op for that test only.
    """
    from flax_classify import cycle
    monkeypatch.setattr(cycle, "write_hosts_file",
                        lambda path, records: None)


def _stub_targets(monkeypatch, cycle):
    """Shared plumbing: two targets (bmc+host) on one port."""
    monkeypatch.setattr(cycle, "read_observe_rows",
                        lambda pool: [{"switch": "rabbit-lorax",
                                        "port": "et6b1",
                                        "resolved": {
                                            "bmc_mac": "1c:34:da:7f:b3:a4",
                                            "nic_mac": "1c:34:da:7f:b3:a2"}}])
    monkeypatch.setattr(cycle, "read_switch_facts",
                        lambda pool: {"rabbit-lorax": {"ports": {
                            "Ethernet6/1": {"access_vid": 17}}}})
    monkeypatch.setattr(cycle, "read_devices", lambda pool: [])


def test_run_one_cycle_writes_a_proposal_per_target(monkeypatch):
    from flax_classify import cycle

    _stub_targets(monkeypatch, cycle)
    desired_writes = []
    monkeypatch.setattr(cycle, "upsert_desired",
                        lambda pool, **kw: desired_writes.append(kw))
    monkeypatch.setattr(cycle, "sweep_desired_not_in",
                        lambda pool, **kw: 0)
    desired_port_writes = []
    monkeypatch.setattr(cycle, "upsert_desired_port",
                        lambda pool, **kw: desired_port_writes.append(kw))

    summary = cycle.run_one_cycle(pool=object(), resolve=lambda sw, p: "triage")
    assert summary == {"written": 2, "deleted": 0, "skipped": 0,
                       "written_desired": 1, "purged": 0, "held": 0}
    # Two desired writes: bmc + host
    by_mac = {w["mac"]: w for w in desired_writes}
    assert by_mac["1c:34:da:7f:b3:a4"]["ipv4"] == "172.17.6.101"
    assert by_mac["1c:34:da:7f:b3:a4"]["hostname"] == "unknown6b1-bmc"
    assert by_mac["1c:34:da:7f:b3:a4"]["kind"] == "bmc"
    assert by_mac["1c:34:da:7f:b3:a4"]["owner_role"] == "triage"
    assert by_mac["1c:34:da:7f:b3:a2"]["ipv4"] == "172.17.6.1"
    assert by_mac["1c:34:da:7f:b3:a2"]["hostname"] == "unknown6b1"
    # One desired_port write for the single port (bmc + host share a port)
    assert len(desired_port_writes) == 1
    dp = desired_port_writes[0]
    assert dp["switch"] == "rabbit-lorax"
    assert dp["port"] == "Ethernet6/1"
    assert dp["desired_vid"] == 17
    assert "bmc" in dp["occupants"]
    assert "host" in dp["occupants"]


def test_run_one_cycle_threads_ipv6_and_writes_dns_hosts(monkeypatch):
    """Each upsert_desired gets ipv6 from classify_one, and after the write
    loop write_hosts_file is called ONCE with the collected {hostname, ipv4,
    ipv6} records for the written targets."""
    from flax_classify import cycle

    _stub_targets(monkeypatch, cycle)
    desired_writes = []
    monkeypatch.setattr(cycle, "upsert_desired",
                        lambda pool, **kw: desired_writes.append(kw))
    monkeypatch.setattr(cycle, "sweep_desired_not_in", lambda pool, **kw: 0)
    monkeypatch.setattr(cycle, "upsert_desired_port", lambda pool, **kw: None)

    dns_calls = []
    monkeypatch.setattr(cycle, "write_hosts_file",
                        lambda path, records: dns_calls.append((path, records)))

    cycle.run_one_cycle(pool=object(), resolve=lambda sw, p: "triage",
                        dns_hosts_path="/tmp/flax-triage-devices")

    # ipv6 threaded into every desired write
    by_mac = {w["mac"]: w for w in desired_writes}
    assert by_mac["1c:34:da:7f:b3:a4"]["ipv6"] == "fd00:17::6:101"
    assert by_mac["1c:34:da:7f:b3:a2"]["ipv6"] == "fd00:17::6:1"

    # write_hosts_file called exactly once, with the path + collected records
    assert len(dns_calls) == 1
    path, records = dns_calls[0]
    assert path == "/tmp/flax-triage-devices"
    by_host = {r["hostname"]: r for r in records}
    assert by_host["unknown6b1-bmc"] == {
        "hostname": "unknown6b1-bmc", "ipv4": "172.17.6.101",
        "ipv6": "fd00:17::6:101"}
    assert by_host["unknown6b1"] == {
        "hostname": "unknown6b1", "ipv4": "172.17.6.1",
        "ipv6": "fd00:17::6:1"}


def test_run_one_cycle_dns_write_failure_does_not_kill_cycle(monkeypatch):
    """A hosts-file write error is logged and swallowed -- the cycle still
    returns its normal summary."""
    from flax_classify import cycle

    monkeypatch.setattr(cycle, "read_observe_rows", lambda pool: [])
    monkeypatch.setattr(cycle, "read_switch_facts", lambda pool: {})
    monkeypatch.setattr(cycle, "read_devices", lambda pool: [])
    monkeypatch.setattr(cycle, "upsert_desired_port", lambda pool, **kw: None)
    monkeypatch.setattr(cycle, "sweep_desired_not_in", lambda pool, **kw: 0)

    def _boom(path, records):
        raise OSError("disk full")

    monkeypatch.setattr(cycle, "write_hosts_file", _boom)

    summary = cycle.run_one_cycle(pool=object(), resolve=lambda sw, p: "triage")
    assert summary == {"written": 0, "deleted": 0, "skipped": 0,
                       "written_desired": 0, "purged": 0, "held": 0}


def test_run_one_cycle_returns_deleted_count_when_nothing_to_write(monkeypatch):
    from flax_classify import cycle

    monkeypatch.setattr(cycle, "read_observe_rows", lambda pool: [])
    monkeypatch.setattr(cycle, "read_switch_facts", lambda pool: {})
    monkeypatch.setattr(cycle, "read_devices", lambda pool: [])
    monkeypatch.setattr(cycle, "upsert_desired_port",
                        lambda pool, **kw: None)
    monkeypatch.setattr(cycle, "sweep_desired_not_in",
                        lambda pool, **kw: 1)  # stale row swept

    summary = cycle.run_one_cycle(pool=object(), resolve=lambda sw, p: "triage")
    assert summary == {"written": 0, "deleted": 1, "skipped": 0,
                       "written_desired": 0, "purged": 0, "held": 0}


def test_run_one_cycle_counts_skipped_on_formula_error(monkeypatch):
    """If classify_one raises ValueError (e.g. infra-slot collision),
    the row is logged and skipped — cycle keeps going.
    """
    from flax_classify import cycle

    monkeypatch.setattr(cycle, "read_observe_rows",
                        lambda pool: [{"switch": "turtle-fudge",
                                        "port": "et6b1",  # turtle expects swp<N>
                                        "resolved": {"bmc_mac": "aa:bb:cc:dd:ee:ff"}}])
    monkeypatch.setattr(cycle, "read_switch_facts",
                        lambda pool: {"turtle-fudge": {"ports": {
                            "Ethernet6/1": {"access_vid": 24}}}})
    monkeypatch.setattr(cycle, "read_devices", lambda pool: [])
    desired_writes = []
    monkeypatch.setattr(cycle, "upsert_desired",
                        lambda pool, **kw: desired_writes.append(kw))
    monkeypatch.setattr(cycle, "sweep_desired_not_in", lambda pool, **kw: 0)
    desired_port_writes = []
    monkeypatch.setattr(cycle, "upsert_desired_port",
                        lambda pool, **kw: desired_port_writes.append(kw))

    summary = cycle.run_one_cycle(pool=object(), resolve=lambda sw, p: "triage")
    # desired_port write succeeds (before classify_one fails); desired
    # reservation write is skipped
    assert summary == {"written": 0, "deleted": 0, "skipped": 1,
                       "written_desired": 1, "purged": 0, "held": 0}
    assert desired_writes == []
    # desired_port row was written (port was valid, even though classify_one errored)
    assert len(desired_port_writes) == 1


def test_run_one_cycle_dual_writes_desired_reservations_per_target(monkeypatch):
    """Per classified target, upsert_desired is called with owner_role=
    "triage" and the values derived from classify_one (mac/kind/vid/switch/
    port/hostname/ipv4/ipv6)."""
    from flax_classify import cycle

    _stub_targets(monkeypatch, cycle)
    monkeypatch.setattr(cycle, "upsert_desired_port", lambda pool, **kw: None)
    desired_writes = []
    monkeypatch.setattr(cycle, "upsert_desired",
                        lambda pool, **kw: desired_writes.append(kw))
    monkeypatch.setattr(cycle, "sweep_desired_not_in",
                        lambda pool, **kw: 0)

    cycle.run_one_cycle(pool=object(), resolve=lambda sw, p: "triage")

    assert len(desired_writes) == 2
    by_mac = {w["mac"]: w for w in desired_writes}
    assert by_mac["1c:34:da:7f:b3:a4"]["owner_role"] == "triage"
    assert by_mac["1c:34:da:7f:b3:a4"]["kind"] == "bmc"
    assert by_mac["1c:34:da:7f:b3:a4"]["vid"] == 17
    assert by_mac["1c:34:da:7f:b3:a4"]["switch"] == "rabbit-lorax"
    assert by_mac["1c:34:da:7f:b3:a4"]["port"] == "et6b1"


def test_run_one_cycle_sweeps_desired_with_seen_macs(monkeypatch):
    """sweep_desired_not_in is called with owner_role="triage" and exactly the
    macs classified (written) this cycle."""
    from flax_classify import cycle

    _stub_targets(monkeypatch, cycle)
    monkeypatch.setattr(cycle, "upsert_desired_port", lambda pool, **kw: None)
    monkeypatch.setattr(cycle, "upsert_desired", lambda pool, **kw: None)

    desired_sweep_calls = []
    monkeypatch.setattr(cycle, "sweep_desired_not_in",
                        lambda pool, **kw: desired_sweep_calls.append(kw) or 0)

    cycle.run_one_cycle(pool=object(), resolve=lambda sw, p: "triage")

    assert len(desired_sweep_calls) == 1
    assert desired_sweep_calls[0]["owner_role"] == "triage"
    assert desired_sweep_calls[0]["keep_macs"] == \
        {"1c:34:da:7f:b3:a4", "1c:34:da:7f:b3:a2"}


def test_run_one_cycle_desired_write_failure_isolated(monkeypatch):
    """If upsert_desired raises, the cycle still completes normally: written/
    skipped are computed from classify_one succeeding, independent of the
    desired_reservations write outcome, and sweep_desired_not_in must not run
    after a mid-batch upsert_desired failure."""
    from flax_classify import cycle

    _stub_targets(monkeypatch, cycle)
    monkeypatch.setattr(cycle, "upsert_desired_port", lambda pool, **kw: None)

    def _boom(pool, **kw):
        raise RuntimeError("desired_reservations table missing (pre-migration)")

    monkeypatch.setattr(cycle, "upsert_desired", _boom)

    def _sweep_should_not_run(pool, **kw):
        raise AssertionError("sweep_desired_not_in must not run after upsert_desired raised")

    monkeypatch.setattr(cycle, "sweep_desired_not_in", _sweep_should_not_run)

    summary = cycle.run_one_cycle(pool=object(), resolve=lambda sw, p: "triage")

    # Cycle completes with its normal summary shape -- write failure did not
    # propagate, and "deleted" stays at its pre-sweep default (0).
    assert summary == {"written": 2, "deleted": 0, "skipped": 0,
                       "written_desired": 1, "purged": 0, "held": 0}


def test_run_one_cycle_makes_no_kea_writer_calls(monkeypatch):
    """Post-3b demolition: run_one_cycle must NEVER call a kea.hosts writer
    (upsert_kea_host, delete_stale_kea_hosts, purge_superseded_port_hosts,
    purge_relocated_mac_hosts). Two layers, matching the post-lane tests'
    strength: (1) none of those names are even attributes of the cycle
    module anymore; (2) belt-and-suspenders -- the kea_hosts module's writer
    functions are patched to raise if called (raising=False: Task 3 deletes
    the dead ones outright, which is an even stronger pass), and a full
    cycle run still completes, so no code path (direct or deferred import)
    reaches them."""
    import flax_classify.cycle as cycle_mod
    import flax_classify.kea_hosts as kea_hosts

    def _must_not_be_called(*a, **k):
        raise AssertionError("legacy kea writer must never be called post-3b")

    for name in ("upsert_kea_host", "delete_stale_kea_hosts",
                "purge_superseded_port_hosts", "purge_relocated_mac_hosts"):
        assert not hasattr(cycle_mod, name), \
            f"cycle.py must not import legacy kea writer {name!r}"
        monkeypatch.setattr(kea_hosts, name, _must_not_be_called,
                            raising=False)

    # Full cycle run, with desired writes wired up normally, still completes.
    _stub_targets(monkeypatch, cycle_mod)
    monkeypatch.setattr(cycle_mod, "upsert_desired_port", lambda pool, **kw: None)
    monkeypatch.setattr(cycle_mod, "upsert_desired", lambda pool, **kw: None)
    monkeypatch.setattr(cycle_mod, "sweep_desired_not_in", lambda pool, **kw: 0)

    summary = cycle_mod.run_one_cycle(pool=object(),
                                      resolve=lambda sw, p: "triage")
    assert summary["written"] == 2


def test_run_one_cycle_skips_reservation_for_non_triage_phase(monkeypatch):
    """The triage cycle must NOT write an owner="triage" reservation for a
    port that resolves to a non-triage role (that's the other lane's job) --
    but it MUST still write the port's desired_port steering row, and its
    triage keep-set must exclude the post mac so the stale triage row is
    swept. This is the rabbit-edam et10 cross-role ownership leak."""
    from flax_classify import cycle

    _stub_targets(monkeypatch, cycle)
    desired_writes = []
    monkeypatch.setattr(cycle, "upsert_desired",
                        lambda pool, **kw: desired_writes.append(kw))
    sweep_calls = []
    monkeypatch.setattr(cycle, "sweep_desired_not_in",
                        lambda pool, **kw: sweep_calls.append(kw) or 0)
    desired_port_writes = []
    monkeypatch.setattr(cycle, "upsert_desired_port",
                        lambda pool, **kw: desired_port_writes.append(kw))

    summary = cycle.run_one_cycle(pool=object(), resolve=lambda sw, p: "post")

    # No triage reservation for a post-resolved port
    assert desired_writes == []
    assert summary["written"] == 0
    # Steering preserved: the port still gets its desired_port row
    assert len(desired_port_writes) == 1
    assert desired_port_writes[0]["port"] == "Ethernet6/1"
    # The sweep keep-set includes the PRESENT post macs -> the triage sweep
    # must NOT evict them (they are re-owned by the post lane's own write, not
    # force-evicted here). Only the reservation WRITE is phase-scoped; the
    # sweep stays pure vacancy-GC over every present mac.
    assert len(sweep_calls) == 1
    assert sweep_calls[0]["owner_role"] == "triage"
    assert sweep_calls[0]["keep_macs"] == {"1c:34:da:7f:b3:a4", "1c:34:da:7f:b3:a2"}


def test_run_one_cycle_mixed_triage_and_post_does_not_evict_post(monkeypatch):
    """Motivating topology: a triage-resolved port AND a post-resolved port in
    one cycle. The triage port gets its owner=triage reservation; the post port
    gets NONE (its lane owns it) but STILL gets desired_port steering. Both macs
    are in the sweep keep-set -- a present mac that resolves to another lane is
    never force-evicted by the triage sweep. This is what protects a genuine
    triage port that mis-resolves (empty/stale geometry.json) from losing its
    reservation, while still keeping triage from CLAIMING the post port."""
    from flax_classify import cycle

    monkeypatch.setattr(cycle, "read_observe_rows", lambda pool: [
        {"switch": "rabbit-gouda", "port": "et6b1",
         "resolved": {"bmc_mac": "aa:aa:aa:00:00:01"}},
        {"switch": "rabbit-edam", "port": "et10b1",
         "resolved": {"bmc_mac": "bb:bb:bb:00:00:01"}}])
    monkeypatch.setattr(cycle, "read_switch_facts", lambda pool: {
        "rabbit-gouda": {"ports": {"Ethernet6/1": {"access_vid": 17}}},
        "rabbit-edam": {"ports": {"Ethernet10/1": {"access_vid": 25}}}})
    monkeypatch.setattr(cycle, "read_devices", lambda pool: [])
    desired_writes = []
    monkeypatch.setattr(cycle, "upsert_desired",
                        lambda pool, **kw: desired_writes.append(kw))
    sweep_calls = []
    monkeypatch.setattr(cycle, "sweep_desired_not_in",
                        lambda pool, **kw: sweep_calls.append(kw) or 0)
    dp_writes = []
    monkeypatch.setattr(cycle, "upsert_desired_port",
                        lambda pool, **kw: dp_writes.append(kw))

    resolve = lambda sw, p: "triage" if sw == "rabbit-gouda" else "post"
    cycle.run_one_cycle(pool=object(), resolve=resolve)

    # Only the triage port gets an owner="triage" reservation
    assert [w["mac"] for w in desired_writes] == ["aa:aa:aa:00:00:01"]
    assert desired_writes[0]["owner_role"] == "triage"
    # BOTH ports still get desired_port steering (reconcile keeps steering post)
    assert {dp["switch"] for dp in dp_writes} == {"rabbit-gouda", "rabbit-edam"}
    # Sweep keep-set carries BOTH present macs -> the post mac is not evicted
    assert sweep_calls[0]["keep_macs"] == {"aa:aa:aa:00:00:01", "bb:bb:bb:00:00:01"}


def test_run_one_cycle_attaches_operator_aliases_to_dns(monkeypatch):
    """user_context.aliases (read via read_aliases_for_macs) are attached to the
    matching dns record so they render alongside the primary hostname."""
    from flax_classify import cycle

    _stub_targets(monkeypatch, cycle)
    monkeypatch.setattr(cycle, "upsert_desired_port", lambda pool, **kw: None)
    monkeypatch.setattr(cycle, "upsert_desired", lambda pool, **kw: None)
    monkeypatch.setattr(cycle, "sweep_desired_not_in", lambda pool, **kw: 0)
    # only the bmc mac has aliases
    monkeypatch.setattr(cycle, "read_aliases_for_macs",
                        lambda pool, macs: {"1c:34:da:7f:b3:a4": ["leo-mgmt", "x1"]})
    captured = {}
    monkeypatch.setattr(cycle, "write_hosts_file",
                        lambda path, records: captured.update(records=records))

    cycle.run_one_cycle(pool=object(), resolve=lambda sw, p: "triage",
                        dns_hosts_path="/tmp/flax-triage-devices")

    recs = captured["records"]
    aliased = [r for r in recs if r.get("aliases")]
    assert len(aliased) == 1
    assert aliased[0]["aliases"] == ["leo-mgmt", "x1"]
    # the non-aliased record keeps the plain shape (no aliases key)
    assert any("aliases" not in r for r in recs)


def test_run_one_cycle_default_dns_path_is_triage_file(monkeypatch):
    """The triage DNS hostsdir file is flax-triage-devices (renamed from
    flax-devices; post owns flax-post-devices)."""
    from flax_classify import cycle
    monkeypatch.setattr(cycle, "read_observe_rows", lambda pool: [])
    monkeypatch.setattr(cycle, "read_switch_facts", lambda pool: {})
    monkeypatch.setattr(cycle, "read_devices", lambda pool: [])
    monkeypatch.setattr(cycle, "upsert_desired_port", lambda pool, **kw: None)
    monkeypatch.setattr(cycle, "sweep_desired_not_in", lambda pool, **kw: 0)
    calls = []
    monkeypatch.setattr(cycle, "write_hosts_file",
                        lambda path, records: calls.append(path))
    cycle.run_one_cycle(pool=object())
    assert calls == ["/etc/dnsmasq.hosts/flax-triage-devices"]


# ── Part 2 / A: the sweep keep-set includes held macs ──────────────────────

def test_cycle_sweep_keeps_held_macs(monkeypatch):
    """Link blip: the port has no access_vid this cycle -> no targets. The
    triage rows must be passed to the sweep as keep_macs (held), not swept."""
    from flax_classify import cycle
    from flax_classify.triage_hold import TriageHold

    monkeypatch.setattr(cycle, "read_observe_rows",
                        lambda pool: [{"switch": "rabbit-lorax", "port": "et6b1",
                                       "resolved": {"bmc_mac": "1c:34:da:7f:b3:a4",
                                                    "nic_mac": "1c:34:da:7f:b3:a2",
                                                    "chassis_sn": "SN1"}}])
    monkeypatch.setattr(cycle, "read_switch_facts",
                        lambda pool: {"rabbit-lorax": {"ports": {"Ethernet6/1": {}}}})
    monkeypatch.setattr(cycle, "read_devices", lambda pool: [])
    monkeypatch.setattr(cycle, "read_desired", lambda pool: [
        {"mac": "1c:34:da:7f:b3:a4", "owner_role": "triage",
         "switch": "rabbit-lorax", "port": "et6b1"},
        {"mac": "1c:34:da:7f:b3:a2", "owner_role": "triage",
         "switch": "rabbit-lorax", "port": "et6b1"}])
    monkeypatch.setattr(cycle, "upsert_desired", lambda pool, **kw: None)
    monkeypatch.setattr(cycle, "upsert_desired_port", lambda pool, **kw: None)
    swept = {}
    monkeypatch.setattr(cycle, "sweep_desired_not_in",
                        lambda pool, **kw: swept.update(kw) or 0)

    summary = cycle.run_one_cycle(pool=object(), resolve=lambda sw, p: "triage",
                                  hold=TriageHold())
    assert swept["keep_macs"] == {"1c:34:da:7f:b3:a4", "1c:34:da:7f:b3:a2"}
    assert summary["held"] == 2
    assert summary["written"] == 0


def test_cycle_without_hold_is_unchanged(monkeypatch):
    """hold=None (tests, old callers): keep-set is exactly the targets and
    read_desired is never called."""
    from flax_classify import cycle

    _stub_targets(monkeypatch, cycle)
    monkeypatch.setattr(cycle, "read_desired",
                        lambda pool: (_ for _ in ()).throw(AssertionError("read")))
    monkeypatch.setattr(cycle, "upsert_desired", lambda pool, **kw: None)
    monkeypatch.setattr(cycle, "upsert_desired_port", lambda pool, **kw: None)
    swept = {}
    monkeypatch.setattr(cycle, "sweep_desired_not_in",
                        lambda pool, **kw: swept.update(kw) or 0)
    summary = cycle.run_one_cycle(pool=object(), resolve=lambda sw, p: "triage")
    assert swept["keep_macs"] == {"1c:34:da:7f:b3:a4", "1c:34:da:7f:b3:a2"}
    assert summary["held"] == 0


def test_cycle_read_desired_failure_sweeps_nothing(monkeypatch):
    """If the hold cannot be computed, the cycle must NOT fall back to a
    targets-only sweep (that is exactly the reservation loss) -- it skips the
    sweep this cycle."""
    from flax_classify import cycle
    from flax_classify.triage_hold import TriageHold

    _stub_targets(monkeypatch, cycle)
    def boom(pool):
        raise RuntimeError("db down")
    monkeypatch.setattr(cycle, "read_desired", boom)
    monkeypatch.setattr(cycle, "upsert_desired", lambda pool, **kw: None)
    monkeypatch.setattr(cycle, "upsert_desired_port", lambda pool, **kw: None)
    calls = []
    monkeypatch.setattr(cycle, "sweep_desired_not_in",
                        lambda pool, **kw: calls.append(kw) or 0)
    summary = cycle.run_one_cycle(pool=object(), resolve=lambda sw, p: "triage",
                                  hold=TriageHold())
    assert calls == []
    assert summary["deleted"] == 0


def test_healthz_accepts_the_cycle_summary_with_held():
    """__main__ does health.record_cycle_done(**summary); a summary key the
    recorder does not accept would fail EVERY cycle."""
    from flax_classify.healthz import HealthState
    h = HealthState(stale_secs=30)
    h.record_cycle_done(written=1, deleted=0, skipped=0, written_desired=1,
                        purged=0, held=2)
    assert h.snapshot()["last_cycle"]["held"] == 2
