"""Known-bad parts that must never reach a ready-to-ship node (ruling
2026-09-12, OVH): a DIMM whose SPD identity matches a blocked signature fails
the population check, is listed on the population-check modal, and is painted
red in the INV/POP memory tables (the INV button goes red too).

The built-in list is the triage finding of 2026-09-10: 521 slots share serial
39E16042 with a 32 GB part string on 64 GB silicon (cloned SPD). A site can
extend it with /opt/flax/node_config/blocklist-dimms.json:
    [{"serial": "...", "part": "...", "mfg": "...", "reason": "..."}, ...]
(any field may be omitted: only the given fields must match, case-insensitive).
"""
import json
import os
import re

from . import population

BLOCKLIST_PATH = os.environ.get("FLAX_POST_BLOCKLIST",
                                os.path.join(population.PROFILE_DIR, "blocklist-dimms.json"))

BUILTIN = [
    {"serial": "39E16042", "part": "M386A4G40DM0-CPB", "mfg": "Samsung",
     "reason": "cloned SPD (triage finding 2026-09-10: shared serial, 32 GB part string on 64 GB silicon)"},
]


def load(path=None) -> list:
    """Built-in signatures plus the site file (silently absent)."""
    out = list(BUILTIN)
    try:
        with open(path or BLOCKLIST_PATH) as f:
            extra = json.load(f)
        if isinstance(extra, list):
            out += [e for e in extra if isinstance(e, dict)]
    except (OSError, ValueError):
        pass
    return out


def _norm(v) -> str:
    return re.sub(r"\s+", "", str(v or "")).lower()


def matches(dimm: dict, sig: dict) -> bool:
    """Every field the signature names must equal the DIMM's (whitespace and
    case ignored); a signature with no identity fields never matches."""
    keys = [k for k in ("serial", "part", "mfg") if sig.get(k)]
    return bool(keys) and all(_norm(dimm.get(k)) == _norm(sig[k]) for k in keys)


def check(dimms: list, sigs=None) -> list:
    """[{slot, size, mfg, serial, part, reason}] for every DIMM matching a
    blocked signature. `dimms` are dicts with slot/size/mfg/serial/part."""
    sigs = load() if sigs is None else sigs
    out = []
    for d in dimms:
        for sig in sigs:
            if matches(d, sig):
                out.append({"slot": d.get("slot") or "?", "size": d.get("size") or "",
                            "mfg": d.get("mfg") or "", "serial": d.get("serial") or "",
                            "part": d.get("part") or "", "reason": sig.get("reason") or "blocked part"})
                break
    return out


# dimmsum artifact line:  "  64 GB  DIMM A0  _Node0_Channel0_Dimm0  2666   Samsung  39E16042  M386A4G40DM0-CPB"
_DIMMSUM_RE = re.compile(
    r"^\s*(?P<size>\d+\s*[GM]B)\s+(?P<slot>DIMM\s+\S+)\s+\S+\s+(?P<speed>\d+)\s+"
    r"(?P<mfg>\S+)\s+(?P<serial>\S+)\s+(?P<part>\S+)\s*$")


def parse_dimmsum(text) -> list:
    """The agent's `dimmsum` inventory artifact -> [{slot, size, speed, mfg, serial, part}]."""
    out = []
    for line in (text or "").splitlines():
        m = _DIMMSUM_RE.match(line)
        if m:
            out.append({"slot": m.group("slot"), "size": m.group("size"), "speed": m.group("speed"),
                        "mfg": m.group("mfg"), "serial": m.group("serial"), "part": m.group("part")})
    return out


def check_dimmsum(text, sigs=None) -> list:
    return check(parse_dimmsum(text), sigs)


def describe(hit: dict) -> str:
    """One line for a failed rule / modal row."""
    return "blocked DIMM in %s: %s %s SN %s — %s" % (
        hit.get("slot"), hit.get("mfg"), hit.get("part"), hit.get("serial"), hit.get("reason"))
