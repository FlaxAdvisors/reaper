# tests/test_flax_classify_main_args.py
"""Smoke-test: flax_classify.__main__ arg parser recognises the three new
steering-policy flags and calls the right loaders with the right paths."""
import argparse
import json
import importlib
import logging
import pathlib
import sys
import unittest.mock as mock

import pytest

from flax_classify import role_registry

REPO = pathlib.Path(__file__).resolve().parents[1]


def _parse_argv(argv):
    """Run only the argparse step (not main()) so no DB/file I/O is needed."""
    import flax_classify.__main__ as m
    p = argparse.ArgumentParser(prog="flax-classify")
    # Replicate the same parser construction as main() so we can exercise it
    # without triggering the env-var / DB / file-load paths.
    p.add_argument("--cycle-secs", type=float, default=30.0)
    p.add_argument("--debounce-secs", type=float, default=1.5)
    p.add_argument("--healthz-port", type=int, default=10991)
    p.add_argument("--healthz-stale-secs", type=float, default=60.0)
    p.add_argument("--vlans", default="/etc/flax/vlans.json")
    p.add_argument("--geometry", default="/etc/flax/geometry.json")
    p.add_argument("--no-steer", dest="no_steer",
                   default="/etc/flax/no-steer-ports.json")
    p.add_argument("--roles-dir", default=role_registry.DEFAULT_ROLES_DIR)
    return p.parse_args(argv)


def test_arg_defaults():
    args = _parse_argv([])
    assert args.vlans == "/etc/flax/vlans.json"
    assert args.geometry == "/etc/flax/geometry.json"
    assert args.no_steer == "/etc/flax/no-steer-ports.json"
    assert args.roles_dir == "/etc/flax/roles.d"


def test_arg_overrides():
    args = _parse_argv(["--vlans", "/tmp/v.json",
                        "--geometry", "/tmp/g.json",
                        "--no-steer", "/tmp/ns.json",
                        "--roles-dir", "/tmp/roles.d"])
    assert args.vlans == "/tmp/v.json"
    assert args.geometry == "/tmp/g.json"
    assert args.no_steer == "/tmp/ns.json"
    assert args.roles_dir == "/tmp/roles.d"


def test_loaders_called_with_paths_from_args(tmp_path, monkeypatch):
    """main() calls load_fp_to_vid/load_phase_geometry/load_no_steer with the
    paths from args before any cycle runs."""
    vlans_file = tmp_path / "vlans.json"
    geom_file = tmp_path / "geometry.json"
    ns_file = tmp_path / "no-steer-ports.json"
    vlans_file.write_text(json.dumps([
        {"family": "tioga-pass", "phase": "triage", "vid": 16}
    ]))
    geom_file.write_text(json.dumps([{"port": "et6b1"}]))
    ns_file.write_text(json.dumps([]))

    called = {}

    # Patch heavy imports so main() never blocks on DB / healthz / listen.
    import flax_classify.__main__ as m

    # __main__ binds the loader names directly at import time via
    # "from .vlan_policy import ..."; patch the names IN __main__ so the
    # call in main() hits our fakes.
    monkeypatch.setattr(m, "load_fp_to_vid",
                        lambda path: called.update(vlans=path) or {})
    monkeypatch.setattr(m, "load_phase_geometry",
                        lambda path: called.update(geom=path) or set())
    monkeypatch.setattr(m, "load_no_steer",
                        lambda path: called.update(ns=path) or set())
    monkeypatch.setattr(m, "_build_conninfo", lambda: "host=127.0.0.1")
    monkeypatch.setattr(m, "build_pool", lambda ci: object())
    monkeypatch.setattr(m, "HealthState", lambda **kw: mock.MagicMock())
    monkeypatch.setattr(m, "Debouncer",
                        lambda **kw: mock.MagicMock())
    monkeypatch.setattr(m.threading, "Thread",
                        lambda target=None, args=(), daemon=False: mock.MagicMock())
    # Make the infinite loop exit immediately after the first iteration.
    import itertools
    call_count = itertools.count()
    original_sleep = m.time.sleep

    def _one_shot_sleep(secs):
        if next(call_count) == 0:
            raise SystemExit(0)

    monkeypatch.setattr(m.time, "sleep", _one_shot_sleep)
    monkeypatch.setattr(m, "run_one_cycle",
                        lambda pool, **kw: {"written": 0, "deleted": 0,
                                            "skipped": 0, "written_desired": 0})

    import pytest
    with pytest.raises(SystemExit):
        m.main(["--vlans", str(vlans_file),
                "--geometry", str(geom_file),
                "--no-steer", str(ns_file),
                # Point at a dir that doesn't exist so this test's behaviour
                # doesn't depend on whatever /etc/flax/roles.d happens to
                # hold on the host running the suite.
                "--roles-dir", str(tmp_path / "roles.d")])

    assert called["vlans"] == str(vlans_file)
    assert called["geom"] == str(geom_file)
    assert called["ns"] == str(ns_file)


