"""The post BIOS manifest's declared target must match the image it ships.

This is the landmine found live on bang-gouda 2026-09-04: the deployed manifest
declared `target: TPC_P26C` while `bin_url` pointed at `TPC_P26F.tar`. Trace
`biosd/service._reprobe_inflight` after a flash that WORKS:

    current becomes TPC_P26F
      -> current == target  is False  (no success, no claim release)
      -> current == baseline is False (the FLASH_TIMEOUT_S stall branch
                                       never fires either)
      -> falls to "still mid-flash/reboot -- no write, claim held"

The registry claim is held forever and the port sits in `flashing`, never
released and never faulted. The 900 s safety net is defeated PRECISELY BECAUSE
the flash succeeded.

There is a second consumer that must agree and cannot be checked from this repo:
the ghost-published population profile (/opt/flax/node_config/<profile>) carries
a `BIOS Version: <target>` presence rule that gates the QUALIFY verdict. If it
disagrees with the flashed version, every flashed node fails population-check.
Keep the three in step: manifest target, manifest bin_url, profile rule.
"""
import json
import os
import re

# Since 2026-11-11 (reaper fw) the post BIOS manifest is per site: rendered from
# post_bios_fw_platforms in each site's inventory, not a static role file.
REAPER = os.path.join(os.path.dirname(__file__), os.pardir)
SITES = ("eindhoven", "lavergne", "braintree")


def _vars(site):
    import yaml
    from reapercli.lib.inventory import _Loader
    with open(os.path.join(REAPER, "inventories", site, "group_vars", "all", "vars.yml")) as f:
        return yaml.load(f, Loader=_Loader)

# 'http://.../OEM-TP-wiwynn-FW-2021-02-23/TPC_P26F.tar' -> 'TPC_P26F'
# '.../tiogapass-bios-TPO_P26F.tar' -> 'TPO_P26F' (lowercase `name-` prefix, 2026-09-29;
# the tar's own MANIFEST says version=TPO_P26F)
_ARTIFACT_VER = re.compile(r"/(?:[a-z0-9]+-)*([A-Za-z0-9_]+)\.(?:tar|zip|tar\.gz|tgz)$")


def _entries():
    return [e for s in SITES for e in _vars(s)["post_bios_fw_platforms"]]


def _flashable():
    """Entries biosd may flash. A report-only entry (updatable: false, e.g. the
    Quanta Leopard, 2026-11-06) carries no artifact and is never flashed."""
    return [e for e in _entries() if e.get("updatable", True) is not False]


def test_flashable_entries_are_still_checked():
    assert any(e["family"] == "tiogapass" for e in _flashable())


def test_report_only_entries_carry_no_flash_fields():
    for e in _entries():
        if e.get("updatable") is False:
            assert not {"bin_url", "afulnx_url", "bin_path", "flags"} & set(e), e["platform"]


def test_manifest_is_a_non_empty_list_of_entries():
    entries = _entries()
    assert isinstance(entries, list) and entries


def test_declared_target_matches_the_version_the_bin_url_ships():
    """target must equal the version named by the downloaded artifact. A
    mismatch wedges the port permanently on a SUCCESSFUL flash."""
    for e in _flashable():
        m = _ARTIFACT_VER.search(e["bin_url"])
        assert m, "bin_url has no recognisable version: %s" % e["bin_url"]
        assert e["target"] == m.group(1), (
            "%s: target %r != artifact %r -- a successful flash would never "
            "match target, so the registry claim is held forever"
            % (e["platform"], e["target"], m.group(1)))


def test_bin_path_is_consistent_with_the_artifact():
    """bin_path is the path WITHIN the artifact. The P26C zip nested the image
    under `<VER>/<VER>.bin`; the P26F tar ships it flat as `image-bios`. A
    bin_path left over from the other artifact makes the flasher read a file
    that isn't there."""
    for e in _flashable():
        target, bin_path = e["target"], e["bin_path"]
        if e["bin_url"].endswith(".zip"):
            assert bin_path == "%s/%s.bin" % (target, target), (
                "%s: zip artifact expects a %s/%s.bin bin_path, got %r"
                % (e["platform"], target, target, bin_path))
        else:
            assert "/" not in bin_path, (
                "%s: tar artifact ships the image flat; nested bin_path %r "
                "will not resolve" % (e["platform"], bin_path))


def test_post_target_follows_the_triage_family():
    """reaper fw moves both in one commit: post's linked entry must name the same
    version and the same tar as triage's host_fw_platforms entry, at every site."""
    for s in SITES:
        v = _vars(s)
        for e in v["post_bios_fw_platforms"]:
            fam = e.get("host_fw_family")
            if not fam:
                continue
            tri = v["host_fw_platforms"][fam]
            assert e["target"] == tri["target_version"], s
            assert e["bin_url"].endswith("/" + tri["flash"]["artifact"]), s
