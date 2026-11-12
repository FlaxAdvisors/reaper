"""reaper fw: MANIFEST parse, lane check and the inventory entry rewrite (spec 2026-11-11 §6 steps 2, 5)."""
import difflib
from pathlib import Path

import pytest
import yaml

pytest.importorskip("reapercli")
from reapercli.lib import fwentry  # noqa: E402
from reapercli.lib.guards import Refused  # noqa: E402
from reapercli.lib.inventory import _Loader  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EHV = (ROOT / "inventories" / "eindhoven" / "group_vars" / "all" / "vars.yml").read_text()

# The real MANIFESTs on bang-gouda's share (2026-11-11), verbatim.
BMC_MANIFEST = ("purpose=xyz.openbmc_project.Software.Version.VersionPurpose.BMC\n"
                "version=flax-onetree-1.1.5\nMachineName=tiogapass\n"
                "ExtendedVersion=flax-onetree-1.1.5\n"
                "CompatibleName=com.meta.Hardware.BMC.Model.TiogaPass\n")
BIOS_MANIFEST = ("purpose=xyz.openbmc_project.Software.Version.VersionPurpose.Host\n"
                 "version=TPO_P26F\nMachineName=tiogapass\nExtendedVersion=TPO_P26F\n")


def load(text):
    return yaml.load(text, Loader=_Loader)


def changed_lines(old, new):
    return [l for l in difflib.ndiff(old.splitlines(), new.splitlines()) if l[:2] in ("- ", "+ ")]


def test_parse_manifest_real_bmc():
    assert fwentry.parse_manifest(BMC_MANIFEST) == {
        "version": "flax-onetree-1.1.5",
        "purpose": "xyz.openbmc_project.Software.Version.VersionPurpose.BMC"}


def test_parse_manifest_real_bios():
    assert fwentry.parse_manifest(BIOS_MANIFEST)["version"] == "TPO_P26F"


def test_parse_manifest_strips_whitespace_and_crlf():
    m = fwentry.parse_manifest("purpose=x.BMC\r\nversion=  flax-onetree-1.1.6 \r\n")
    assert m["version"] == "flax-onetree-1.1.6"


def test_parse_manifest_empty_version_raises():
    with pytest.raises(ValueError):
        fwentry.parse_manifest("purpose=x.BMC\nversion=\n")
    with pytest.raises(ValueError):
        fwentry.parse_manifest("purpose=x.BMC\n")


def test_check_lane_refuses_bmc_tar_for_bios():
    with pytest.raises(Refused):
        fwentry.check_lane("bios", fwentry.parse_manifest(BMC_MANIFEST)["purpose"])
    fwentry.check_lane("bmc", fwentry.parse_manifest(BMC_MANIFEST)["purpose"])


def test_check_lane_refuses_bios_tar_for_bmc():
    with pytest.raises(Refused):
        fwentry.check_lane("bmc", fwentry.parse_manifest(BIOS_MANIFEST)["purpose"])
    fwentry.check_lane("bios", fwentry.parse_manifest(BIOS_MANIFEST)["purpose"])


def test_lanes():
    assert fwentry.LANES["bmc"].var == "bmc_fw_platforms"
    assert fwentry.LANES["bios"].store == "/etc/flax/biosfw.json"
    assert fwentry.LANES["bios"].agent == "bios_fw"


@pytest.mark.parametrize("var,family", [("bmc_fw_platforms", "leopard-ami"),
                                        ("host_fw_platforms", "leopard-bios")])
def test_entry_refuses_report_only(var, family):
    with pytest.raises(Refused):
        fwentry.entry(EHV, var, family)


def test_entry_refuses_unknown_family():
    with pytest.raises(Refused):
        fwentry.entry(EHV, "bmc_fw_platforms", "nope")


def test_entry_reads_flashable():
    e = fwentry.entry(EHV, "bmc_fw_platforms", "tiogapass-openbmc")
    assert e["target_version"] == "flax-onetree-1.1.5"


def _assert_only(old, new, var, family, version, artifact):
    want = load(old)
    want[var][family]["target_version"] = version
    want[var][family]["flash"]["artifact"] = artifact
    assert load(new) == want
    assert len([l for l in changed_lines(old, new) if l.startswith("- ")]) == 2
    old_comments = [l for l in old.splitlines() if l.lstrip().startswith("#")]
    assert old_comments == [l for l in new.splitlines() if l.lstrip().startswith("#")]


def test_rewrite_flow_map_form():
    art = "SXC-Lab_OCP-Updates/OpenBMC/flax-onetree-1.1.6.tar"
    new = fwentry.rewrite_entry(EHV, "bmc_fw_platforms", "tiogapass-openbmc", "flax-onetree-1.1.6", art)
    _assert_only(EHV, new, "bmc_fw_platforms", "tiogapass-openbmc", "flax-onetree-1.1.6", art)


def test_rewrite_block_form():
    art = "SXC-Lab_OCP-Updates/X/tiogapass-bios-TPO_P27A.tar"
    new = fwentry.rewrite_entry(EHV, "host_fw_platforms", "tiogapass-bios", "TPO_P27A", art)
    _assert_only(EHV, new, "host_fw_platforms", "tiogapass-bios", "TPO_P27A", art)


def test_rewrite_same_family_name_other_var_untouched():
    text = ("a_fw:\n  fam:\n    target_version: \"1\"\n    flash: { method: m, artifact: \"a/1.tar\" }\n"
            "b_fw:\n  fam:\n    target_version: \"1\"\n    flash: { method: m, artifact: \"a/1.tar\" }\n")
    new = fwentry.rewrite_entry(text, "b_fw", "fam", "2", "a/2.tar")
    assert load(new)["a_fw"]["fam"] == {"target_version": "1", "flash": {"method": "m", "artifact": "a/1.tar"}}
    assert load(new)["b_fw"]["fam"]["target_version"] == "2"


POST = """\
host_fw_platforms:
  tiogapass-bios:
    target_version: "TPO_P26F"
    flash:
      method: redfish-update-multipart
      artifact: "A/tiogapass-bios-TPO_P26F.tar"
# post lane
post_bios_fw_platforms:
  - platform: "Wiwynn Tioga Pass"
    family: tiogapass
    host_fw_family: tiogapass-bios
    target: "TPO_P26F"
    afulnx_url: "http://bang/export/share/U/afulnx"
    bin_url: "http://bang/export/share/A/tiogapass-bios-TPO_P26F.tar"
    bin_path: image-bios
  - platform: "Quanta Leopard"
    family: leopard
    target: "F06_3B22"
    updatable: false
other: 1
"""


def test_rewrite_post_bios_linked_entries_only():
    new, n = fwentry.rewrite_post_bios(POST, "tiogapass-bios", "TPO_P27A", "B/tiogapass-bios-TPO_P27A.tar")
    assert n == 1
    want = load(POST)
    want["post_bios_fw_platforms"][0]["target"] = "TPO_P27A"
    want["post_bios_fw_platforms"][0]["bin_url"] = "http://bang/export/share/B/tiogapass-bios-TPO_P27A.tar"
    assert load(new) == want
    assert len([l for l in changed_lines(POST, new) if l.startswith("- ")]) == 2


def test_rewrite_post_bios_nothing_linked():
    new, n = fwentry.rewrite_post_bios(POST, "other-family", "X", "B/x.tar")
    assert (new, n) == (POST, 0)
    new, n = fwentry.rewrite_post_bios("a: 1\n", "tiogapass-bios", "X", "B/x.tar")
    assert (new, n) == ("a: 1\n", 0)
