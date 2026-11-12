"""The unlock bundle's FWMAP must be EXTRACTED from update_mellanox.sh, never
copied.

The repo already carries three hand-synced copies of the PSID->image intent --
update_mellanox.sh's FWMAP (what burns the card), gen-nic-fw-manifest's INTENT
("MIRRORS the FWMAP", per its own docstring), and the generated
nic-firmware-versions.json. The unlock-with-ubuntu bundle would be a fourth if
its map were typed out by hand, so the builder parses the shell source instead.

That makes the parser load-bearing: a silent mis-parse ships a station that
flashes nothing, or flashes the wrong image. Every failure mode here must
raise, never return a partial map.
"""
import importlib.util
import json
import os

import pytest

_REAPER = os.path.join(os.path.dirname(__file__), os.pardir)
_PATH = os.path.join(_REAPER, "scripts", "unlock-with-ubuntu", "fwmap.py")
_spec = importlib.util.spec_from_file_location("fwmap", _PATH)
fwmap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fwmap)


SAMPLE = """#!/bin/bash -x

fwdst=/tmp/

FWREL=14_32_1912
FWSFX=UEFI-14.25.17-FlexBoot-3.6.502.bin
declare -A FWMAP=(
  # -- PCIe stand-up, dual-port --------------------------------------------
  [MT_2420110034]="MT_2420110034|fw-ConnectX4Lx-rel-${FWREL}-MCX4121A-ACA_Ax-${FWSFX}"   # MCX4121A-ACA 25GbE
  # -- OEM-branded overrides -----------------------------------------------
  [FB_2450111034]="MT_2450112034|fw-ConnectX4Lx-rel-${FWREL}-MCX4411A-ACQ_Ax-${FWSFX}"   # -> MCX4411A-ACQ
)

devuntouchable="secure-fw"
"""


def test_parse_expands_fwrel_and_fwsfx_into_full_bin_names():
    """The map's bin names are built from ${FWREL}/${FWSFX} at shell-expansion
    time. The bundle needs the literal filename to find the image on the share,
    so the parser must do that expansion itself."""
    got = fwmap.parse(SAMPLE)
    assert got == {
        "MT_2420110034": {
            "dir": "MT_2420110034",
            "bin": "fw-ConnectX4Lx-rel-14_32_1912-MCX4121A-ACA_Ax-"
                   "UEFI-14.25.17-FlexBoot-3.6.502.bin",
        },
        "FB_2450111034": {
            # An OEM re-brand: the key is the PSID the card REPORTS, dir is the
            # Mellanox PSID it is flashed TO. Collapsing these would fetch the
            # wrong image.
            "dir": "MT_2450112034",
            "bin": "fw-ConnectX4Lx-rel-14_32_1912-MCX4411A-ACQ_Ax-"
                   "UEFI-14.25.17-FlexBoot-3.6.502.bin",
        },
    }


def test_missing_fwmap_block_raises_instead_of_returning_an_empty_map():
    """If update_mellanox.sh is restructured and the marker moves, the build
    must stop. Returning {} would produce a bundle with no firmware in it that
    installs cleanly and silently flashes nothing."""
    with pytest.raises(fwmap.FwmapError):
        fwmap.parse(SAMPLE.replace("declare -A FWMAP=(", "declare -A OTHER=("))


def test_empty_fwmap_block_raises():
    """Same failure with the marker intact but the table emptied."""
    gutted = SAMPLE.replace(
        '  [MT_2420110034]="MT_2420110034|fw-ConnectX4Lx-rel-${FWREL}-'
        'MCX4121A-ACA_Ax-${FWSFX}"   # MCX4121A-ACA 25GbE\n', "").replace(
        '  [FB_2450111034]="MT_2450112034|fw-ConnectX4Lx-rel-${FWREL}-'
        'MCX4411A-ACQ_Ax-${FWSFX}"   # -> MCX4411A-ACQ\n', "")
    with pytest.raises(fwmap.FwmapError):
        fwmap.parse(gutted)


def test_missing_fwrel_raises():
    """Without FWREL the bin names cannot be expanded to real filenames."""
    with pytest.raises(fwmap.FwmapError):
        fwmap.parse(SAMPLE.replace("FWREL=14_32_1912\n", ""))


def test_an_unexpanded_shell_variable_in_a_bin_name_raises():
    """Guards the expansion itself: if a new ${VAR} is introduced into the bin
    template, the parser must not ship the literal '${VAR}' inside a filename
    that will then never be found on the share."""
    with pytest.raises(fwmap.FwmapError):
        fwmap.parse(SAMPLE.replace("-${FWSFX}", "-${FWNEW}-${FWSFX}"))


_SOURCE = os.path.join(_REAPER, "roles", "apply_pxe_payloads", "files", "post",
                       "update_mellanox.sh")
_MANIFEST = os.path.join(_REAPER, "roles", "apply_flax_post", "files",
                         "nic-firmware-versions.json")


def _real_map():
    with open(_SOURCE) as f:
        return fwmap.parse(f.read())


def test_the_real_update_mellanox_source_parses():
    got = _real_map()
    assert got, "parsed the live FWMAP to nothing"
    for psid, e in got.items():
        assert e["bin"].endswith(".bin"), "%s: %r is not a bin name" % (psid, e["bin"])
        assert e["dir"].startswith("MT_"), (
            "%s: dir %r is not a Mellanox PSID dir -- the share is PSID-keyed"
            % (psid, e["dir"]))


def test_extracted_map_agrees_with_the_committed_nic_manifest():
    """The real guard. nic-firmware-versions.json is generated from the same
    FWMAP (via gen-nic-fw-manifest's mirrored INTENT), so an extraction that
    disagrees with it means the parser read the table wrong -- and the bundle
    would fetch images the fleet does not agree are correct."""
    with open(_MANIFEST) as f:
        manifest = json.load(f)
    got = _real_map()
    assert set(got) == set(manifest), (
        "PSID sets differ; only in map: %s; only in manifest: %s"
        % (sorted(set(got) - set(manifest)), sorted(set(manifest) - set(got))))
    for psid, e in got.items():
        assert e["dir"] == manifest[psid]["dir"], "%s: dir differs" % psid
        assert e["bin"] == manifest[psid]["bin"], (
            "%s: bin differs\n  map:      %s\n  manifest: %s"
            % (psid, e["bin"], manifest[psid]["bin"]))


def test_to_tsv_emits_sorted_psid_dir_bin_rows():
    """The builder consumes the map as data. Sorted output keeps the bundle
    byte-reproducible for a given source, so two builds of the same commit can
    be compared."""
    out = fwmap.to_tsv({"MT_B": {"dir": "d2", "bin": "b2"},
                        "MT_A": {"dir": "d1", "bin": "b1"}})
    assert out.splitlines() == ["MT_A\td1\tb1", "MT_B\td2\tb2"]
