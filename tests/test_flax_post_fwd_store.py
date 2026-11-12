"""flax_post.fwd.store: atomic per-port row updates to post_fw.json."""
import json


_MANIFEST = {"tiogapass-openbmc": {
    "match": {"product_name": ["(?i)tioga"], "bmc_kind": "openbmc"},
    "target_version": "flax-onetree-1.1.2-202609091341",
    "check": {"method": "redfish-firmware-inventory", "id": "bmc_active"},
    "flash": {"method": "redfish-update-multipart", "artifact": "OpenBMC/fx.tar"},
    "auto": True}}


def _write_manifest(tmp_path, data):
    p = tmp_path / "bmc-firmware-versions.json"
    p.write_text(json.dumps(data))
    return p


def test_read_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("flax_post.fwd.store.STORE_PATH", str(tmp_path / "post_fw.json"))
    from flax_post.fwd import store
    assert store.read() == {}


def test_set_row_creates_and_merges(tmp_path, monkeypatch):
    p = tmp_path / "post_fw.json"
    monkeypatch.setattr("flax_post.fwd.store.STORE_PATH", str(p))
    from flax_post.fwd import store
    row = store.set_row("et6b1", bmc_ip="172.24.6.101", phase="needs_update", percent=0)
    assert row["bmc_ip"] == "172.24.6.101"
    assert row["phase"] == "needs_update"
    assert "updated_at" in row and isinstance(row["updated_at"], float)
    # second update merges into the same port, preserving prior fields
    store.set_row("et6b1", phase="flashing", percent=42)
    on_disk = store.read()
    assert on_disk["et6b1"]["bmc_ip"] == "172.24.6.101"   # preserved
    assert on_disk["et6b1"]["phase"] == "flashing"        # updated
    assert on_disk["et6b1"]["percent"] == 42


def test_set_row_isolates_ports(tmp_path, monkeypatch):
    monkeypatch.setattr("flax_post.fwd.store.STORE_PATH", str(tmp_path / "post_fw.json"))
    from flax_post.fwd import store
    store.set_row("et6b1", phase="up_to_date")
    store.set_row("et7b1", phase="fault", fault_reason="x")
    s = store.read()
    assert s["et6b1"]["phase"] == "up_to_date"
    assert s["et7b1"]["fault_reason"] == "x"


def test_set_row_is_thread_safe(tmp_path, monkeypatch):
    """Concurrent set_row for distinct ports must not lose rows to the
    read-modify-write race (the fanned-out probe + the async flash both write)."""
    import threading
    monkeypatch.setattr("flax_post.fwd.store.STORE_PATH", str(tmp_path / "post_fw.json"))
    from flax_post.fwd import store
    ports = ["et%db1" % i for i in range(64)]
    start = threading.Barrier(len(ports))   # release all writers at once to maximize collision

    def writer(port):
        start.wait()
        store.set_row(port, phase="needs_update")

    threads = [threading.Thread(target=writer, args=(p,)) for p in ports]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    on_disk = store.read()
    assert set(on_disk) == set(ports)        # no lost updates
    assert all(on_disk[p]["phase"] == "needs_update" for p in ports)


