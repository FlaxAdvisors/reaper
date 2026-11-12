# tests/test_roles_d_vid_by_family_render.py
"""Render tests for the roles.d/{triage,post}.json.j2 templates' new
policy.vid.by_family block (Task 4: vid policy sourced from the role
registry).

Repo root is derived from __file__ so this runs in any worktree. A minimal
`to_json` filter (= json.dumps, matching ansible's own to_json) is
registered on the bare jinja2.Environment since these templates are
normally rendered by ansible, not plain jinja2.
"""
import json
import os

import jinja2

from flax_classify.role_registry import load_role_dir, validate_roles

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = f"{REPO}/roles/apply_lease_agent/templates/roles.d"

# The DEPLOYED gouda map (verified 2026-07-05), exactly as pinned in
# inventories/eindhoven/group_vars/all/vars.yml -- grandcanyon=31, NOT
# tiogapass (the repo inventory's `vlans:` var carries the pending integra
# drift and must never be read as this map's source).
TRIAGE_VID_BY_FAMILY = {
    "leopard": 16, "tiogapass": 17, "wedge100s": 18, "wedge400": 19,
    "yv3": 20, "brycecanyon": 21, "lightning": 22, "grandcanyon": 23,
}
POST_VID_BY_FAMILY = {
    "leopard": 24, "tiogapass": 25, "wedge100s": 26, "wedge400": 27,
    "yv3": 28, "brycecanyon": 29, "lightning": 30, "grandcanyon": 31,
}


def _env():
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TPL),
        trim_blocks=True, lstrip_blocks=True, undefined=jinja2.StrictUndefined)
    env.filters["to_json"] = json.dumps
    return env


def _render(name, ctx):
    return _env().get_template(name).render(**ctx)


# --- triage.json.j2 ----------------------------------------------------

def test_triage_renders_valid_json_with_deployed_map():
    out = _render("triage.json.j2",
                  {"flax_role_triage_vid_by_family": TRIAGE_VID_BY_FAMILY})
    data = json.loads(out)  # raises if not valid JSON
    assert data["policy"]["vid"]["default"] == 16
    assert data["policy"]["vid"]["by_family"] == TRIAGE_VID_BY_FAMILY
    assert data["policy"]["vid"]["by_family"]["grandcanyon"] == 23


def test_triage_renders_valid_json_when_var_absent():
    """Other sites (no flax_role_triage_vid_by_family set) -> defaults to
    {} -- must still be valid JSON, not a StrictUndefined error."""
    out = _render("triage.json.j2", {})
    data = json.loads(out)
    assert data["policy"]["vid"]["by_family"] == {}
    assert data["policy"]["vid"]["default"] == 16


# --- post.json.j2 -------------------------------------------------------

def test_post_renders_valid_json_with_deployed_map():
    out = _render("post.json.j2",
                  {"flax_role_post_vid_by_family": POST_VID_BY_FAMILY})
    data = json.loads(out)
    assert data["policy"]["vid"]["default"] == 24
    assert data["policy"]["vid"]["by_family"] == POST_VID_BY_FAMILY
    # Integra-drift firewall: 31 = grandcanyon, never tiogapass (25 stays
    # tiogapass's own vid).
    assert data["policy"]["vid"]["by_family"]["grandcanyon"] == 31
    assert data["policy"]["vid"]["by_family"]["tiogapass"] == 25


def test_post_renders_valid_json_when_var_absent():
    out = _render("post.json.j2", {})
    data = json.loads(out)
    assert data["policy"]["vid"]["by_family"] == {}
    assert data["policy"]["vid"]["default"] == 24


def test_post_lifecycle_defaults():
    """linkdown_evict_secs defaults to 30 (2026-09-28: was 300). Operators pull
    a blade and wait for the tile to clear before slotting the next one; 30 s
    matches flax-observe's DOWN_DEBOUNCE_SECS. replace_debounce_cycles stays 2."""
    data = json.loads(_render("post.json.j2", {}))
    lifecycle = data["policy"]["lifecycle"]
    assert lifecycle["linkdown_evict_secs"] == 30
    assert lifecycle["replace_debounce_cycles"] == 2


def test_post_linkdown_evict_secs_overridable():
    data = json.loads(_render("post.json.j2", {"flax_role_post_linkdown_evict_secs": 90}))
    assert data["policy"]["lifecycle"]["linkdown_evict_secs"] == 90


# --- loader acceptance (contract item 5: "loader-accepted via
# load_role_dir on rendered output") -------------------------------------

def test_rendered_templates_are_loader_accepted(tmp_path):
    geom_path = tmp_path / "geometry.json"
    geom_path.write_text(json.dumps([{"port": "Ethernet6/1", "switch": "rabbit-gouda"}]))

    roles_dir = tmp_path / "roles.d"
    roles_dir.mkdir()
    (roles_dir / "triage.json").write_text(_render("triage.json.j2", {
        "flax_role_triage_vid_by_family": TRIAGE_VID_BY_FAMILY,
    }).replace("/etc/flax/geometry.json", str(geom_path)))
    (roles_dir / "post.json").write_text(_render("post.json.j2", {
        "flax_role_post_vid_by_family": POST_VID_BY_FAMILY,
        "flax_role_post_switches": ["rabbit-edam"],
        "flax_role_post_prefixes": ["turtle"],
        "flax_role_post_catch_all": True,
    }))

    defs = load_role_dir(str(roles_dir))  # raises RegistryError if malformed
    validate_roles(defs)  # raises RegistryError on universe conflicts

    from flax_classify.vlan_policy import fp_to_vid_from_roles
    fp = fp_to_vid_from_roles(defs)
    assert fp[("grandcanyon", "triage")] == 23
    assert fp[("grandcanyon", "post")] == 31
