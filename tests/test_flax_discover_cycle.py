import flax_discover.cycle as cyc
from flax_discover.cycle import Discoverer


class FakeInputs:
    def __init__(self, observe, facts, devices):
        self.observe, self.facts, self.devices = observe, facts, devices


def _patch_reads(monkeypatch, inputs):
    monkeypatch.setattr(cyc, "read_observe_rows", lambda pool: inputs.observe)
    monkeypatch.setattr(cyc, "read_switch_facts", lambda pool: inputs.facts)
    monkeypatch.setattr(cyc, "read_devices", lambda pool: inputs.devices)


def _capture_upserts(monkeypatch):
    calls = []
    monkeypatch.setattr(cyc, "upsert_device",
                        lambda pool, **kw: calls.append(kw))
    return calls


def _capture_supersedes(monkeypatch, rowcount=0):
    calls = []

    def _fake(pool, *, switch, port, keep_macs):
        calls.append({"switch": switch, "port": port, "keep_macs": keep_macs})
        return rowcount

    monkeypatch.setattr(cyc, "supersede_port", _fake)
    return calls


def _disco(tmp_path, **kw):
    (tmp_path / "leopard.txt").write_text("Leopard-DDR4\n")
    return Discoverer(family_map_dir=str(tmp_path), base_secs=30,
                      max_secs=3600, **kw)


def test_known_family_written_to_bmc_and_host(tmp_path, monkeypatch):
    inputs = FakeInputs(
        observe=[{"switch": "rabbit-x", "port": "et6b1",
                  "resolved": {"bmc_mac": "0c:42:a1:00:00:64",
                               "nic_mac": "0c:42:a1:00:00:62",
                               "chassis_sn": "SN123",
                               "product_name": "Leopard-DDR4"}}],
        facts={"rabbit-x": {"ports": {"Ethernet6/1": {"junk_macs": []}}}},
        devices=[])
    _patch_reads(monkeypatch, inputs)
    calls = _capture_upserts(monkeypatch)
    _disco(tmp_path).run_one_cycle(pool=None, now=0.0)

    by_mac = {c["mac"]: c for c in calls}
    assert by_mac["0c:42:a1:00:00:64"]["kind"] == "bmc"
    assert by_mac["0c:42:a1:00:00:64"]["latched"]["family"] == "leopard"
    assert by_mac["0c:42:a1:00:00:64"]["latched"]["serial"] == "SN123"
    assert by_mac["0c:42:a1:00:00:64"]["latched"]["product_name"] == "Leopard-DDR4"
    assert by_mac["0c:42:a1:00:00:62"]["kind"] == "host"
    assert by_mac["0c:42:a1:00:00:62"]["latched"]["family"] == "leopard"


def test_vm_mac_gets_kind_vm_and_vm_n(tmp_path, monkeypatch):
    inputs = FakeInputs(
        observe=[{"switch": "rabbit-x", "port": "et6b1",
                  "resolved": {"bmc_mac": "0c:42:a1:00:00:64",
                               "nic_mac": "0c:42:a1:00:00:62",
                               "product_name": "Leopard-DDR4"}}],
        facts={"rabbit-x": {"ports": {"Ethernet6/1": {
            "junk_macs": [{"mac": "02:00:00:00:00:01", "reason": "x"},
                          {"mac": "0c:42:a1:99:99:99", "reason": "y"}]}}}},
        devices=[])
    _patch_reads(monkeypatch, inputs)
    calls = _capture_upserts(monkeypatch)
    _disco(tmp_path).run_one_cycle(pool=None, now=0.0)

    vms = [c for c in calls if c["kind"] == "vm"]
    assert len(vms) == 1                                  # only the LAA mac
    assert vms[0]["mac"] == "02:00:00:00:00:01"
    assert vms[0]["latched"]["vm_n"] == 1
    assert vms[0]["latched"]["family"] == "leopard"


def test_unknown_family_backs_off(tmp_path, monkeypatch):
    inputs = FakeInputs(
        observe=[{"switch": "rabbit-x", "port": "et6b1",
                  "resolved": {"bmc_mac": "0c:42:a1:00:00:64",
                               "product_name": "MysteryBox9000"}}],
        facts={"rabbit-x": {"ports": {"Ethernet6/1": {"junk_macs": []}}}},
        devices=[])
    _patch_reads(monkeypatch, inputs)
    calls = _capture_upserts(monkeypatch)
    d = _disco(tmp_path)
    d.run_one_cycle(pool=None, now=0.0)
    assert calls[0]["latched"].get("family") in (None, "unknown")
    # second cycle within back-off window: unknown is NOT re-matched
    assert d.backoff.due("0c:42:a1:00:00:64", now=5.0) is False


