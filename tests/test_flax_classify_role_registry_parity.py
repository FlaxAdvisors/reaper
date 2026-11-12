"""role_registry_parity: compare() core + CLI (phase-1 deploy gate artifact).

Reuses Task 2's fixture builder (tests/test_flax_classify_role_registry.py):
a flat-list-of-{"port","switch"} geometry fixture (matches the real
geometry.json shape) plus the triage/post roles.d pair (triage sourced from
geometry via ports_from; post = switch claim (rabbit-edam) + prefix claim
(turtle*) + catch_all).
"""
import json

import pytest

from flax_classify.role_registry import load_role_dir, validate_roles
from flax_classify.role_registry_parity import compare, main
from flax_classify.vlan_policy import load_phase_geometry

from test_flax_classify_role_registry import (  # noqa: F401 (fixture)
    roles_dir, _write_integra)

# switch_facts shape: {switch: {"ports": {port: {...}}}}
# Only mask=="access" ports are in any role's lens (operator directive
# 2026-07-04): non-access ports (uplinks/trunks/Cpu) are skipped entirely.
FACTS = {
    "rabbit-gouda": {"ports": {
        "Ethernet6/1": {"mask": "access"},   # in GEOM -> triage (both)
        "Ethernet9/1": {"mask": "access"},   # not in GEOM -> post via catch_all (both)
        "Ethernet1/1": {"mask": "trunk"},    # uplink -> skipped, never reported
        "Cpu": {},                           # no mask -> skipped
    }},
    "rabbit-edam": {"ports": {
        "Ethernet4/1": {"mask": "access"},   # post via switch claim
    }},
    "turtle-3": {"ports": {
        "swp7": {"mask": "access"},          # post via prefix claim
    }},
}


def _loaded(d, geom_path):
    defs = load_role_dir(str(d))
    validate_roles(defs)
    return defs, load_phase_geometry(str(geom_path))


def test_compare_zero_mismatches_and_would_be_unassigned(roles_dir):
    d, geom_path = roles_dir
    defs, geom_tokens = _loaded(d, geom_path)

    result = compare(FACTS, geom_tokens, defs)

    assert result["total"] == 4
    assert result["skipped_non_access"] == 2   # trunk uplink + maskless Cpu
    assert result["mismatches"] == []
    # non-access ports never appear in would-be-unassigned either
    assert set(result["would_be_unassigned"]) == {("rabbit-gouda", "Ethernet9/1")}


def test_compare_geometry_port_not_would_be_unassigned(roles_dir):
    d, geom_path = roles_dir
    defs, geom_tokens = _loaded(d, geom_path)

    result = compare(FACTS, geom_tokens, defs)

    assert ("rabbit-gouda", "Ethernet6/1") not in result["would_be_unassigned"]


def test_compare_switch_claim_not_would_be_unassigned(roles_dir):
    d, geom_path = roles_dir
    defs, geom_tokens = _loaded(d, geom_path)

    result = compare(FACTS, geom_tokens, defs)

    assert ("rabbit-edam", "Ethernet4/1") not in result["would_be_unassigned"]


def test_compare_prefix_claim_not_would_be_unassigned(roles_dir):
    d, geom_path = roles_dir
    defs, geom_tokens = _loaded(d, geom_path)

    result = compare(FACTS, geom_tokens, defs)

    assert ("turtle-3", "swp7") not in result["would_be_unassigned"]


def test_compare_broken_registry_yields_mismatches(roles_dir):
    d, geom_path = roles_dir
    defs, geom_tokens = _loaded(d, geom_path)
    defs["post"]["universe"]["catch_all"] = False  # break the phase-1 registry

    result = compare(FACTS, geom_tokens, defs)

    assert result["mismatches"] != []
    assert any(sw == "rabbit-gouda" and port == "Ethernet9/1"
               and legacy == "post" and new is None
               for sw, port, legacy, new in result["mismatches"])


def test_compare_with_integra_added_no_regression(roles_dir):
    # Phase 6: adding the integra ports-claim role must not perturb legacy
    # parity for any existing FACTS port (none of which is et32/1). compare()
    # against legacy phase_for stays clean -- integra only owns et32/1.
    d, geom_path = roles_dir
    _write_integra(d)
    defs, geom_tokens = _loaded(d, geom_path)

    result = compare(FACTS, geom_tokens, defs)

    assert result["total"] == 4
    assert result["skipped_non_access"] == 2
    assert result["mismatches"] == []
    assert set(result["would_be_unassigned"]) == {("rabbit-gouda", "Ethernet9/1")}


def test_cli_exit_zero_on_parity(tmp_path, roles_dir, capsys):
    d, geom_path = roles_dir
    facts_path = tmp_path / "facts.json"
    facts_path.write_text(json.dumps(FACTS))

    rc = main(["--roles-dir", str(d), "--geometry", str(geom_path),
               "--facts-json", str(facts_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "PARITY OK" in out
    assert "total access ports compared: 4 (skipped non-access: 2)" in out
    assert "rabbit-gouda Ethernet9/1" in out  # would-be-unassigned listing


def test_cli_exit_one_on_mismatch(tmp_path, roles_dir, capsys):
    d, geom_path = roles_dir
    post_path = d / "post.json"
    data = json.loads(post_path.read_text())
    data["universe"]["catch_all"] = False
    post_path.write_text(json.dumps(data))

    facts_path = tmp_path / "facts.json"
    facts_path.write_text(json.dumps(FACTS))

    rc = main(["--roles-dir", str(d), "--geometry", str(geom_path),
               "--facts-json", str(facts_path)])

    assert rc == 1
    out = capsys.readouterr().out
    assert "MISMATCHES" in out
    assert "PARITY OK" not in out
