# tests/test_inventory_vlan_firewall_pin.py
"""Integra-drift firewall (global-context hard rule, verified 2026-07-05):
pins the DEPLOYED gouda vid-by-family transcription in
`inventories/eindhoven/group_vars/all/vars.yml` (`flax_role_post_vid_by_family`
/ `flax_role_triage_vid_by_family`) against the pending integra drift.

The repo's `vlans:` var (elsewhere in this same inventory) carries a PENDING
flip where tiogapass moves onto vid 31 for the post phase -- that is a
future-state value, NOT what gouda runs today. This test reads the actual
YAML file (no hardcoded copy drifting out of sync, no import of the live
inventory var) and asserts the deployed literals: post grandcanyon=31 (post
tiogapass must stay 25, never 31), triage grandcanyon=23. If this test ever
needs to change, that change must be backed by a re-verified gouda
`/etc/flax/vlans.json` dump, not just an inventory edit.
"""
import os

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VARS_FILE = f"{REPO}/inventories/eindhoven/group_vars/all/vars.yml"


def _load_vars():
    with open(VARS_FILE) as f:
        return yaml.safe_load(f)


def test_post_vid_by_family_pins_grandcanyon_31_not_tiogapass():
    data = _load_vars()
    post = data["flax_role_post_vid_by_family"]
    assert post["grandcanyon"] == 31
    assert post["tiogapass"] == 25
    # The integra-drift firewall: tiogapass must never occupy vid 31 here.
    assert post["tiogapass"] != 31
    assert 31 not in [v for k, v in post.items() if k != "grandcanyon"]


def test_triage_vid_by_family_pins_grandcanyon_23():
    data = _load_vars()
    triage = data["flax_role_triage_vid_by_family"]
    assert triage["grandcanyon"] == 23
