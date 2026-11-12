"""Known-bad parts that must never reach a ready-to-ship node (ruling
2026-09-12, OVH): a DIMM whose SPD identity matches a blocked signature fails
the population check, is listed on the population-check modal, and is painted
red in the INV/POP memory tables (the INV button goes red too).

The signatures live in ONE file shared with the triage api (operator
2026-10-02, reaper-devel spec 2026-10-02-triage-node-config-pop):
/opt/flax/node_config/blocklist-parts.json, beside the population profiles --
where the other things a node is judged against are defined.

    {"dimms": [{"serial": "...", "part": "...", "mfg": "...", "reason": "..."}]}

Only the fields an entry names must match (case and whitespace ignored). There
is no built-in list any more: a missing file blocks nothing. A file that does
not parse, is not that shape, or holds an entry naming none of serial / part /
mfg (a typo such as "serail") FAILS CLOSED: check() reports one "blocklist
unreadable" hit, so the population check goes red with a reason instead of
quietly passing a part the file meant to block.
"""
import json
import os
import re

from . import population

FILE_NAME = "blocklist-parts.json"
_IDENTITY = ("serial", "part", "mfg")


def path() -> str:
    """FLAX_POST_BLOCKLIST overrides; else the file in the profile dir. Read at
    call time so tests can point PROFILE_DIR elsewhere."""
    return os.environ.get("FLAX_POST_BLOCKLIST") or os.path.join(population.PROFILE_DIR, FILE_NAME)


def load(path_=None) -> list:
    """The DIMM signatures. [] when the file is absent. When it is present but
    unusable, a one-element list holding an error marker (see module doc)."""
    p = path_ or path()
    try:
        with open(p) as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return [_unreadable(p, "not valid JSON")]
    dimms = data.get("dimms") if isinstance(data, dict) else None
    if not isinstance(dimms, list) or not all(isinstance(e, dict) for e in dimms):
        return [_unreadable(p, 'not {"dimms": [...]}')]
    if not all(any(e.get(k) for k in _IDENTITY) for e in dimms):
        return [_unreadable(p, "an entry names none of serial/part/mfg")]
    return dimms


def _unreadable(p, why) -> dict:
    return {"error": "%s: %s" % (p, why)}


def _norm(v) -> str:
    return re.sub(r"\s+", "", str(v or "")).lower()


def matches(dimm: dict, sig: dict) -> bool:
    """Every field the signature names must equal the DIMM's (whitespace and
    case ignored); a signature with no identity fields never matches."""
    keys = [k for k in _IDENTITY if sig.get(k)]
    return bool(keys) and all(_norm(dimm.get(k)) == _norm(sig[k]) for k in keys)


def check(dimms: list, sigs=None) -> list:
    """[{slot, size, mfg, serial, part, reason}] for every DIMM matching a
    blocked signature. `dimms` are dicts with slot/size/mfg/serial/part. An
    unreadable file yields its single error hit instead (fails closed)."""
    sigs = load() if sigs is None else sigs
    errors = [s for s in sigs if s.get("error")]
    if errors:
        return [{"slot": "?", "size": "", "mfg": "", "serial": "", "part": "",
                 "error": errors[0]["error"], "reason": "blocklist unreadable"}]
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
    if hit.get("error"):
        return "blocklist unreadable: %s -- fix the file" % hit["error"]
    return "blocked DIMM in %s: %s %s SN %s — %s" % (
        hit.get("slot"), hit.get("mfg"), hit.get("part"), hit.get("serial"), hit.get("reason"))