def _patch_common(m, monkeypatch):
    """Shared monkeypatching for full main() smoke tests: no DB, no real
    threads/healthz/listen, single pass through the periodic loop."""
    monkeypatch.setattr(m, "_build_conninfo", lambda: "host=127.0.0.1")
    monkeypatch.setattr(m, "build_pool", lambda ci: object())
    monkeypatch.setattr(m, "HealthState", lambda **kw: mock.MagicMock())
    monkeypatch.setattr(m, "Debouncer", lambda **kw: mock.MagicMock())
    monkeypatch.setattr(m.threading, "Thread",
                        lambda target=None, args=(), daemon=False: mock.MagicMock())

    def _one_shot_sleep(secs):
        raise SystemExit(0)
    monkeypatch.setattr(m.time, "sleep", _one_shot_sleep)

    captured = {}

    def _fake_run_one_cycle(pool, **kw):
        captured.update(kw)
        return {"written": 0, "deleted": 0, "skipped": 0, "written_desired": 0}
    monkeypatch.setattr(m, "run_one_cycle", _fake_run_one_cycle)
    return captured


def _policy_files(tmp_path):
    vlans_file = tmp_path / "vlans.json"
    geom_file = tmp_path / "geometry.json"
    ns_file = tmp_path / "no-steer-ports.json"
    vlans_file.write_text(json.dumps([]))
    geom_file.write_text(json.dumps([]))
    ns_file.write_text(json.dumps([]))
    return vlans_file, geom_file, ns_file


def test_missing_roles_dir_falls_back_no_publish_no_ack(tmp_path, monkeypatch, caplog):
    """Missing roles.d -> resolve stays None, publish_roles is NEVER called
    (finding #1: publish_roles({}) would wipe the tables), a warning is
    logged, and the cycle runs with resolve=None (legacy phase_for path)."""
    import flax_classify.__main__ as m

    vlans_file, geom_file, ns_file = _policy_files(tmp_path)
    missing_roles_dir = tmp_path / "roles.d"  # never created

    captured = _patch_common(m, monkeypatch)

    publish_calls = []
    monkeypatch.setattr(m.role_registry, "publish_roles",
                        lambda pool, defs: publish_calls.append(defs) or 1)
    ack_calls = []
    monkeypatch.setattr(m, "write_ack",
                        lambda pool, consumer, source, gen, action, detail=None:
                            ack_calls.append((consumer, source, gen, action)))

    with caplog.at_level(logging.INFO):
        with pytest.raises(SystemExit):
            m.main(["--vlans", str(vlans_file), "--geometry", str(geom_file),
                    "--no-steer", str(ns_file),
                    "--roles-dir", str(missing_roles_dir)])

    assert publish_calls == []
    assert not [c for c in ack_calls if c[1] == "roles"]
    assert captured.get("resolve") is None
    assert any("roles.d missing/empty" in r.message for r in caplog.records)


def test_invalid_roles_dir_falls_back_no_publish(tmp_path, monkeypatch, caplog):
    """Malformed roles.d -> RegistryError caught, publish_roles NEVER called
    (finding #2: never publish unvalidated defs), fallback to resolve=None."""
    import flax_classify.__main__ as m

    vlans_file, geom_file, ns_file = _policy_files(tmp_path)
    roles_dir = tmp_path / "roles.d"
    roles_dir.mkdir()
    (roles_dir / "bad.json").write_text("{not valid json")

    captured = _patch_common(m, monkeypatch)

    publish_calls = []
    monkeypatch.setattr(m.role_registry, "publish_roles",
                        lambda pool, defs: publish_calls.append(defs) or 1)
    ack_calls = []
    monkeypatch.setattr(m, "write_ack",
                        lambda pool, consumer, source, gen, action, detail=None:
                            ack_calls.append((consumer, source, gen, action)))

    with caplog.at_level(logging.INFO):
        with pytest.raises(SystemExit):
            m.main(["--vlans", str(vlans_file), "--geometry", str(geom_file),
                    "--no-steer", str(ns_file),
                    "--roles-dir", str(roles_dir)])

    assert publish_calls == []
    assert not [c for c in ack_calls if c[1] == "roles"]
    assert captured.get("resolve") is None
    assert any("role registry invalid" in r.message for r in caplog.records)