def test_read_overlays_live_target_onto_a_frozen_needs_update_row(tmp_path, monkeypatch):
    """Real bug, 2026-09-10: a row frozen at an OLD target while unreachable must
    show the CURRENT manifest target, not whatever it last saw."""
    _write_manifest(tmp_path, _MANIFEST)
    monkeypatch.setattr("flax_post.fwd.config.CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("flax_post.fwd.store.STORE_PATH", str(tmp_path / "post_fw.json"))
    from flax_post.fwd import store
    store.set_row("et10b1", current_version="flax-onetree-1.1.1",
                  target_version="flax-onetree-1.1.1",   # frozen, pre-manifest-bump value
                  phase="needs_update")
    row = store.read()["et10b1"]
    assert row["target_version"] == "flax-onetree-1.1.2-202609091341"
    assert row["phase"] == "needs_update"                # still behind -> correctly still needs_update


def test_read_overlay_flips_phase_when_current_now_matches_live_target(tmp_path, monkeypatch):
    """The exact reported symptom: current_version already equals the manifest's
    real target, but a frozen row still says needs_update against a stale target
    it saw before the manifest moved. Must flip to up_to_date, not just fix the
    displayed target string."""
    _write_manifest(tmp_path, _MANIFEST)
    monkeypatch.setattr("flax_post.fwd.config.CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("flax_post.fwd.store.STORE_PATH", str(tmp_path / "post_fw.json"))
    from flax_post.fwd import store
    store.set_row("et26b1", current_version="flax-onetree-1.1.2-202609091341",
                  target_version="flax-onetree-1.1.1", phase="needs_update")
    row = store.read()["et26b1"]
    assert row["target_version"] == "flax-onetree-1.1.2-202609091341"
    assert row["phase"] == "up_to_date"


def test_read_overlay_does_not_touch_unreachable_phase(tmp_path, monkeypatch):
    """An unreachable row's target display gets fixed, but its phase stays
    'unreachable' -- we cannot vouch for a verdict off a possibly ancient
    current_version just because the manifest changed."""
    _write_manifest(tmp_path, _MANIFEST)
    monkeypatch.setattr("flax_post.fwd.config.CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("flax_post.fwd.store.STORE_PATH", str(tmp_path / "post_fw.json"))
    from flax_post.fwd import store
    store.set_row("et10b2", current_version="flax-onetree-1.1.1",
                  target_version="flax-onetree-1.1.1", phase="unreachable",
                  fault_reason="BMC unreachable: No route to host")
    row = store.read()["et10b2"]
    assert row["target_version"] == "flax-onetree-1.1.2-202609091341"
    assert row["phase"] == "unreachable"


def test_read_overlay_does_not_touch_flashing_phase(tmp_path, monkeypatch):
    """A row mid-flash keeps its procedural phase untouched by the overlay --
    only up_to_date/needs_update are version-compare verdicts."""
    _write_manifest(tmp_path, _MANIFEST)
    monkeypatch.setattr("flax_post.fwd.config.CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("flax_post.fwd.store.STORE_PATH", str(tmp_path / "post_fw.json"))
    from flax_post.fwd import store
    store.set_row("et6b1", current_version="flax-onetree-1.1.1",
                  target_version="flax-onetree-1.1.1", phase="flashing", percent=42)
    row = store.read()["et6b1"]
    assert row["target_version"] == "flax-onetree-1.1.2-202609091341"  # display still fixed
    assert row["phase"] == "flashing"                                  # but not reinterpreted
    assert row["percent"] == 42


def test_read_overlay_fails_open_with_no_manifest(tmp_path, monkeypatch):
    """Missing/unreadable manifest -> every row is returned exactly as stored."""
    monkeypatch.setattr("flax_post.fwd.config.CONFIG_DIR", str(tmp_path))  # no manifest file here
    monkeypatch.setattr("flax_post.fwd.store.STORE_PATH", str(tmp_path / "post_fw.json"))
    from flax_post.fwd import store
    store.set_row("et6b1", current_version="flax-onetree-1.1.1",
                  target_version="flax-onetree-1.1.1", phase="needs_update")
    row = store.read()["et6b1"]
    assert row["target_version"] == "flax-onetree-1.1.1"
    assert row["phase"] == "needs_update"


def test_read_overlay_leaves_rows_without_a_target_version_key_alone(tmp_path, monkeypatch):
    _write_manifest(tmp_path, _MANIFEST)
    monkeypatch.setattr("flax_post.fwd.config.CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("flax_post.fwd.store.STORE_PATH", str(tmp_path / "post_fw.json"))
    from flax_post.fwd import store
    store.set_row("et6b1", phase="flashing", percent=0)   # no version fields at all
    row = store.read()["et6b1"]
    assert "target_version" not in row


def test_sweep_removes_rows_for_ports_no_longer_reserved(tmp_path, monkeypatch):
    monkeypatch.setattr("flax_post.fwd.config.CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("flax_post.fwd.store.STORE_PATH", str(tmp_path / "post_fw.json"))
    from flax_post.fwd import store
    store.set_row("et6b1", phase="up_to_date")
    store.set_row("et7b1", phase="unreachable")
    store.set_row("et8b1", phase="needs_update")
    removed = store.sweep({"et6b1", "et8b1"})
    assert removed == ["et7b1"]
    assert set(store.read()) == {"et6b1", "et8b1"}


def test_sweep_is_a_noop_when_nothing_is_stale(tmp_path, monkeypatch):
    monkeypatch.setattr("flax_post.fwd.config.CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("flax_post.fwd.store.STORE_PATH", str(tmp_path / "post_fw.json"))
    from flax_post.fwd import store
    store.set_row("et6b1", phase="up_to_date")
    assert store.sweep({"et6b1"}) == []
    assert set(store.read()) == {"et6b1"}


def test_sweep_on_missing_store_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr("flax_post.fwd.store.STORE_PATH", str(tmp_path / "nope.json"))
    from flax_post.fwd import store
    assert store.sweep({"et6b1"}) == []


def test_overlay_leaves_oem_rows_alone(tmp_path, monkeypatch):
    _write_manifest(tmp_path, _MANIFEST)
    monkeypatch.setattr("flax_post.fwd.config.CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("flax_post.fwd.store.STORE_PATH", str(tmp_path / "post_fw.json"))
    from flax_post.fwd import store
    store.set_row("et7b1", phase="oem", current_version="report-only", target_version="14.00")
    store.set_row("et8b1", phase="needs_update", current_version="x", target_version="old")
    rows = store.read()
    assert rows["et7b1"]["target_version"] == "14.00"
    assert rows["et8b1"]["target_version"] == "flax-onetree-1.1.2-202609091341"
