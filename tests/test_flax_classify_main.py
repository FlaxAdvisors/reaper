"""Unit tests for flax_classify.__main__ post-reconcile config parsing.

Post-3b Task 4 cutover: POST_LINKDOWN_EVICT_SECS / POST_REPLACE_DEBOUNCE_CYCLES
env-var reads are RETIRED -- _post_reconcile_cfg is registry-first
(role_defs["post"]["policy"]["lifecycle"]), falling back to the code defaults
per-key when the registry carries no lifecycle block (or is empty/absent,
e.g. an invalid/missing roles.d -- deploy-order safety). Task 5 ride-along:
the linkdown_evict_secs code default is -1 (Rule 3 fail-safe DISABLED), not
900 -- a registry-degraded startup must never arm Rule 3 against
potentially months-old link_down_since timers. replace_debounce_cycles
keeps its 2-cycle code default."""
import importlib

from flax_classify.__main__ import _post_dns_records


def test_post_dns_records_selects_only_post_rows():
    actuals = [
        {"source": "post", "hostname": "fl001-et10b1-bmc",
         "ipv4": "172.25.10.101", "ipv6": "fd00:25::10:101"},
        {"source": "triage", "hostname": "tiogapass10b1-bmc",
         "ipv4": "172.25.10.1", "ipv6": "fd00:25::10:1"},
        {"source": None, "hostname": "legacy", "ipv4": "10.0.0.1", "ipv6": None},
        {"source": "post", "hostname": None, "ipv4": "172.25.10.9", "ipv6": None},
    ]
    recs = _post_dns_records(actuals)
    assert recs == [{"hostname": "fl001-et10b1-bmc",
                     "ipv4": "172.25.10.101", "ipv6": "fd00:25::10:101"}]


def test_post_reconcile_cfg_defaults_when_registry_has_no_lifecycle():
    m = importlib.import_module("flax_classify.__main__")
    cfg = m._post_reconcile_cfg(cycle_secs=30.0, role_defs={})
    assert cfg["linkdown_evict_secs"] == -1.0
    assert cfg["replace_debounce_secs"] == 60.0  # 2 cycles * 30s (code default)
    assert cfg["flash_macs"] == set()


def test_post_reconcile_cfg_registry_first():
    m = importlib.import_module("flax_classify.__main__")
    role_defs = {"post": {"policy": {"lifecycle": {
        "linkdown_evict_secs": 120, "replace_debounce_cycles": 3}}}}
    cfg = m._post_reconcile_cfg(cycle_secs=30.0, role_defs=role_defs)
    assert cfg["linkdown_evict_secs"] == 120.0
    assert cfg["replace_debounce_secs"] == 90.0  # 3 cycles * 30s


def test_post_reconcile_cfg_registry_disable_replace():
    m = importlib.import_module("flax_classify.__main__")
    role_defs = {"post": {"policy": {"lifecycle": {
        "replace_debounce_cycles": -1}}}}
    cfg = m._post_reconcile_cfg(cycle_secs=30.0, role_defs=role_defs)
    assert cfg["replace_debounce_secs"] < 0  # disabled sentinel


def test_post_reconcile_cfg_partial_lifecycle_falls_back_per_key():
    """Only one key present in the registry -> the other key still comes
    from the code default (per-key fallback, not all-or-nothing)."""
    m = importlib.import_module("flax_classify.__main__")
    role_defs = {"post": {"policy": {"lifecycle": {"linkdown_evict_secs": 45}}}}
    cfg = m._post_reconcile_cfg(cycle_secs=10.0, role_defs=role_defs)
    assert cfg["linkdown_evict_secs"] == 45.0
    assert cfg["replace_debounce_secs"] == 20.0  # code default cycles=2 * 10s


def test_post_reconcile_cfg_missing_post_role_uses_code_defaults():
    """role_defs present but with no 'post' key at all (e.g. triage-only
    registry) -- must not KeyError, degrades to code defaults."""
    m = importlib.import_module("flax_classify.__main__")
    role_defs = {"triage": {"policy": {"vid": {"default": 16}}}}
    cfg = m._post_reconcile_cfg(cycle_secs=30.0, role_defs=role_defs)
    assert cfg["linkdown_evict_secs"] == -1.0
    assert cfg["replace_debounce_secs"] == 60.0


def test_post_reconcile_cfg_env_vars_no_longer_read(monkeypatch):
    """POST_LINKDOWN_EVICT_SECS / POST_REPLACE_DEBOUNCE_CYCLES env reads were
    retired in the registry-first cutover (Task 4) -- os.environ must have
    zero effect on the computed cfg now, regardless of role_defs."""
    m = importlib.import_module("flax_classify.__main__")
    monkeypatch.setenv("POST_LINKDOWN_EVICT_SECS", "1")
    monkeypatch.setenv("POST_REPLACE_DEBOUNCE_CYCLES", "99")
    cfg = m._post_reconcile_cfg(cycle_secs=30.0, role_defs={})
    assert cfg["linkdown_evict_secs"] == -1.0
    assert cfg["replace_debounce_secs"] == 60.0


