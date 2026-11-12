# tests/test_flax_classify_vlan_policy.py
import json
from flax_classify import vlan_policy as vp


def test_desired_vid_known_family_steers():
    fp = {("leopard", "triage"): 16, ("leopard", "post"): 24}
    # access port, known family, not excluded -> family's (phase) vid
    assert vp.desired_vid("leopard", "triage", access_vid=17, in_no_steer=False,
                          fp_to_vid=fp) == 16


def test_desired_vid_unknown_family_holds():
    assert vp.desired_vid("unknown", "triage", access_vid=17, in_no_steer=False,
                          fp_to_vid={}) == 17  # hold at current


def test_desired_vid_no_steer_listed_skips():
    assert vp.desired_vid("leopard", "triage", access_vid=31, in_no_steer=True,
                          fp_to_vid={("leopard", "triage"): 16}) is None  # skip


def test_desired_vid_unmapped_family_falls_back_to_default():
    assert vp.desired_vid("widget", "post", access_vid=99, in_no_steer=False,
                          fp_to_vid={}) == vp.DEFAULT_VIDS["post"]  # 24


def test_load_fp_to_vid_inverts_vlans_json(tmp_path):
    p = tmp_path / "vlans.json"
    p.write_text(json.dumps([{"vid": 16, "family": "leopard", "phase": "triage"},
                             {"vid": 24, "family": "leopard", "phase": "post"}]))
    fp = vp.load_fp_to_vid(str(p))
    assert fp[("leopard", "triage")] == 16 and fp[("leopard", "post")] == 24


def test_phase_for_triage_when_in_geometry():
    geom = {"rabbit-fiesta": {"et6/1"}}
    assert vp.phase_for(geom, "rabbit-fiesta", "Ethernet6/1") == "triage"
    assert vp.phase_for(geom, "rabbit-fiesta", "Ethernet7/1") == "post"
    assert vp.phase_for(geom, "turtle-blues", "swp6") == "post"


# --- fp_to_vid_from_roles (Task 4: vid policy sourced from the role registry) ---
#
# The DEPLOYED gouda map (verified 2026-07-05): triage 16-23 = leopard,
# tiogapass, wedge100s, wedge400, yv3, brycecanyon, lightning, grandcanyon;
# post 24-31 same family order (grandcanyon=31 -- NOT tiogapass; the repo
# inventory's `vlans:` var carries the pending integra drift and must never
# be treated as this map's source).
DEPLOYED_TRIAGE_BY_FAMILY = {
    "leopard": 16, "tiogapass": 17, "wedge100s": 18, "wedge400": 19,
    "yv3": 20, "brycecanyon": 21, "lightning": 22, "grandcanyon": 23,
}
DEPLOYED_POST_BY_FAMILY = {
    "leopard": 24, "tiogapass": 25, "wedge100s": 26, "wedge400": 27,
    "yv3": 28, "brycecanyon": 29, "lightning": 30, "grandcanyon": 31,
}


def _deployed_role_defs():
    return {
        "triage": {"policy": {"vid": {"default": 16,
                                       "by_family": DEPLOYED_TRIAGE_BY_FAMILY}}},
        "post": {"policy": {"vid": {"default": 24,
                                     "by_family": DEPLOYED_POST_BY_FAMILY}}},
    }


def test_fp_to_vid_from_roles_builds_family_role_map():
    fp = vp.fp_to_vid_from_roles(_deployed_role_defs())
    assert fp[("leopard", "triage")] == 16
    assert fp[("grandcanyon", "post")] == 31  # firewall check: 31 = grandcanyon
    assert fp[("tiogapass", "post")] == 25    # NOT 31 (that's the integra drift)


def test_fp_to_vid_from_roles_none_when_no_role_has_by_family():
    """A registry that predates the vid policy (no by_family anywhere) must
    signal 'no policy here' via None so the caller falls back to
    load_fp_to_vid(vlans.json) -- registry-empty deploy-order safety."""
    role_defs = {"triage": {"policy": {"vid": {"default": 16}}},
                 "post": {"policy": {"vid": {"default": 24}}}}
    assert vp.fp_to_vid_from_roles(role_defs) is None


def test_fp_to_vid_from_roles_none_when_role_defs_empty():
    assert vp.fp_to_vid_from_roles({}) is None


def test_fp_to_vid_from_roles_none_when_policy_missing_entirely():
    """A role_def with no 'policy' key at all (e.g. a bare universe-only
    registry) must not KeyError -- treated the same as absent by_family."""
    role_defs = {"post": {"universe": {"catch_all": True}}}
    assert vp.fp_to_vid_from_roles(role_defs) is None


def test_fp_to_vid_from_roles_partial_registry_still_builds():
    """Only one role carries by_family (a partial/mid-rollout registry) ->
    builder still returns a dict (not None) covering just that role; the
    other phase's lookups fall through to DEFAULT_VIDS at the desired_vid
    layer exactly as an unmapped-family would."""
    role_defs = {"triage": {"policy": {"vid": {"default": 16,
                                                "by_family": {"leopard": 16}}}},
                 "post": {"policy": {"vid": {"default": 24}}}}
    fp = vp.fp_to_vid_from_roles(role_defs)
    assert fp == {("leopard", "triage"): 16}


def test_fp_to_vid_from_roles_parity_with_load_fp_to_vid(tmp_path):
    """fp_to_vid_from_roles(deployed-map role_defs) must equal
    load_fp_to_vid(deployed-map vlans.json) row-for-row (16 rows total)."""
    rows = []
    for family, vid in DEPLOYED_TRIAGE_BY_FAMILY.items():
        rows.append({"vid": vid, "family": family, "phase": "triage"})
    for family, vid in DEPLOYED_POST_BY_FAMILY.items():
        rows.append({"vid": vid, "family": family, "phase": "post"})
    assert len(rows) == 16

    vlans_path = tmp_path / "vlans.json"
    vlans_path.write_text(json.dumps(rows))

    from_roles = vp.fp_to_vid_from_roles(_deployed_role_defs())
    from_file = vp.load_fp_to_vid(str(vlans_path))
    assert from_roles == from_file