def test_latched_family_not_rederived(tmp_path, monkeypatch):
    # product_name now blank, but family already latched -> keep leopard
    inputs = FakeInputs(
        observe=[{"switch": "rabbit-x", "port": "et6b1",
                  "resolved": {"bmc_mac": "0c:42:a1:00:00:64",
                               "product_name": None}}],
        facts={"rabbit-x": {"ports": {"Ethernet6/1": {"junk_macs": []}}}},
        devices=[{"mac": "0c:42:a1:00:00:64", "switch": "rabbit-x",
                  "port": "et6b1", "kind": "bmc",
                  "latched": {"family": "leopard"}}])
    _patch_reads(monkeypatch, inputs)
    calls = _capture_upserts(monkeypatch)
    _disco(tmp_path).run_one_cycle(pool=None, now=0.0)
    bmc = next(c for c in calls if c["mac"] == "0c:42:a1:00:00:64")
    assert bmc["latched"]["family"] == "leopard"


def test_two_vms_on_one_port_get_distinct_vm_n(tmp_path, monkeypatch):
    inputs = FakeInputs(
        observe=[{"switch": "rabbit-x", "port": "et6b1",
                  "resolved": {"bmc_mac": "0c:42:a1:00:00:64",
                               "product_name": "Leopard-DDR4"}}],
        facts={"rabbit-x": {"ports": {"Ethernet6/1": {
            "junk_macs": [{"mac": "02:00:00:00:00:01", "reason": "x"},
                          {"mac": "02:00:00:00:00:02", "reason": "y"}]}}}},
        devices=[])
    _patch_reads(monkeypatch, inputs)
    calls = _capture_upserts(monkeypatch)
    _disco(tmp_path).run_one_cycle(pool=None, now=0.0)

    vms = [c for c in calls if c["kind"] == "vm"]
    assert len(vms) == 2
    by_mac = {c["mac"]: c["latched"]["vm_n"] for c in vms}
    assert set(by_mac) == {"02:00:00:00:00:01", "02:00:00:00:00:02"}
    assert set(by_mac.values()) == {1, 2}


def test_vm_n_reused_from_existing_device(tmp_path, monkeypatch):
    inputs = FakeInputs(
        observe=[{"switch": "rabbit-x", "port": "et6b1",
                  "resolved": {"bmc_mac": "0c:42:a1:00:00:64",
                               "product_name": "Leopard-DDR4"}}],
        facts={"rabbit-x": {"ports": {"Ethernet6/1": {
            "junk_macs": [{"mac": "02:00:00:00:00:09", "reason": "x"}]}}}},
        devices=[{"mac": "02:00:00:00:00:09", "switch": "rabbit-x",
                  "port": "et6b1", "kind": "vm",
                  "latched": {"family": "leopard", "vm_n": 7}}])
    _patch_reads(monkeypatch, inputs)
    calls = _capture_upserts(monkeypatch)
    _disco(tmp_path).run_one_cycle(pool=None, now=0.0)

    vm = next(c for c in calls if c["mac"] == "02:00:00:00:00:09")
    assert vm["kind"] == "vm"
    assert vm["latched"]["vm_n"] == 7


def test_unknown_family_stamped_on_vm(tmp_path, monkeypatch):
    inputs = FakeInputs(
        observe=[{"switch": "rabbit-x", "port": "et6b1",
                  "resolved": {"bmc_mac": "0c:42:a1:00:00:64",
                               "product_name": "MysteryBox9000"}}],
        facts={"rabbit-x": {"ports": {"Ethernet6/1": {
            "junk_macs": [{"mac": "02:00:00:00:00:01", "reason": "x"}]}}}},
        devices=[])
    _patch_reads(monkeypatch, inputs)
    calls = _capture_upserts(monkeypatch)
    _disco(tmp_path).run_one_cycle(pool=None, now=0.0)

    vm = next(c for c in calls if c["kind"] == "vm")
    assert vm["latched"]["family"] == "unknown"


# ---- BMC-arrival port supersede (one BMC per port) ----

def _bmc_swap_inputs(new_bmc, existing_bmc):
    return FakeInputs(
        observe=[{"switch": "rabbit-x", "port": "et6b1",
                  "resolved": {"bmc_mac": new_bmc,
                               "nic_mac": "0c:42:a1:00:00:62",
                               "product_name": "Leopard-DDR4"}}],
        facts={"rabbit-x": {"ports": {"Ethernet6/1": {"junk_macs": []}}}},
        devices=[{"mac": existing_bmc, "switch": "rabbit-x", "port": "et6b1",
                  "kind": "bmc", "latched": {"family": "leopard"}}])