# --- registry-intersection arming ---
#
# run_cycle (the materializer) only plans/applies for owners the ACTIVE
# registry holds. Arming a role the registry does not hold would therefore
# enforce a role run_cycle can never actually apply for -- a silent no-op
# arm. _effective_enforced_roles is the startup intersection guard (see its
# docstring in flax_classify/__main__.py for the post-3b ops-semantics note:
# this no longer gates a legacy writer, only run_cycle's apply layer).

def test_effective_enforced_roles_strips_unregistered_role_and_logs(caplog):
    m = importlib.import_module("flax_classify.__main__")
    with caplog.at_level("ERROR"):
        effective = m._effective_enforced_roles(frozenset({"post"}), {})
    assert effective == frozenset()
    assert any("post" in rec.message and "not in the active registry"
               in rec.message for rec in caplog.records)


def test_effective_enforced_roles_keeps_registered_strips_rest(caplog):
    m = importlib.import_module("flax_classify.__main__")
    role_defs = {"triage": {"kind": "triage"}}
    with caplog.at_level("ERROR"):
        effective = m._effective_enforced_roles(
            frozenset({"post", "triage"}), role_defs)
    assert effective == frozenset({"triage"})
    errors = [rec.message for rec in caplog.records
              if "not in the active registry" in rec.message]
    assert len(errors) == 1
    assert "post" in errors[0] and "triage" not in errors[0].split(":")[1]


def test_effective_enforced_roles_all_registered_no_error(caplog):
    m = importlib.import_module("flax_classify.__main__")
    role_defs = {"triage": {}, "post": {}}
    with caplog.at_level("ERROR"):
        effective = m._effective_enforced_roles(
            frozenset({"post", "triage"}), role_defs)
    assert effective == frozenset({"post", "triage"})
    assert not [rec for rec in caplog.records if rec.levelname == "ERROR"]


def test_effective_enforced_roles_empty_parse_is_noop(caplog):
    """Default shadow (empty parse) intersects to empty with no logging --
    the guard is dormant when nothing is armed."""
    m = importlib.import_module("flax_classify.__main__")
    with caplog.at_level("ERROR"):
        effective = m._effective_enforced_roles(frozenset(), {"triage": {}})
    assert effective == frozenset()
    assert not caplog.records


def test_run_post_lane_writes_post_dns(monkeypatch):
    import flax_classify.__main__ as m
    # Neutralize the reservation half of the lane.
    monkeypatch.setattr(m, "read_post_order", lambda pool: "FLAX-001")
    monkeypatch.setattr(m, "read_post_racks", lambda path: [])
    monkeypatch.setattr(m, "read_switch_facts", lambda pool: {})
    monkeypatch.setattr(m, "read_observe_rows", lambda pool: [])
    monkeypatch.setattr(m, "observed_by_port", lambda rows: {})
    monkeypatch.setattr(m, "run_post_reservations",
                        lambda pool, **kw: {"written": 0, "purged": 0})
    monkeypatch.setattr(m, "reconcile_post_reservations",
                        lambda pool, **kw: {"deleted": 0, "timers": 0})
    monkeypatch.setattr(m, "db_now", lambda pool: None)
    monkeypatch.setattr(m, "_post_reconcile_cfg", lambda *a, **k: {})
    # DNS half under test.
    monkeypatch.setattr(m, "read_kea_actuals", lambda pool: [
        {"source": "post", "hostname": "fl001-et10b1-bmc",
         "ipv4": "172.25.10.101", "ipv6": "fd00:25::10:101"},
        {"source": "triage", "hostname": "x", "ipv4": "1.1.1.1", "ipv6": None},
    ])
    calls = []
    monkeypatch.setattr(m, "write_hosts_file",
                        lambda path, records: calls.append((path, records)))

    m.run_post_lane(pool=object(), geometry_path="/x", cycle_secs=30,
                    post_dns_path="/tmp/flax-post-devices")

    assert len(calls) == 1
    path, records = calls[0]
    assert path == "/tmp/flax-post-devices"
    assert records == [{"hostname": "fl001-et10b1-bmc",
                        "ipv4": "172.25.10.101", "ipv6": "fd00:25::10:101"}]


def test_run_post_lane_dns_failure_is_isolated(monkeypatch):
    import flax_classify.__main__ as m
    monkeypatch.setattr(m, "read_post_order", lambda pool: "FLAX-001")
    monkeypatch.setattr(m, "read_post_racks", lambda path: [])
    monkeypatch.setattr(m, "read_switch_facts", lambda pool: {})
    monkeypatch.setattr(m, "read_observe_rows", lambda pool: [])
    monkeypatch.setattr(m, "observed_by_port", lambda rows: {})
    monkeypatch.setattr(m, "run_post_reservations",
                        lambda pool, **kw: {"written": 0, "purged": 0})
    monkeypatch.setattr(m, "reconcile_post_reservations",
                        lambda pool, **kw: {"deleted": 0, "timers": 0})
    monkeypatch.setattr(m, "db_now", lambda pool: None)
    monkeypatch.setattr(m, "_post_reconcile_cfg", lambda *a, **k: {})
    monkeypatch.setattr(m, "read_kea_actuals", lambda pool: [])
    def _boom(path, records):
        raise OSError("read-only fs")
    monkeypatch.setattr(m, "write_hosts_file", _boom)
    # Must NOT raise.
    m.run_post_lane(pool=object(), geometry_path="/x", cycle_secs=30,
                    post_dns_path="/tmp/flax-post-devices")
