"""Tests for flax_control.roles_view — pure view functions for /roles.

No DB, no FastAPI client: build_registry()/coverage() are fed literal row
fixtures shaped exactly like the roles / role_universe table columns
(rows_roles = (role, definition, generation, loaded_at) tuples; rows_universe
= (role, kind, switch, port) tuples), mirroring how test_flax_control_config_view.py
exercises config_view directly.
"""
import datetime

from flax_control import roles_view

TRIAGE_DEF = {
    "role": "triage",
    "universe": {"ports_from": "/etc/flax/geometry.json"},
    "capabilities": {"reconcile_switch": True},
    "policy": {"vid": {"default": 16}},
    "record_keys": ["mac", "serial"],
}
POST_DEF = {
    "role": "post",
    "universe": {"switches": ["rabbit-edam"], "switch_prefixes": ["turtle"],
                 "catch_all": True},
    "capabilities": {"reconcile_switch": False},
    "policy": {"vid": {"default": 24}},
    "record_keys": ["customer", "order"],
}

LOADED_AT = datetime.datetime(2026, 7, 3, 12, 0, 0)

ROWS_ROLES = [
    ("triage", TRIAGE_DEF, 3, LOADED_AT),
    ("post", POST_DEF, 3, LOADED_AT),
]

# role_universe rows: (role, kind, switch, port)
ROWS_UNIVERSE = [
    ("triage", "port", "*geometry*", "et6/1"),
    ("triage", "port", "*geometry*", "et7/1"),
    ("post", "switch", "rabbit-edam", None),
    ("post", "prefix", "turtle", None),
    ("post", "catch_all", None, None),
]


# ---------------------------------------------------------------------------
# build_registry
# ---------------------------------------------------------------------------

def test_build_registry_empty_rows_returns_empty_state():
    out = roles_view.build_registry([], [])
    assert out == {"roles": [], "catch_all_role": None}


def test_build_registry_two_role_fixture_shapes_summary_and_catch_all():
    out = roles_view.build_registry(ROWS_ROLES, ROWS_UNIVERSE)
    assert out["catch_all_role"] == "post"
    names = {r["name"] for r in out["roles"]}
    assert names == {"triage", "post"}

    triage = next(r for r in out["roles"] if r["name"] == "triage")
    assert triage["generation"] == 3
    assert triage["loaded_at"] == LOADED_AT
    assert triage["capabilities"] == {"reconcile_switch": True}
    assert triage["policy"] == {"vid": {"default": 16}}
    assert triage["record_keys"] == ["mac", "serial"]
    assert any("ports: 2 claims" in s for s in triage["universe_summary"])
    assert not any("catch-all" in s for s in triage["universe_summary"])

    post = next(r for r in out["roles"] if r["name"] == "post")
    assert any("rabbit-edam" in s for s in post["universe_summary"])
    assert any("turtle" in s for s in post["universe_summary"])
    assert any("catch-all" in s for s in post["universe_summary"])


def test_build_registry_role_with_no_universe_rows_has_empty_summary():
    rows_roles = [("orphan", {"role": "orphan"}, 1, LOADED_AT)]
    out = roles_view.build_registry(rows_roles, [])
    role = out["roles"][0]
    assert role["universe_summary"] == []
    assert role["capabilities"] == {}
    assert role["policy"] == {}
    assert role["record_keys"] == []


# ---------------------------------------------------------------------------
# build_registry — phase 6: integra, a pure ports-claim role (TEST ONLY,
# no route/template/view code change). integra contributes a role row + a
# single `port` kind universe row for (rabbit-gouda, et32/1). et32/1 is
# absent from triage's geometry, so with only triage+post it would fall to
# post's catch_all; integra's explicit port claim carves it out.
# ---------------------------------------------------------------------------

INTEGRA_DEF = {
    "role": "integra",
    "universe": {"ports": {"rabbit-gouda": ["et32/1"]}},
    "capabilities": {"reconcile_switch": False, "dhcp_reservations": False,
                     "firmware": False},
    "policy": {"vid": {"default": 31}},
    "record_keys": ["customer", "order"],
}

