"""role_registry: load/validate/resolve + parity with legacy phase_for."""
import json
import pytest

from flax_classify.role_registry import (
    RegistryError, load_role_dir, resolve_role, validate_roles)
from flax_classify.vlan_policy import load_phase_geometry, phase_for

# NOTE: load_phase_geometry (flax_classify/vlan_policy.py) expects the same
# flat-list-of-{"port": ...} shape as the real geometry.json files
# (files/configs/eindhoven/services/geometry.json) -- NOT the nested
# {"racks": [{"nodes": [...]}]} shape used by the unrelated flax_post.geometry
# module. Using the flat shape here so this fixture actually exercises the
# real loader (a nested dict would raise TypeError: string indices must be
# integers when load_phase_geometry iterates it).
GEOM = [{"port": "Ethernet6/1", "switch": "rabbit-gouda"},
        {"port": "Ethernet7/1", "switch": "rabbit-gouda"}]


def _write_roles(tmp_path, geometry_path):
    d = tmp_path / "roles.d"
    d.mkdir()
    (d / "triage.json").write_text(json.dumps({
        "role": "triage", "universe": {"ports_from": str(geometry_path)},
        "capabilities": {"reconcile_switch": True},
        "policy": {"vid": {"default": 16}}, "record_keys": ["mac", "serial"]}))
    (d / "post.json").write_text(json.dumps({
        "role": "post",
        "universe": {"switches": ["rabbit-edam"], "switch_prefixes": ["turtle"],
                     "catch_all": True},
        # Mirrors the LIVE /etc/flax/roles.d/post.json capability set (verified
        # on bang-gouda 2026-09-21). `observe` is the one flax_observe reads --
        # role_caps.py enrols the switches of any role whose
        # capabilities.observe is truthy, which is why rabbit-edam is observed
        # at all. The fixture previously declared only reconcile_switch, so
        # test_post_profile_declares_observe_capability asserted a key its own
        # fixture never wrote.
        "capabilities": {"reconcile_switch": False, "dhcp_reservations": True,
                         "firmware": True, "observe": True},
        "policy": {"vid": {"default": 24}}, "record_keys": ["customer", "order"]}))
    return d


def _write_integra(d, switch="rabbit-gouda"):
    """Drop the phase-6 integra role into an existing roles.d dir.

    A pure ``ports``-claim role: one explicit (switch, port) claim
    (default rabbit-gouda et32/1), all capabilities off, vid 31. et32/1 is NOT
    in the triage geometry fixture, so it neither collides on validate nor
    changes any other port's resolution -- it flips exactly one catch_all port
    to integra. ``switch`` is parameterised so the DB-backed test can publish a
    SCRATCH switch name (never a real one -- see
    test_no_real_switch_names_in_db_fixtures for the live-wipe rationale).
    """
    (d / "integra.json").write_text(json.dumps({
        "role": "integra",
        "universe": {"ports": {switch: ["et32/1"]}},
        "capabilities": {"reconcile_switch": False, "dhcp_reservations": False,
                         "firmware": False},
        "policy": {"vid": {"default": 31}},
        "record_keys": ["customer", "order"]}))
    return d


@pytest.fixture
def roles_dir(tmp_path):
    geom = tmp_path / "geometry.json"
    geom.write_text(json.dumps(GEOM))
    return _write_roles(tmp_path, geom), geom


def test_load_missing_dir_returns_empty(tmp_path):
    assert load_role_dir(str(tmp_path / "absent")) == {}


def test_load_malformed_raises(tmp_path):
    d = tmp_path / "roles.d"; d.mkdir()
    (d / "bad.json").write_text("{nope")
    with pytest.raises(RegistryError):
        load_role_dir(str(d))


def test_load_non_dict_top_level_raises(tmp_path):
    # valid JSON but not an object (e.g. a bare list) must raise
    # RegistryError, not AttributeError, when .get() is attempted on it.
    d = tmp_path / "roles.d"; d.mkdir()
    (d / "triage.json").write_text(json.dumps(["not", "a", "dict"]))
    with pytest.raises(RegistryError):
        load_role_dir(str(d))


def test_role_key_must_match_filename_stem(tmp_path):
    d = tmp_path / "roles.d"; d.mkdir()
    (d / "triage.json").write_text(json.dumps({"role": "post", "universe": {}}))
    with pytest.raises(RegistryError):
        load_role_dir(str(d))


def test_validate_rejects_switch_claimed_twice(roles_dir):
    d, _ = roles_dir
    defs = load_role_dir(str(d))
    defs["triage"]["universe"]["switches"] = ["rabbit-edam"]
    with pytest.raises(RegistryError):
        validate_roles(defs)


def test_validate_rejects_duplicate_catch_all(roles_dir):
    d, _ = roles_dir
    defs = load_role_dir(str(d))
    defs["triage"]["universe"]["catch_all"] = True
    with pytest.raises(RegistryError):
        validate_roles(defs)


