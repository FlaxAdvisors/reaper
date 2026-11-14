from flax_post.fwd import __main__ as fwd_main


def test_deps_set_row_mirrors_to_state(monkeypatch):
    file_writes, state_writes = [], []
    monkeypatch.setattr(fwd_main.store, "set_row",
                        lambda port, **f: (file_writes.append((port, f)), dict(port=port, **f))[1])
    monkeypatch.setattr(fwd_main.state, "set_state",
                        lambda port, **kw: state_writes.append((port, kw)))
    # build a _Deps without touching creds/manifest/DB
    d = fwd_main._Deps.__new__(fwd_main._Deps)
    d._install_set_row()
    d.set_row("et6b1", bmc_ip="172.25.6.101", current_version="1.0.8",
              target_version="1.0.8", phase="done")
    assert file_writes[0][0] == "et6b1"
    assert state_writes[0][0] == "et6b1"
    assert state_writes[0][1]["fw_bmc"]["phase"] == "done"


def test_deps_set_row_survives_state_error(monkeypatch):
    monkeypatch.setattr(fwd_main.store, "set_row", lambda port, **f: dict(port=port, **f))
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(fwd_main.state, "set_state", boom)
    d = fwd_main._Deps.__new__(fwd_main._Deps)
    d._install_set_row()
    # must not raise even though the mirror fails
    result = d.set_row("et6b1", phase="flashing", percent=10)
    # mirror failed, but the store row must still come back unchanged
    assert result == {"port": "et6b1", "mode": "detect", "phase": "flashing", "percent": 10}


def test_deps_set_row_lets_a_caller_override_mode(monkeypatch):
    """`mode=config.MODE, **fields` raised TypeError on a caller that passed
    its own mode=; the wrapper defaults the field instead of forcing it."""
    monkeypatch.setattr(fwd_main.store, "set_row", lambda port, **f: dict(port=port, **f))
    monkeypatch.setattr(fwd_main.state, "set_state", lambda port, **kw: None)
    d = fwd_main._Deps.__new__(fwd_main._Deps)
    d._install_set_row()
    assert d.set_row("et6b1", phase="done")["mode"] == fwd_main.config.MODE
    assert d.set_row("et6b1", phase="done", mode="enforce")["mode"] == "enforce"


def test_fw_records_carry_the_nodes_effective_order(monkeypatch):
    """fw-flash / fw-action records stamp the node's EFFECTIVE order and that
    order's customer, not the header's (spec 2026-11-09 order labels)."""
    flashes, actions = [], []
    monkeypatch.setattr(fwd_main.records, "node_identity", lambda mac: ("p0", "SN1"))
    monkeypatch.setattr(fwd_main.records, "record_flash", lambda **kw: flashes.append(kw["keys"]))
    monkeypatch.setattr(fwd_main.records, "record_action", lambda **kw: actions.append(kw["keys"]))
    monkeypatch.setattr(fwd_main.store, "read", lambda: {})
    monkeypatch.setattr(fwd_main.state, "read_settings",
                        lambda: {"order_no": "RRORD-1033", "customer": "Barivia"})
    monkeypatch.setattr(fwd_main.state, "read_nodes", lambda macs=None: {"aa:01": {
        "bmc_mac": "aa:01", "serial": "SN1", "assigned_order_no": "RRORD-100",
        "assigned_serial": "SN1"}})
    monkeypatch.setattr(fwd_main.state, "order_labels", lambda: {
        "RRORD-100": {"customer": "OVH"}, "RRORD-1033": {"customer": "Barivia"}})
    d = fwd_main._Deps.__new__(fwd_main._Deps)
    d.record_flash("et6b1", "aa:01", "done", [])
    d.record_action("et6b1", "aa:01", "reset", True, "")
    assert flashes == [{"order": "RRORD-100", "customer": "OVH"}]
    assert actions == [{"order": "RRORD-100", "customer": "OVH"}]


def test_fw_records_without_order_labels_stamp_order_only(monkeypatch):
    keys = []
    monkeypatch.setattr(fwd_main.records, "node_identity", lambda mac: ("p0", "SN1"))
    monkeypatch.setattr(fwd_main.records, "record_action", lambda **kw: keys.append(kw["keys"]))
    monkeypatch.setattr(fwd_main.state, "read_settings",
                        lambda: {"order_no": "RRORD-1033", "customer": "Barivia"})
    monkeypatch.setattr(fwd_main.state, "read_nodes", lambda macs=None: {})
    def boom():
        raise RuntimeError("post_order unreadable")
    monkeypatch.setattr(fwd_main.state, "order_labels", boom)
    d = fwd_main._Deps.__new__(fwd_main._Deps)
    d.record_action("et6b1", "aa:01", "reset", True, "")
    assert keys == [{"order": "RRORD-1033"}]