# 3-role registry: same tuple shapes queries.roles()/role_universe() return.
ROWS_ROLES_3 = ROWS_ROLES + [("integra", INTEGRA_DEF, 3, LOADED_AT)]
ROWS_UNIVERSE_3 = ROWS_UNIVERSE + [("integra", "port", "rabbit-gouda", "et32/1")]


def test_build_registry_exposes_integra_role_capabilities_policy_record_keys():
    out = roles_view.build_registry(ROWS_ROLES_3, ROWS_UNIVERSE_3)
    names = {r["name"] for r in out["roles"]}
    assert names == {"triage", "post", "integra"}

    integra = next(r for r in out["roles"] if r["name"] == "integra")
    assert integra["capabilities"] == {
        "reconcile_switch": False, "dhcp_reservations": False, "firmware": False}
    assert integra["policy"] == {"vid": {"default": 31}}
    assert integra["record_keys"] == ["customer", "order"]
    assert integra["generation"] == 3
    assert integra["loaded_at"] == LOADED_AT


def test_build_registry_port_claim_attributed_to_integra_not_post():
    # The (rabbit-gouda, et32/1) port claim must surface under integra's
    # universe summary, not post's — mirrors how the two-role test asserts
    # triage owns its "ports: N claims". post keeps only its catch-all.
    out = roles_view.build_registry(ROWS_ROLES_3, ROWS_UNIVERSE_3)

    integra = next(r for r in out["roles"] if r["name"] == "integra")
    assert any("ports: 1 claims" in s for s in integra["universe_summary"])
    assert not any("catch-all" in s for s in integra["universe_summary"])

    post = next(r for r in out["roles"] if r["name"] == "post")
    assert not any("ports:" in s for s in post["universe_summary"])


def test_build_registry_post_stays_catch_all_integra_is_not():
    out = roles_view.build_registry(ROWS_ROLES_3, ROWS_UNIVERSE_3)
    assert out["catch_all_role"] == "post"
    integra = next(r for r in out["roles"] if r["name"] == "integra")
    assert not any("catch-all" in s for s in integra["universe_summary"])


def test_build_registry_integra_presence_no_regression_for_triage_post():
    # Adding the integra role + its port row must not perturb the triage or
    # post entries produced from the two-role fixture.
    two = roles_view.build_registry(ROWS_ROLES, ROWS_UNIVERSE)
    three = roles_view.build_registry(ROWS_ROLES_3, ROWS_UNIVERSE_3)
    by_name_two = {r["name"]: r for r in two["roles"]}
    by_name_three = {r["name"]: r for r in three["roles"]}
    for name in ("triage", "post"):
        assert by_name_three[name] == by_name_two[name]
    assert three["catch_all_role"] == two["catch_all_role"] == "post"


# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------

SWITCH_PORTS = [
    ("rabbit-gouda", "Ethernet6/1"),   # geometry port claim -> covered
    ("rabbit-edam", "Ethernet4/1"),    # switch claim -> covered
    ("turtle-3", "swp7"),              # prefix claim -> covered
    ("hare-1", "Ethernet2/1"),         # unknown switch, no claim -> uncovered
]


def test_coverage_geometry_claimed_port_not_listed():
    out = roles_view.coverage(ROWS_UNIVERSE, SWITCH_PORTS)
    assert ("rabbit-gouda", "Ethernet6/1") not in out


def test_coverage_switch_claim_not_listed():
    out = roles_view.coverage(ROWS_UNIVERSE, SWITCH_PORTS)
    assert ("rabbit-edam", "Ethernet4/1") not in out


def test_coverage_prefix_claim_not_listed():
    out = roles_view.coverage(ROWS_UNIVERSE, SWITCH_PORTS)
    assert ("turtle-3", "swp7") not in out


def test_coverage_unknown_switch_port_is_listed():
    out = roles_view.coverage(ROWS_UNIVERSE, SWITCH_PORTS)
    assert ("hare-1", "Ethernet2/1") in out


def test_coverage_empty_universe_lists_everything():
    out = roles_view.coverage([], SWITCH_PORTS)
    assert set(out) == set(SWITCH_PORTS)
