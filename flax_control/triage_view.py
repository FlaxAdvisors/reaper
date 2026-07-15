"""Builder for the Triage page — a node/state/time table echoing the old
switchportrecon 10988 dashboard, restricted to geometry.json (triage rack) ports.

geometry.json IS the triage rack: every entry is a triage DUT port (port + ou +
switch). We join each entry against observe_state (the 12 state vars + their
`since` timestamps + last_polled), color-classify each var by value (the old
dashboard's green/red/gray), and sort by OU (rack position). Ports in observe_state
but NOT in geometry (e.g. turtle swp OOB-mgmt ports) are excluded — they're 'post',
not 'triage'.
"""
import re

from .triage_compat import _VARS_ORDER, internal_port, display_port, arista_port

# Re-export the canonical column order for the template headers.
VAR_ORDER = _VARS_ORDER

# Value -> colour class, matching scripts/switchportrecond.py's dashboard CSS:
#   green  (good):    link, found, ok, on, openbmc, traditional
#   red    (bad):     nolink, fail, notfound, off
#   gray   (neutral): unknown / absent / anything else
_GOOD = {"link", "found", "ok", "on", "openbmc", "traditional"}
_BAD = {"nolink", "fail", "notfound", "off"}


def classify_value(value) -> str:
    """'good' | 'bad' | 'neutral' for a state var's value (drives the cell colour)."""
    if value in _GOOD:
        return "good"
    if value in _BAD:
        return "bad"
    return "neutral"


def _ou_sort_key(ou: str):
    """Natural sort for an OU like '20L'/'22C'/'8R': (number, letter, raw).
    Non-conforming OUs sort last (big number) but stay deterministic by raw."""
    m = re.match(r"^(\d+)\s*([A-Za-z]*)$", (ou or "").strip())
    if not m:
        return (10**9, "", ou or "")
    return (int(m.group(1)), m.group(2).upper(), ou)


def build_rows(geometry: list, observe_all: dict) -> list:
    """Join geometry (triage ports) with observe_state_all() output.

    geometry: list of {port, ou, switch} (port in internal et6b1 form).
    observe_all: {"<switch>:<port>": {switch, port, vars, last_polled, ...}}.
    Returns OU-sorted rows: {port (Et6/1), port_url (Ethernet6/1), ou, switch,
    last_polled, observed, cells:[{name, value, since, cls} per VAR_ORDER]}.
    """
    # Index observe rows by (switch, canonical-internal-port) so the long Arista
    # form in observe_state matches geometry's et6b1, and a (None, port) fallback
    # for geometry entries that omit the switch.
    by_switch_port = {}
    by_port = {}
    for row in observe_all.values():
        ip = internal_port(row["port"])
        by_switch_port[(row["switch"], ip)] = row
        by_port[ip] = row

    rows = []
    for entry in geometry:
        port = internal_port(entry["port"])
        switch = entry.get("switch")
        ou = entry.get("ou", "")
        obs = by_switch_port.get((switch, port)) if switch else by_port.get(port)
        if obs is None and switch:
            obs = by_port.get(port)  # last-resort port-only match
        vars_ = (obs or {}).get("vars") or {}
        cells = []
        for name in VAR_ORDER:
            value = (vars_.get(name) or {}).get("value") or "unknown"
            since = (vars_.get(name) or {}).get("since")
            cells.append({"name": name, "value": value,
                          "since": since, "cls": classify_value(value)})
        rows.append({
            "port": display_port(port),
            "port_url": arista_port(port),
            "ou": ou,
            "switch": switch or (obs or {}).get("switch") or "",
            "last_polled": (obs or {}).get("last_polled"),
            "observed": obs is not None,
            "cells": cells,
        })

    rows.sort(key=lambda r: (_ou_sort_key(r["ou"]), r["switch"], r["port"]))
    return rows