def test_publish_roles_db_error_falls_back_no_crash(tmp_path, monkeypatch, caplog):
    """A valid roles.d that fails at publish time (e.g. a DB/psycopg error
    because migration 029 hasn't been applied yet) must not crash classify:
    the generic except falls back to resolve=None, same as an invalid
    registry, and no 'roles' ack is written."""
    import flax_classify.__main__ as m

    vlans_file, geom_file, ns_file = _policy_files(tmp_path)
    roles_dir = tmp_path / "roles.d"
    roles_dir.mkdir()
    (roles_dir / "post.json").write_text(json.dumps({
        "role": "post", "universe": {"catch_all": True}}))

    captured = _patch_common(m, monkeypatch)

    def _boom(pool, defs):
        raise RuntimeError("relation \"roles\" does not exist")
    monkeypatch.setattr(m.role_registry, "publish_roles", _boom)
    ack_calls = []
    monkeypatch.setattr(m, "write_ack",
                        lambda pool, consumer, source, gen, action, detail=None:
                            ack_calls.append((consumer, source, gen, action)))

    with caplog.at_level(logging.INFO):
        with pytest.raises(SystemExit):
            m.main(["--vlans", str(vlans_file), "--geometry", str(geom_file),
                    "--no-steer", str(ns_file),
                    "--roles-dir", str(roles_dir)])

    assert not [c for c in ack_calls if c[1] == "roles"]
    assert captured.get("resolve") is None
    assert any("role registry startup publish failed" in r.message
               for r in caplog.records)


def test_valid_roles_dir_publishes_and_acks(tmp_path, monkeypatch, caplog):
    """A valid roles.d loads, validates, publishes exactly once, writes the
    'roles' ack with the returned generation, and builds a resolve callable
    (ack written only on successful publish)."""
    import flax_classify.__main__ as m

    vlans_file, geom_file, ns_file = _policy_files(tmp_path)
    roles_dir = tmp_path / "roles.d"
    roles_dir.mkdir()
    (roles_dir / "post.json").write_text(json.dumps({
        "role": "post", "universe": {"catch_all": True}}))

    captured = _patch_common(m, monkeypatch)

    publish_calls = []
    monkeypatch.setattr(m.role_registry, "publish_roles",
                        lambda pool, defs: publish_calls.append(defs) or 7)
    ack_calls = []
    monkeypatch.setattr(m, "write_ack",
                        lambda pool, consumer, source, gen, action, detail=None:
                            ack_calls.append((consumer, source, gen, action)))

    with caplog.at_level(logging.INFO):
        with pytest.raises(SystemExit):
            m.main(["--vlans", str(vlans_file), "--geometry", str(geom_file),
                    "--no-steer", str(ns_file),
                    "--roles-dir", str(roles_dir)])

    assert len(publish_calls) == 1
    assert ("flax-classify", "roles", 7, "applied") in ack_calls
    assert captured.get("resolve") is not None
    assert callable(captured["resolve"])


def test_roles_ack_action_satisfies_consumer_acks_check():
    """The roles-ack action literal must be one migration 003 allows.

    'publish' violated consumer_acks_action_check on the live DB
    (2026-07-03); pin the literal to the constraint's allowed set so a
    rename cannot silently re-introduce the violation.
    """
    import re

    mig = (REPO / "schema/versions/003_state_and_action_tables.py").read_text()
    allowed = set(re.search(r"action\s+TEXT NOT NULL CHECK \(action IN \(([^)]+)\)\)",
                            mig).group(1).replace("'", "").replace(" ", "").split(","))
    src = (REPO / "flax_classify/__main__.py").read_text()
    roles_ack = re.search(r'write_ack\(pool, "flax-classify", "roles", gen, "(\w+)"\)', src)
    assert roles_ack, "roles ack call not found"
    assert roles_ack.group(1) in allowed, (roles_ack.group(1), allowed)


# --- vid policy sourced from the role registry (Task 4) ---
#
# fp_to_vid = fp_to_vid_from_roles(role_defs) or load_fp_to_vid(args.vlans),
# gated the same way _effective_enforced_roles is (role_defs if resolve is
# not None else {}) -- an invalid/unpublished registry must degrade the vid
# steering policy exactly like it degrades role resolution, not partially
# trust a registry whose universe/validation failed.

def _write_vlans_fallback_marker(tmp_path):
    """A vlans.json whose content is clearly NOT any registry by_family map
    used below, so a captured fp_to_vid equal to this proves the fallback
    path (not the registry) supplied the value."""
    vlans_file = tmp_path / "vlans.json"
    vlans_file.write_text(json.dumps([
        {"family": "widget", "phase": "triage", "vid": 99}]))
    return vlans_file, {("widget", "triage"): 99}


