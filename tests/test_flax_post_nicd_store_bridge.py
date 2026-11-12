import importlib
from flax_post.nicd import bridge


def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("FLAX_POST_NIC_STORE", str(tmp_path / "post_nic_fw.json"))
    import flax_post.nicd.store as store
    return importlib.reload(store)


def test_set_row_merges_and_persists(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.set_row("et10b1", phase="needs_update", devices=[{"pci": "5e:00.0"}])
    store.set_row("et10b1", phase="flashing")
    row = store.read()["et10b1"]
    assert row["phase"] == "flashing" and row["devices"] == [{"pci": "5e:00.0"}]
    assert row["port"] == "et10b1" and "updated_at" in row


def test_read_missing_is_empty(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    assert store.read() == {}


def test_fw_nic_slice_only_ui_fields():
    row = {"port": "et10b1", "phase": "needs_update", "updated_at": 1.0,
           "devices": [{"pci": "5e:00.0", "phase": "needs_update"}], "fault_reason": ""}
    assert bridge.fw_nic_slice(row) == {"phase": "needs_update",
                                        "devices": [{"pci": "5e:00.0", "phase": "needs_update"}],
                                        "fault_reason": "", "mode": None}


def test_mirror_row_calls_set_state():
    calls = {}
    bridge.mirror_row(lambda port, **kw: calls.update(port=port, **kw), "et10b1",
                      {"phase": "up_to_date", "devices": [], "fault_reason": ""})
    assert calls["port"] == "et10b1" and calls["fw_nic"]["phase"] == "up_to_date"

def test_slice_carries_mode():
    assert bridge.fw_nic_slice({"phase": "needs_update", "mode": "detect"})["mode"] == "detect"
