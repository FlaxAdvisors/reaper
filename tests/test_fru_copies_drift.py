"""The FRU-ID-0 parser and family-map modules are copied verbatim between
packages (flax-state-delta.md entry 2 convention; spec 2026-09-14-fru-id0-only D10)."""
import os
import pathlib

import flax_discover.family_map as discover_fm
import flax_observe.family_map as observe_fm
import flax_observe.family_map_live as observe_live
import flax_observe.fru as observe_fru
import flax_post.observe.family_map as post_fm
import flax_post.observe.family_map_live as post_live
import flax_post.observe.fru as post_fru


def _b(mod):
    return pathlib.Path(mod.__file__).read_bytes()


def test_fru_copies_are_identical():
    assert _b(observe_fru) == _b(post_fru)


def test_family_map_live_copies_are_identical():
    assert _b(observe_live) == _b(post_live)


def test_family_map_copies_match_flax_discover():
    assert _b(observe_fm) == _b(discover_fm)
    assert _b(post_fm) == _b(discover_fm)


def test_shared_modules_carry_no_role_strings():
    for mod in (observe_fru, observe_live):
        src = _b(mod).decode()
        assert "triage" not in src.lower()
        assert "'post'" not in src and '"post"' not in src


# The unlock station is a THIRD verbatim copy. The bundle is a self-contained
# tarball that runs on a blade with no network, so it cannot import from the
# reaper tree at runtime -- but it must identify a blade EXACTLY as the fleet
# does, or a station and triage disagree about which blade is in a slot.
# Same call create-unlock-with-ubuntu.sh already makes for the PSID->image map:
# extract, never hand-copy, and fail the build on drift.
_REAPER_ROOT = pathlib.Path(__file__).resolve().parents[1]
_STATION = _REAPER_ROOT / "scripts" / "unlock-with-ubuntu" / "payload" / "flaxfru"


def test_station_bundle_fru_copy_is_identical():
    assert (_STATION / "fru.py").read_bytes() == _b(observe_fru)


def test_station_bundle_family_map_copy_is_identical():
    assert (_STATION / "family_map.py").read_bytes() == _b(observe_fm)
