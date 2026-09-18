# fru.py -- the blade's FRU: Builtin FRU Device (ID 0) only.
#
# COPIED VERBATIM between flax_observe/fru.py and flax_post/observe/fru.py
# (reaper-devel tests/test_fru_copies_drift.py fails when they differ).
# Stdlib only, no I/O, no retries: callers read, this parses.
# Spec: reaper-devel docs/superpowers/specs/2026-09-14-fru-id0-only-design.md §4.
"""Blade identity and board data from FRU ID 0.

`ipmitool fru` prints one block per FRU device -- the blade (ID 0), the NIC,
the M.2 carrier -- and each carries its own Board Mfg / Product Serial. Only
ID 0 is the blade: nothing is read from another block, and there is no
fallback between serial fields (operator ruling 2026-09-14). The family
(family-map match on the ID 0 Product Name, else Board Product) picks the
field: a leopard's ship serial is its Chassis Serial, everyone else's is the
Product Serial.
"""
import re

from .family_map import match_family

SERIAL_FIELD_BY_FAMILY = {"leopard": "Chassis Serial"}
DEFAULT_SERIAL_FIELD = "Product Serial"

_HEADER_RE = re.compile(
    r"^\s*FRU Device Description\s*:\s*(?P<desc>.*?)\s*(?:\(ID\s*(?P<id>[^)]*)\))?\s*$")
# weutil (Wedge switches) spells the serial key out.
_KEY_ALIASES = {"Product Serial Number": "Product Serial"}
_NOT_PRESENT = "Device not present"


def _parse_id(raw):
    try:
        return int((raw or "").strip())
    except ValueError:
        return None


def _block(fru_id, description, lines):
    fields, reason = {}, None
    for line in lines:
        s = line.strip()
        if s.startswith(_NOT_PRESENT):
            reason = reason or s
            continue
        if ":" not in s:
            continue
        k, v = s.split(":", 1)
        k = _KEY_ALIASES.get(k.strip(), k.strip())
        v = v.strip()
        if k and v and k not in fields:
            fields[k] = v
    if reason is None and not fields:
        reason = "no FRU fields"
    return {"id": fru_id, "description": description,
            "present": reason is None, "reason": reason, "fields": fields}


def fru_blocks(text):
    """Split FRU text into blocks. Text with no FRU headers (`fru print 0` on
    some BMCs, /run/fru, fruid-util, weutil) is one ID 0 block. Within a block
    the first non-empty value of a key wins; nothing crosses blocks."""
    lines = (text or "").splitlines()
    if not any(_HEADER_RE.match(line) for line in lines):
        return [_block(0, "", lines)]
    blocks, cur = [], None
    for line in lines:
        m = _HEADER_RE.match(line)
        if m:
            cur = {"id": _parse_id(m.group("id")), "description": m.group("desc").strip(), "lines": []}
            blocks.append(cur)
        elif cur is not None:
            cur["lines"].append(line)
    return [_block(b["id"], b["description"], b["lines"]) for b in blocks]


def read_baseboard(text, family_map):
    """The blade's FRU (ID 0) as one dict.

    state: "ok" (the family's serial field is set), "no_serial" (FRU 0 read,
    that field empty -- the node must not ship), "absent" (no ID 0 block, ID 0
    "Device not present", or an ID 0 block with no fields). addons: the
    descriptions of the other FRU devices that are present (presence only)."""
    blocks = fru_blocks(text)
    base = next((b for b in blocks if b["id"] == 0), None)
    out = {"state": "absent", "reason": None, "family": None,
           "board_mfg": None, "board_product": None, "product_name": None,
           "serial": None, "serial_field": DEFAULT_SERIAL_FIELD,
           "addons": [b["description"] for b in blocks if b["id"] != 0 and b["present"]]}
    if base is None:
        out["reason"] = "no FRU 0 block"
        return out
    if not base["present"]:
        out["reason"] = base["reason"]
        return out
    f = base["fields"]
    out["board_mfg"] = f.get("Board Mfg")
    out["board_product"] = f.get("Board Product")
    out["product_name"] = f.get("Product Name")
    out["family"] = match_family(family_map or {}, out["product_name"] or out["board_product"])
    out["serial_field"] = SERIAL_FIELD_BY_FAMILY.get(out["family"], DEFAULT_SERIAL_FIELD)
    out["serial"] = f.get(out["serial_field"])
    out["state"] = "ok" if out["serial"] else "no_serial"
    return out
