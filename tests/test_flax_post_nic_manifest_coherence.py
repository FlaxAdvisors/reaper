"""The post NIC manifest's target_version must match the image it ships.

Same invariant as test_flax_post_bios_manifest_coherence.py, and it matters
MORE here: `apply_flax_post_nic_fw_mode` defaults to `enforce` (BMC and BIOS
default to `detect`), and eindhoven does not pin it back. So a target naming a
version the shipped .bin cannot produce is not a latent wedge -- it is an
autonomous reflash loop on live NICs:

    classify -> needs_update (current != target)
    flash the only available bin -> card comes up at the BIN's version
    bin version != target -> still needs_update
    ... every pass, indefinitely

Guard the pair, not either half. Checked 2026-09-06 against a proposal to set
the target to 14.32.1910 while the share holds only 14_32_1912 images.

The filename carries THREE versions -- the firmware release, the UEFI option
ROM (14.25.17) and FlexBoot (3.6.502). Only `rel-<ver>` is the firmware.
"""
import json
import os
import re

MANIFEST = os.path.join(os.path.dirname(__file__), os.pardir, "roles", "apply_flax_post", "files",
                        "nic-firmware-versions.json")

# fw-ConnectX4Lx-rel-14_32_1912-MCX4111A-ACA_Ax-UEFI-14.25.17-... -> 14.32.1912
_REL = re.compile(r"-rel-(\d+)_(\d+)_(\d+)-")


def _entries():
    with open(MANIFEST) as f:
        return json.load(f)


def _bin_version(bin_name):
    m = _REL.search(bin_name)
    return ".".join(m.groups()) if m else None


def test_manifest_is_a_non_empty_mapping():
    d = _entries()
    assert isinstance(d, dict) and d


def test_target_version_matches_the_version_encoded_in_the_bin():
    """The one that matters. nicd is in ENFORCE mode -- a mismatch reflashes
    every card on every pass, forever, with nothing to converge on."""
    for psid, e in _entries().items():
        got = _bin_version(e["bin"])
        assert got, "%s: bin has no rel-<ver> segment: %s" % (psid, e["bin"])
        assert e["target_version"] == got, (
            "%s: target_version %r != the %r the bin actually flashes (%s) -- "
            "under enforce this reflashes forever"
            % (psid, e["target_version"], got, e["bin"]))


def test_dir_and_target_psid_agree():
    """The share is PSID-keyed (see the mellanox-fw-share-layout note), so the
    directory the image is fetched from must be the PSID being flashed TO."""
    for psid, e in _entries().items():
        assert e["dir"] == e["target_psid"], (
            "%s: dir %r != target_psid %r -- fetchfw would build a URL for a "
            "different card's image" % (psid, e["dir"], e["target_psid"]))


def test_oem_rebrand_keys_may_differ_from_the_psid_they_flash_to():
    """The KEY is the PSID the card REPORTS; target_psid is what it is flashed
    TO. For an OEM re-brand these differ ON PURPOSE -- flashing the generic
    Mellanox image re-brands the card. Four such entries exist (the three the
    operator confirmed, plus Dell 020NJD added 2026-11-04), so a test asserting key == target_psid
    would be wrong about the data, not the other way round."""
    rebrands = {k: e for k, e in _entries().items() if k != e["target_psid"]}
    assert set(rebrands) == {"FB_2450111034", "FB_0000000005", "HP_2420110034",
                             "DEL2420110034"}
    # every re-brand still points at a real Mellanox PSID dir
    for k, e in rebrands.items():
        assert e["target_psid"].startswith("MT_"), (
            "%s re-brands to %r, which is not a Mellanox PSID" % (k, e["target_psid"]))


def test_the_guard_catches_a_mismatched_target():
    """Prove the assertion above can actually fail -- a target that disagrees
    with its bin must be rejected, not silently accepted."""
    bad = {"MT_2410110034": {
        "dir": "MT_2410110034", "target_psid": "MT_2410110034",
        "bin": "fw-ConnectX4Lx-rel-14_32_1912-MCX4111A-ACA_Ax-UEFI-14.25.17.bin",
        "target_version": "14.32.1910"}}
    e = bad["MT_2410110034"]
    assert _bin_version(e["bin"]) == "14.32.1912"
    assert e["target_version"] != _bin_version(e["bin"])


def test_uefi_and_flexboot_versions_are_not_mistaken_for_the_firmware():
    """A looser regex would match the UEFI 14.25.17 or FlexBoot 3.6.502."""
    name = ("fw-ConnectX4Lx-rel-14_32_1912-MCX4111A-ACA_Ax-"
            "UEFI-14.25.17-FlexBoot-3.6.502.bin")
    assert _bin_version(name) == "14.32.1912"
