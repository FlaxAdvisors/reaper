"""Every triage site carries both firmware manifests (operator 2026-11-09: braintree gets
the same BIOS + BMC manifests). Targets are moved per site by `reaper fw <site>`
(spec 2026-11-11; eindhoven BIOS went to TPO_P27F alone, 1181dd9), so sites may differ;
what must hold at each site is that the triage BIOS target agrees with the population
profile the site's triage UI starts on, whose `BIOS Version:` rule gates QUALIFY.
The profiles are ONE set for every site (operator 2026-11-13), so a site whose target
differs from the shared profile's rule fails here: move it with `reaper fw` first."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SITES = ("eindhoven", "lavergne", "braintree")


def _vars(site):
    return yaml.safe_load((ROOT / "inventories" / site / "group_vars" / "all" / "vars.yml").read_text())


def test_every_site_has_both_manifests():
    for s in SITES:
        v = _vars(s)
        assert v.get("bmc_fw_platforms", {}).get("tiogapass-openbmc"), s
        assert v.get("host_fw_platforms", {}).get("tiogapass-bios"), s


PROFILES = ROOT / "files" / "services" / "_common" / "node_config"


def test_profiles_are_not_per_site():
    for s in SITES:
        assert not (ROOT / "files" / "configs" / s / "services" / "node_config").exists(), s


def _profile_bios_rule(profile):
    text = (PROFILES / profile).read_text()
    rules = re.findall(r"^'BIOS Version: ([^']*)'$", text, re.M)
    assert len(rules) == 1, (profile, rules)
    return rules[0]


def test_tioga_bios_target_agrees_with_the_sites_seeded_profile():
    checked = []
    for s in SITES:
        v = _vars(s)
        profile = v.get("apply_triage_compose_node_config")
        if not profile:
            continue                      # braintree seeds no profile
        assert _profile_bios_rule(profile) == v["host_fw_platforms"]["tiogapass-bios"]["target_version"], s
        checked.append(s)
    assert checked == ["eindhoven", "lavergne"]


def test_braintree_keeps_its_ami_oem_entry():
    assert _vars("braintree")["bmc_fw_platforms"]["tiogapass-ami-oem"]["updatable"] is False
