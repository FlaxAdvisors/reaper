"""BMC firmware manifest matcher + flax-onetree version comparison.

The manifest (/etc/flax/bmc-firmware-versions.json) maps a platform name to an
entry: {match:{product_name:[regex...], bmc_kind}, target_version, check:{method,id},
flash:{method, artifact}, auto}. We match a probed product_name against the
regexes (same gate as flax_control/bmcfw_view), but return the FULL entry so the
flasher can reach flash.artifact + check.id.

Version compare is exact-equal on the normalized token: strip a known
'flax-onetree-' prefix, then tuple-compare (major, minor, patch, build_minute).
A loose date-prefix match would wrongly accept any build that day — do not soften.
"""
import json
import os
import re

_PREFIX = "flax-onetree-"


class Matcher:
    """Compiled product_name gate built from the manifest dict."""

    def __init__(self, data: dict):
        self._data = data or {}
        self._compiled = {
            name: [re.compile(p) for p in entry.get("match", {}).get("product_name", [])]
            for name, entry in self._data.items()
        }

    def match(self, product_name):
        """Return (platform_name, entry) for the first matching platform, else None."""
        if not product_name:
            return None
        for name, entry in self._data.items():
            if any(rx.search(product_name) for rx in self._compiled[name]):
                return name, entry
        return None

    def match_oem(self, product_name):
        """Return (name, entry) for the first `updatable: false` platform whose
        product_name regex matches, else None — a reachable Redfish board we
        deliberately do NOT flash (an OEM/AMI board with no flax-managed firmware)."""
        return _match_oem(self._data, self._compiled, product_name)


def _match_oem(data, compiled, product_name):
    """First `updatable: false` platform whose product_name regex matches -> (name, entry).

    Shared by Matcher/PostMatcher: an OEM board (reachable Redfish, no flax-managed
    firmware) is recognised by product_name against the updatable:false manifest
    entries only — never by the flashable onetree entry."""
    if not product_name:
        return None
    for name, entry in data.items():
        if entry.get("updatable") is False and any(
                rx.search(product_name) for rx in compiled.get(name, [])):
            return name, entry
    return None


class PostMatcher:
    """Manifest selector for the (mostly) homogeneous post rack.

    Post BMCs return no Redfish product name (Systems.Model is None) and post is
    unobserved, so we cannot gate onetree selection by product_name like the Triage
    Matcher. The post rack's flashable platform is a single one (flax-onetree Tioga
    Pass OpenBMC), so `match` returns the manifest entry whose check.id ==
    'bmc_active', ignoring the product name argument entirely. `match_oem`, however,
    DOES gate on product_name — a heterogeneous rack (braintree) can carry an OEM/AMI
    board the onetree driver must not flash, recognised via an updatable:false entry.
    """

    def __init__(self, data: dict):
        self._data = data or {}
        # Compile the product_name regexes of the OEM (updatable:false) entries only;
        # the onetree entry is selected by check.id, not product_name.
        self._oem_compiled = {
            name: [re.compile(p) for p in entry.get("match", {}).get("product_name", [])]
            for name, entry in self._data.items() if entry.get("updatable") is False
        }

    def match(self, product_name=None):
        for name, entry in self._data.items():
            if (entry.get("check") or {}).get("id") == "bmc_active":
                return name, entry
        return None

    def match_oem(self, product_name):
        """Return (name, entry) for the first `updatable: false` platform whose
        product_name regex matches, else None."""
        return _match_oem(self._data, self._oem_compiled, product_name)


def load_manifest(config_dir="/etc/flax") -> dict:
    """Read bmc-firmware-versions.json; {} on missing/malformed."""
    try:
        with open(os.path.join(config_dir, "bmc-firmware-versions.json")) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def target_version(entry: dict) -> str:
    return entry["target_version"]


def check_id(entry: dict) -> str:
    return entry["check"]["id"]


def artifact_rel_path(entry: dict) -> str:
    return entry["flash"]["artifact"]


def _parse(s: str):
    s = s.strip()
    if s.startswith(_PREFIX):
        s = s[len(_PREFIX):]
    ver, _, build = s.partition("-")
    try:
        parts = tuple(int(x) for x in ver.split("."))
        build_n = int(build) if build else 0
    except ValueError as e:
        raise ValueError("unparseable version: %r" % s) from e
    return parts + (build_n,)


def compare(reported: str, target: str) -> str:
    """'same' | 'older' | 'newer' (reported relative to target)."""
    r, t = _parse(reported), _parse(target)
    if r == t:
        return "same"
    return "older" if r < t else "newer"