def test_bmc_swap_supersedes_port_keeping_new_chassis(tmp_path, monkeypatch):
    # A DIFFERENT bmc arrives at et6b1 than the one recorded -> supersede,
    # keeping exactly this cycle's chassis (new bmc + paired nic).
    inputs = _bmc_swap_inputs(new_bmc="0c:42:a1:00:00:c8",
                              existing_bmc="0c:42:a1:00:00:64")
    _patch_reads(monkeypatch, inputs)
    _capture_upserts(monkeypatch)
    sweeps = _capture_supersedes(monkeypatch, rowcount=2)
    summary = _disco(tmp_path).run_one_cycle(pool=None, now=0.0)

    assert len(sweeps) == 1
    assert sweeps[0]["switch"] == "rabbit-x"
    assert sweeps[0]["port"] == "et6b1"
    assert set(sweeps[0]["keep_macs"]) == {
        "0c:42:a1:00:00:c8", "0c:42:a1:00:00:62"}
    assert summary["superseded"] == 2


def test_same_bmc_does_not_supersede(tmp_path, monkeypatch):
    # Same bmc re-observed -> NO supersede (idempotent, no transient wipe).
    inputs = _bmc_swap_inputs(new_bmc="0c:42:a1:00:00:64",
                              existing_bmc="0c:42:a1:00:00:64")
    _patch_reads(monkeypatch, inputs)
    _capture_upserts(monkeypatch)
    sweeps = _capture_supersedes(monkeypatch)
    summary = _disco(tmp_path).run_one_cycle(pool=None, now=0.0)

    assert sweeps == []
    assert summary["superseded"] == 0


def test_first_enrollment_does_not_supersede(tmp_path, monkeypatch):
    # No existing bmc at the port -> first enrollment, NO supersede.
    inputs = FakeInputs(
        observe=[{"switch": "rabbit-x", "port": "et6b1",
                  "resolved": {"bmc_mac": "0c:42:a1:00:00:64",
                               "nic_mac": "0c:42:a1:00:00:62",
                               "product_name": "Leopard-DDR4"}}],
        facts={"rabbit-x": {"ports": {"Ethernet6/1": {"junk_macs": []}}}},
        devices=[])
    _patch_reads(monkeypatch, inputs)
    _capture_upserts(monkeypatch)
    sweeps = _capture_supersedes(monkeypatch)
    summary = _disco(tmp_path).run_one_cycle(pool=None, now=0.0)

    assert sweeps == []
    assert summary["superseded"] == 0


def test_bmc_disappeared_enrolls_host_only_and_supersedes_stale(tmp_path, monkeypatch):
    # Observe flipped the port to host-only (probe_flip_host): bmc_mac=None,
    # nic_mac set. The stale prior heuristic rows (a wrongly-labelled bmc + a
    # phantom host) must be superseded and the real host (re)enrolled.
    # Regression: discover used to `continue` on bmc_mac=None, freezing the
    # port at its pre-flip snapshot forever (rabbit-gouda/et10b4, 2026-06-24).
    inputs = FakeInputs(
        observe=[{"switch": "rabbit-gouda", "port": "et10b4",
                  "resolved": {"bmc_mac": None,
                               "nic_mac": "98:03:9b:a8:f0:8a",
                               "product_name": None}}],
        facts={"rabbit-gouda": {"ports": {"Ethernet10/4": {"junk_macs": []}}}},
        devices=[
            {"mac": "98:03:9b:a8:f0:8a", "switch": "rabbit-gouda",
             "port": "et10b4", "kind": "bmc",
             "latched": {"family": "tiogapass"}},
            {"mac": "98:03:9b:a8:f0:88", "switch": "rabbit-gouda",
             "port": "et10b4", "kind": "host",
             "latched": {"family": "tiogapass"}},
        ])
    _patch_reads(monkeypatch, inputs)
    calls = _capture_upserts(monkeypatch)
    sweeps = _capture_supersedes(monkeypatch, rowcount=1)
    _disco(tmp_path).run_one_cycle(pool=None, now=0.0)

    # the real host NIC is (re)enrolled as host (it was wrongly recorded 'bmc')
    host = next(c for c in calls if c["mac"] == "98:03:9b:a8:f0:8a")
    assert host["kind"] == "host"
    assert host["latched"]["family"] == "tiogapass"
    assert not any(c["kind"] == "bmc" for c in calls)   # there is no bmc
    # the phantom host + the stale bmc row are swept, keeping only the nic
    assert len(sweeps) == 1
    assert sweeps[0]["keep_macs"] == ["98:03:9b:a8:f0:8a"]


