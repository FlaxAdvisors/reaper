"""reaper fw: a firmware tar's MANIFEST and the inventory entry it moves (spec 2026-11-11 §6).

The version string is read from the tar, never typed: `version=` must equal the
manifest `target_version` byte for byte, or the agent loops on a flash that
"succeeded". The inventory edit is line-based so comments and layout survive, and
it re-parses the result: anything but the intended fields changing is an error.
"""
import copy
import re
from dataclasses import dataclass

import yaml

from reapercli.lib.guards import Refused
from reapercli.lib.inventory import _Loader

SHARE_URL = "http://bang/export/share"


@dataclass(frozen=True)
class Lane:
    var: str
    playbook: str
    agent: str
    store: str
    manifest: str
    purpose_suffix: str


LANES = {
    "bmc": Lane("bmc_fw_platforms", "apply_bmc_fw_manifest.yml", "bmc_fw", "/etc/flax/bmcfw.json",
                "/etc/flax/bmc-firmware-versions.json", ".VersionPurpose.BMC"),
    "bios": Lane("host_fw_platforms", "apply_host_fw_manifest.yml", "bios_fw", "/etc/flax/biosfw.json",
                 "/etc/flax/host-firmware-versions.json", ".VersionPurpose.Host"),
}


def _load(text: str):
    return yaml.load(text, Loader=_Loader) or {}


def parse_manifest(text: str) -> dict:
    kv = {}
    for line in (text or "").splitlines():
        k, sep, v = line.partition("=")
        if sep:
            kv[k.strip()] = v.strip()
    if not kv.get("version"):
        raise ValueError("MANIFEST has no version=")
    return {"version": kv["version"], "purpose": kv.get("purpose", "")}


def check_lane(lane: str, purpose: str) -> None:
    want = LANES[lane].purpose_suffix
    if not purpose.endswith(want):
        raise Refused(f"tar purpose {purpose or '(none)'} is not a {lane} image (want *{want})")


def entry(vars_text: str, var: str, family: str) -> dict:
    e = (_load(vars_text).get(var) or {}).get(family)
    if not isinstance(e, dict):
        raise Refused(f"no {family!r} in {var}")
    if e.get("updatable") is False or not isinstance(e.get("flash"), dict):
        raise Refused(f"{family} is report-only (updatable: false or no flash): nothing to move")
    return e


def _block(lines: list, start_re: str, indent: int, after: int = 0) -> tuple:
    """(first, end) line indexes of the block whose header matches start_re at `indent`,
    searched from `after`; the block ends at the next non-blank, non-comment line with
    indent <= indent."""
    pat = re.compile(start_re)
    for i in range(after, len(lines)):
        if pat.match(lines[i]):
            j = i + 1
            while j < len(lines):
                s = lines[j]
                if s.strip() and not s.lstrip().startswith("#") and len(s) - len(s.lstrip()) <= indent:
                    break
                j += 1
            return i, j
    return None, None


def _set_quoted(line: str, key: str, value: str) -> str:
    new, n = re.subn(rf'(\b{re.escape(key)}:\s*)"[^"]*"', lambda m: f'{m.group(1)}"{value}"', line, count=1)
    if not n:
        new, n = re.subn(rf'(\b{re.escape(key)}:\s*)([^\s,}}#][^,}}#]*?)(\s*(?:[,}}#]|$))',
                         lambda m: f'{m.group(1)}"{value}"{m.group(3)}', line, count=1)
    return new if n else None


def _replace_in(lines: list, lo: int, hi: int, key: str, value: str) -> None:
    hits = [i for i in range(lo, hi) if not lines[i].lstrip().startswith("#")
            and _set_quoted(lines[i], key, value) is not None]
    if len(hits) != 1:
        raise RuntimeError(f"expected one {key}: line in the entry, found {len(hits)}")
    lines[hits[0]] = _set_quoted(lines[hits[0]], key, value)


def _check(old: str, new: str, mutate) -> None:
    want = copy.deepcopy(_load(old))
    mutate(want)
    if _load(new) != want:
        raise RuntimeError("inventory rewrite changed more than the intended fields")


def rewrite_entry(vars_text: str, var: str, family: str, version: str, artifact: str) -> str:
    lines = vars_text.split("\n")
    v0, v1 = _block(lines, rf"^{re.escape(var)}:\s*(#.*)?$", 0)
    if v0 is None:
        raise Refused(f"no {var} in the inventory")
    f0, f1 = _block(lines[:v1], rf"^  {re.escape(family)}:\s*(#.*)?$", 2, v0 + 1)
    if f0 is None:
        raise Refused(f"no {family!r} in {var}")
    _replace_in(lines, f0 + 1, f1, "target_version", version)
    _replace_in(lines, f0 + 1, f1, "artifact", artifact)
    new = "\n".join(lines)

    def mutate(d):
        d[var][family]["target_version"] = version
        d[var][family]["flash"]["artifact"] = artifact
    _check(vars_text, new, mutate)
    return new


def rewrite_post_bios(vars_text: str, host_family: str, version: str, artifact: str) -> tuple:
    items = _load(vars_text).get("post_bios_fw_platforms") or []
    linked = [i for i, e in enumerate(items) if isinstance(e, dict) and e.get("host_fw_family") == host_family]
    if not linked:
        return vars_text, 0
    lines = vars_text.split("\n")
    v0, v1 = _block(lines, r"^post_bios_fw_platforms:\s*(#.*)?$", 0)
    starts = [i for i in range(v0 + 1, v1) if re.match(r"^  - ", lines[i])]
    bin_url = f"{SHARE_URL}/{artifact}"
    for idx in linked:
        lo = starts[idx]
        hi = starts[idx + 1] if idx + 1 < len(starts) else v1
        _replace_in(lines, lo, hi, "target", version)
        _replace_in(lines, lo, hi, "bin_url", bin_url)
    new = "\n".join(lines)

    def mutate(d):
        for idx in linked:
            d["post_bios_fw_platforms"][idx]["target"] = version
            d["post_bios_fw_platforms"][idx]["bin_url"] = bin_url
    _check(vars_text, new, mutate)
    return new, len(linked)
