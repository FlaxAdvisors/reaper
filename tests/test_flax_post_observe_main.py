"""Producer entrypoint: one guarded pass."""
from unittest import mock

from flax_post.observe import __main__ as obs


def test_run_pass_runs_producer():
    calls = []
    obs.run_pass(ipmi_once=lambda: calls.append("ipmi"))
    assert calls == ["ipmi"]


def test_run_pass_swallows_errors():
    def boom():
        raise RuntimeError("ipmi blew up")
    obs.run_pass(ipmi_once=boom)   # must not raise


def test_run_power_pass_runs_producer():
    calls = []
    obs.run_power_pass(power_once=lambda: calls.append("power"))
    assert calls == ["power"]


def test_run_power_pass_swallows_errors():
    def boom():
        raise RuntimeError("power pass blew up")
    obs.run_power_pass(power_once=boom)   # must not raise


def test_run_gc_pass_calls_gc_and_swallows_errors():
    with mock.patch.object(obs, "gc") as g:
        g.gc_post_state.return_value = {"deleted": 2, "latched": 1, "cleared": 0}
        obs.run_gc_pass()
        g.gc_post_state.assert_called_once()


def test_run_gc_pass_never_raises():
    def boom():
        raise RuntimeError("db down")
    obs.run_gc_pass(gc_once=boom)   # must not raise


def test_run_pass_default_wires_record_observation(monkeypatch):
    """The production path (no injected ipmi_once) must pass the REAL
    records.record_observation into ipmi.run_once."""
    from flax_post.observe import __main__ as main
    from flax_post import records
    seen = {}
    monkeypatch.setattr(main.ipmi, "run_once",
                        lambda **kw: seen.update(kw))
    main.run_pass()
    assert seen.get("record_observation") is records.record_observation


def test_slot_ports_from_geometry(monkeypatch):
    from flax_post.observe import __main__ as m
    from flax_post import geometry
    monkeypatch.setattr(geometry, "load_geometry",
                        lambda: {"slots": [{"port": "et6b1"}, {"port": "et8b4"}, {"port": None}]})
    assert m.slot_ports() == ["et6b1", "et8b4"]


def test_run_retention_pass_calls_retention_with_no_arguments():
    # The wrapper only decides to call; keep/interval/enabled come from the
    # module's env-derived defaults. The log line itself belongs to
    # retention.run_retention and is asserted in test_flax_post_retention.py.
    from flax_post.observe import __main__ as obs
    with mock.patch.object(obs, "retention") as ret:
        ret.run_retention.return_value = {"records": 9, "empty_serial_records": 0,
                                          "dut_rows": 1, "capped_remaining": 0}
        obs.run_retention_pass()
    ret.run_retention.assert_called_once_with()


def test_run_retention_pass_swallows_errors():
    from flax_post.observe import __main__ as obs

    def boom():
        raise RuntimeError("db down")
    obs.run_retention_pass(retention_once=boom)      # must not raise


def test_run_retention_pass_accepts_a_skipped_sweep():
    from flax_post.observe import __main__ as obs
    obs.run_retention_pass(retention_once=lambda: None)   # disabled/not due: no crash


def test_env_template_carries_the_retention_vars():
    from pathlib import Path
    body = (Path(__file__).resolve().parents[1] /
            "roles/apply_flax_post/templates/flax-post.env.j2").read_text()
    for var in ("FLAX_RECORDS_RETENTION_ENABLED", "FLAX_RECORDS_KEEP",
                "FLAX_RECORDS_RETENTION_INTERVAL_SECS", "FLAX_RECORDS_MAX_DELETES"):
        assert var + "={{" in body


def test_role_defaults_ship_the_sweep_disabled():
    from pathlib import Path
    import yaml
    d = yaml.safe_load((Path(__file__).resolve().parents[1] /
                        "roles/apply_flax_post/defaults/main.yml").read_text())
    assert d["apply_flax_post_records_retention_enabled"] is False
    assert d["apply_flax_post_records_keep"] == 5
    assert d["apply_flax_post_records_retention_interval_secs"] == 3600
    assert d["apply_flax_post_records_max_deletes"] == 20000
