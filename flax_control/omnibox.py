"""Pure query -> DUT resolution for the top-bar omnibox.

Resolution order: exact MAC (any form) -> "switch/port" or "switch port" ->
free text matched against serials + reservation hostnames. Lookups are
injected callables so this module has no DB import and is trivially unit
tested; the /search route passes queries.* bindings.
"""
from . import records_view


def _dedupe(macs):
    seen, out = set(), []
    for m in macs:
        if m and m not in seen:
            seen.add(m); out.append(m)
    return out


def resolve(q, *, device_lookup, port_lookup, serial_lookup, hostname_lookup):
    q = (q or "").strip()
    if not q:
        return {"kind": "none", "q": q}

    # 1. MAC (bare-hex or separated) -> detect_term normalises to colon form.
    term_kind, norm = records_view.detect_term(q)
    if term_kind == "mac":
        mac = device_lookup(norm)
        if mac:
            return {"kind": "mac", "mac": mac}

    # 2. switch/port  (accept '/' or whitespace between switch and port)
    parts = q.replace("/", " ").split()
    if len(parts) == 2:
        mac = port_lookup(parts[0], parts[1])
        if mac:
            return {"kind": "mac", "mac": mac}

    # 3. free text -> serials + hostnames
    cands = _dedupe(list(serial_lookup(q)) + list(hostname_lookup(q)))
    if len(cands) == 1:
        return {"kind": "mac", "mac": cands[0]}
    if cands:
        return {"kind": "candidates",
                "candidates": [{"mac": m, "label": m} for m in cands]}
    return {"kind": "none", "q": q}