def test_resolve_precedence_and_parity(roles_dir):
    d, geom_path = roles_dir
    defs = load_role_dir(str(d))
    validate_roles(defs)
    geom_tokens = load_phase_geometry(str(geom_path))
    cases = [("rabbit-gouda", "Ethernet6/1"),   # geometry -> triage
             ("rabbit-gouda", "Ethernet9/1"),   # not in geometry -> post (catch_all)
             ("rabbit-edam", "Ethernet4/1"),    # post switch claim
             ("turtle-3", "swp7"),              # prefix claim
             ("hare-1", "Ethernet2/1")]         # unknown switch -> post via catch_all
    for sw, port in cases:
        assert resolve_role(defs, sw, port) == phase_for(geom_tokens, sw, port), (sw, port)


def test_resolve_none_without_catch_all(roles_dir):
    d, _ = roles_dir
    defs = load_role_dir(str(d))
    defs["post"]["universe"]["catch_all"] = False
    assert resolve_role(defs, "hare-1", "Ethernet2/1") is None


# --- phase 6: integra, a pure ports-claim role (no product-code change) -----

def test_integra_port_claim_wins(roles_dir):
    # The explicit (rabbit-gouda, et32/1) port claim is the most specific tier
    # (port > switch > prefix > catch_all), so it wins over post's catch_all.
    d, _ = roles_dir
    _write_integra(d)
    defs = load_role_dir(str(d))
    validate_roles(defs)
    assert resolve_role(defs, "rabbit-gouda", "et32/1") == "integra"


def test_integra_validate_accepts_three_role_set(roles_dir):
    # et32/1 is absent from the triage geometry, so the port claim collides with
    # nothing: validate_roles must accept the full triage+post+integra set.
    d, _ = roles_dir
    _write_integra(d)
    defs = load_role_dir(str(d))
    validate_roles(defs)  # must not raise
    assert set(defs) == {"triage", "post", "integra"}


def test_integra_only_changes_et32_1_parity(roles_dir):
    # No-regression: adding integra flips exactly one port (rabbit-gouda et32/1,
    # post's catch_all -> integra) and leaves every other port's resolution
    # byte-for-byte identical to the 2-role (triage+post) set.
    d, _ = roles_dir
    two_role = load_role_dir(str(d))
    validate_roles(two_role)
    _write_integra(d)
    three_role = load_role_dir(str(d))
    validate_roles(three_role)

    unchanged = [
        ("rabbit-gouda", "Ethernet6/1"),   # geometry -> triage
        ("rabbit-gouda", "Ethernet7/1"),   # geometry -> triage
        ("rabbit-gouda", "Ethernet9/1"),   # not geometry -> post via catch_all
        ("rabbit-edam", "Ethernet4/1"),    # post switch claim
        ("turtle-3", "swp7"),              # post prefix claim
        ("hare-1", "Ethernet2/1"),         # unknown switch -> post via catch_all
    ]
    for sw, port in unchanged:
        assert resolve_role(three_role, sw, port) == resolve_role(two_role, sw, port), (sw, port)

    # the single deliberate delta:
    assert resolve_role(two_role, "rabbit-gouda", "et32/1") == "post"
    assert resolve_role(three_role, "rabbit-gouda", "et32/1") == "integra"


def test_integra_normaliser_token_forms_resolve(roles_dir):
    # Any spelling of the claimed port canonicalises to et32/1 via
    # _norm_rabbit_token and resolves to integra.
    d, _ = roles_dir
    _write_integra(d)
    defs = load_role_dir(str(d))
    validate_roles(defs)
    for tok in ("et32/1", "Ethernet32/1", "et32b1"):
        assert resolve_role(defs, "rabbit-gouda", tok) == "integra", tok


def test_geometry_claim_is_switch_scoped(roles_dir):
    """A triage geometry token belongs to triage ONLY on the switch
    geometry.json records it under (rabbit-gouda). The SAME port number on a
    post switch (rabbit-edam) must NOT be hijacked to triage -- it resolves to
    post's switch claim. Regression: the loader filed geometry under a
    switch-agnostic '*geometry*' bucket, so rabbit-edam's et10/et6 post blades
    resolved to triage -> steered to the triage vid (17) instead of post (25)."""
    d, _ = roles_dir  # GEOM = Ethernet6/1, Ethernet7/1 on rabbit-gouda
    defs = load_role_dir(str(d))
    validate_roles(defs)
    # On its own switch: triage (geometry claim honoured).
    assert resolve_role(defs, "rabbit-gouda", "Ethernet6/1") == "triage"
    # Cross-switch: NOT hijacked -> post owns rabbit-edam.
    assert resolve_role(defs, "rabbit-edam", "Ethernet6/1") == "post"
    assert resolve_role(defs, "rabbit-edam", "Ethernet7/1") == "post"


def test_phase_for_is_switch_scoped():
    """The legacy oracle must be switch-scoped too, else the parity gate
    (registry vs phase_for) trips on the cross-switch fix."""
    from flax_classify.vlan_policy import phase_for
    geom = {"rabbit-gouda": {"et6/1"}}
    assert phase_for(geom, "rabbit-gouda", "Ethernet6/1") == "triage"
    assert phase_for(geom, "rabbit-edam", "Ethernet6/1") == "post"
    assert phase_for(geom, "turtle-gouda", "swp4") == "post"
