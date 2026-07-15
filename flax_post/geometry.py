# flax_post/geometry.py
"""Config-driven rack geometry for the post UI (docs/Post-UI-Design.md §4).

Loads post-geometry.json: a {racks, slots} object (or a bare slot list, legacy).
Derives per-slot placement (ou, col, group) and per-switch rack tags. The
convention lives in DATA; this module only loads + normalizes.
"""
import json
import logging
import os
import re

log = logging.getLogger("flax-post.geometry")

GEOMETRY_PATH = os.environ.get("FLAX_POST_GEOMETRY", "/etc/flax/post-geometry.json")

_PORT_RE = re.compile(r"^et(\d+)b(\d+)$")
_POS_RE = re.compile(r"^([1-4]):([1-4])$")
_QUARTER = {"A": "1:4", "B": "2:4", "C": "3:4", "D": "4:4"}


def to_arista(port: str) -> str:
    """'et6b1' -> 'Ethernet6/1'; passthrough for non-matching."""
    m = _PORT_RE.match(port or "")
    return f"Ethernet{m.group(1)}/{m.group(2)}" if m else port


def col_label(pos: str, width: int) -> str:
    """Display column label from a 'n:d' position and the width denominator."""
    pos = _QUARTER.get(pos, pos)
    m = _POS_RE.match(pos or "")
    n = int(m.group(1)) if m else 1
    if width == 1:
        return "full"
    if width == 2:
        return ["L", "R"][n - 1]
    if width == 3:
        return ["L", "C", "R"][n - 1]
    return ["A", "B", "C", "D"][n - 1]      # width 4


def _normalize_slot(s: dict) -> dict:
    pos = _QUARTER.get(s.get("pos", ""), s.get("pos", "1:1"))
    width = int(s.get("width", 1))
    bottom = int(s.get("bottom_ou", 0))
    return {
        "port": s["port"],
        "switch": s.get("switch", "rabbit-edam"),
        "bottom_ou": bottom,
        "height": int(s.get("height", 2)),
        "width": width,
        "pos": pos,
        "chassis": s.get("chassis"),
        "ou": bottom,                       # the label is the bottom OU
        "col": col_label(pos, width),
        "group": s.get("chassis"),          # group == chassis (UI clusters when null)
    }


def parse_geometry(data) -> dict:
    """Normalize a {racks, slots} object (or bare slot list) into {racks: {switch: {...}}, slots: [...]}."""
    if isinstance(data, list):
        data = {"racks": [], "slots": data}
    racks = {r["switch"]: {"tag": r.get("tag", ""), "label": r.get("label", r["switch"])}
             for r in (data.get("racks") or [])}
    slots = [_normalize_slot(s) for s in (data.get("slots") or [])]
    return {"racks": racks, "slots": slots}


def load_geometry(path: str = None) -> dict:
    """Read+parse the geometry file; {racks:{}, slots:[]} on absent/malformed."""
    if path is None:
        path = GEOMETRY_PATH
    try:
        with open(path) as f:
            return parse_geometry(json.load(f))
    except (OSError, ValueError) as exc:
        log.warning("geometry load failed (%s): %s", path, exc)
        return {"racks": {}, "slots": []}


def rack_tag(geo: dict, switch: str) -> str:
    """The hostname rack tag for a switch; '' when unknown (the primary rack)."""
    return (geo.get("racks", {}).get(switch) or {}).get("tag", "")
