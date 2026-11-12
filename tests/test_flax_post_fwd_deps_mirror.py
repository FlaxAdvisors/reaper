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