def test_vid_policy_sourced_from_registry_when_by_family_present(tmp_path, monkeypatch, caplog):
    import flax_classify.__main__ as m

    _, geom_file, ns_file = _policy_files(tmp_path)
    vlans_file, fallback_map = _write_vlans_fallback_marker(tmp_path)
    roles_dir = tmp_path / "roles.d"
    roles_dir.mkdir()
    (roles_dir / "post.json").write_text(json.dumps({
        "role": "post", "universe": {"catch_all": True},
        "policy": {"vid": {"default": 24, "by_family": {"leopard": 24}}}}))

    captured = _patch_common(m, monkeypatch)
    monkeypatch.setattr(m.role_registry, "publish_roles", lambda pool, defs: 1)
    monkeypatch.setattr(m, "write_ack", lambda *a, **kw: None)

    with caplog.at_level(logging.INFO):
        with pytest.raises(SystemExit):
            m.main(["--vlans", str(vlans_file), "--geometry", str(geom_file),
                    "--no-steer", str(ns_file), "--roles-dir", str(roles_dir)])

    assert captured["fp_to_vid"] == {("leopard", "post"): 24}
    assert captured["fp_to_vid"] != fallback_map
    assert any("vid policy: registry" in r.message for r in caplog.records)


def test_vid_policy_falls_back_when_registry_predates_policy(tmp_path, monkeypatch, caplog):
    """Valid, publishable registry but no role carries by_family (an older
    registry generation) -> fp_to_vid_from_roles returns None -> falls back
    to load_fp_to_vid(args.vlans)."""
    import flax_classify.__main__ as m

    _, geom_file, ns_file = _policy_files(tmp_path)
    vlans_file, fallback_map = _write_vlans_fallback_marker(tmp_path)
    roles_dir = tmp_path / "roles.d"
    roles_dir.mkdir()
    (roles_dir / "post.json").write_text(json.dumps({
        "role": "post", "universe": {"catch_all": True},
        "policy": {"vid": {"default": 24}}}))

    captured = _patch_common(m, monkeypatch)
    monkeypatch.setattr(m.role_registry, "publish_roles", lambda pool, defs: 1)
    monkeypatch.setattr(m, "write_ack", lambda *a, **kw: None)

    with caplog.at_level(logging.INFO):
        with pytest.raises(SystemExit):
            m.main(["--vlans", str(vlans_file), "--geometry", str(geom_file),
                    "--no-steer", str(ns_file), "--roles-dir", str(roles_dir)])

    assert captured["fp_to_vid"] == fallback_map
    assert any("vid policy: vlans.json fallback" in r.message for r in caplog.records)


def test_vid_policy_falls_back_when_registry_fails_validation(tmp_path, monkeypatch, caplog):
    """load_role_dir succeeds (role_defs gets populated, WITH a by_family
    block) but validate_roles subsequently raises (two roles claim the same
    switch) -> resolve stays None. The vid policy must NOT trust the
    populated-but-unvalidated role_defs -- same gate as
    _effective_enforced_roles -- so fp_to_vid still falls back to
    load_fp_to_vid(args.vlans), not the registry's by_family."""
    import flax_classify.__main__ as m

    _, geom_file, ns_file = _policy_files(tmp_path)
    vlans_file, fallback_map = _write_vlans_fallback_marker(tmp_path)
    roles_dir = tmp_path / "roles.d"
    roles_dir.mkdir()
    (roles_dir / "triage.json").write_text(json.dumps({
        "role": "triage", "universe": {"switches": ["rabbit-gouda"]},
        "policy": {"vid": {"default": 16, "by_family": {"leopard": 16}}}}))
    (roles_dir / "post.json").write_text(json.dumps({
        "role": "post", "universe": {"switches": ["rabbit-gouda"]},  # conflict
        "policy": {"vid": {"default": 24, "by_family": {"leopard": 24}}}}))

    captured = _patch_common(m, monkeypatch)
    monkeypatch.setattr(m.role_registry, "publish_roles", lambda pool, defs: 1)
    monkeypatch.setattr(m, "write_ack", lambda *a, **kw: None)

    with caplog.at_level(logging.INFO):
        with pytest.raises(SystemExit):
            m.main(["--vlans", str(vlans_file), "--geometry", str(geom_file),
                    "--no-steer", str(ns_file), "--roles-dir", str(roles_dir)])

    assert captured.get("resolve") is None
    assert captured["fp_to_vid"] == fallback_map
    assert any("vid policy: vlans.json fallback" in r.message for r in caplog.records)