def test_empty_resolved_skips_port_no_wipe(tmp_path, monkeypatch):
    # A fully-empty resolved (no bmc, no nic) is a transient/no-data poll:
    # never upsert and never supersede. A wipe-on-absence would be data loss.
    inputs = FakeInputs(
        observe=[{"switch": "rabbit-gouda", "port": "et10b4",
                  "resolved": {"bmc_mac": None, "nic_mac": None}}],
        facts={"rabbit-gouda": {"ports": {"Ethernet10/4": {"junk_macs": []}}}},
        devices=[{"mac": "98:03:9b:a8:f0:8a", "switch": "rabbit-gouda",
                  "port": "et10b4", "kind": "bmc",
                  "latched": {"family": "tiogapass"}}])
    _patch_reads(monkeypatch, inputs)
    calls = _capture_upserts(monkeypatch)
    sweeps = _capture_supersedes(monkeypatch)
    summary = _disco(tmp_path).run_one_cycle(pool=None, now=0.0)

    assert calls == []
    assert sweeps == []
    assert summary == {"written": 0, "superseded": 0, "swept": 0}


def test_bmc_swap_keep_set_includes_this_cycle_vms(tmp_path, monkeypatch):
    # The keep set must carry the new chassis's VMs assigned this cycle,
    # so they are NOT swept along with the old occupant.
    inputs = FakeInputs(
        observe=[{"switch": "rabbit-x", "port": "et6b1",
                  "resolved": {"bmc_mac": "0c:42:a1:00:00:c8",
                               "nic_mac": "0c:42:a1:00:00:62",
                               "product_name": "Leopard-DDR4"}}],
        facts={"rabbit-x": {"ports": {"Ethernet6/1": {
            "junk_macs": [{"mac": "02:00:00:00:00:01", "reason": "x"}]}}}},
        devices=[{"mac": "0c:42:a1:00:00:64", "switch": "rabbit-x",
                  "port": "et6b1", "kind": "bmc",
                  "latched": {"family": "leopard"}}])
    _patch_reads(monkeypatch, inputs)
    _capture_upserts(monkeypatch)
    sweeps = _capture_supersedes(monkeypatch, rowcount=1)
    _disco(tmp_path).run_one_cycle(pool=None, now=0.0)

    assert len(sweeps) == 1
    assert set(sweeps[0]["keep_macs"]) == {
        "0c:42:a1:00:00:c8", "0c:42:a1:00:00:62", "02:00:00:00:00:01"}


def _capture_sweeps(monkeypatch, rowcount=0):
    calls = []

    def _fake(pool, *, switch, port, debounce_secs):
        calls.append({"switch": switch, "port": port,
                      "debounce_secs": debounce_secs})
        return rowcount

    monkeypatch.setattr(cyc, "sweep_vacant_port", _fake)
    return calls


def test_nolink_port_with_devices_is_swept(tmp_path, monkeypatch):
    # No observe occupant (so the main loop skips it); a devices row exists; the
    # port's CURRENT link is nolink -> vacancy sweep must fire for that port.
    inputs = FakeInputs(
        observe=[],
        facts={"rabbit-x": {"ports": {"Ethernet6/1": {"link": "nolink"}}}},
        devices=[{"mac": "aa:bb:cc:00:00:64", "switch": "rabbit-x",
                  "port": "et6b1", "kind": "bmc", "latched": {}}])
    _patch_reads(monkeypatch, inputs)
    _capture_upserts(monkeypatch)
    sweeps = _capture_sweeps(monkeypatch, rowcount=1)
    summary = _disco(tmp_path, vacancy_debounce_secs=600).run_one_cycle(
        pool=None, now=0.0)
    assert sweeps == [{"switch": "rabbit-x", "port": "et6b1",
                       "debounce_secs": 600}]
    assert summary["swept"] == 1


def test_linked_port_with_devices_is_not_swept(tmp_path, monkeypatch):
    # Same shape but link is UP (e.g. a powered-off-but-NC-SI host) -> never swept.
    inputs = FakeInputs(
        observe=[],
        facts={"rabbit-x": {"ports": {"Ethernet6/1": {"link": "link"}}}},
        devices=[{"mac": "aa:bb:cc:00:00:64", "switch": "rabbit-x",
                  "port": "et6b1", "kind": "bmc", "latched": {}}])
    _patch_reads(monkeypatch, inputs)
    _capture_upserts(monkeypatch)
    sweeps = _capture_sweeps(monkeypatch)
    summary = _disco(tmp_path).run_one_cycle(pool=None, now=0.0)
    assert sweeps == []
    assert summary["swept"] == 0


def test_port_absent_from_switch_facts_is_not_swept(tmp_path, monkeypatch):
    # Switch poll gap: the port is not in facts at all (link unknown) -> not swept.
    inputs = FakeInputs(
        observe=[],
        facts={"rabbit-x": {"ports": {}}},
        devices=[{"mac": "aa:bb:cc:00:00:64", "switch": "rabbit-x",
                  "port": "et6b1", "kind": "bmc", "latched": {}}])
    _patch_reads(monkeypatch, inputs)
    _capture_upserts(monkeypatch)
    sweeps = _capture_sweeps(monkeypatch)
    summary = _disco(tmp_path).run_one_cycle(pool=None, now=0.0)
    assert sweeps == []
    assert summary["swept"] == 0
