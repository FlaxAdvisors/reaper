"""Pure view functions for the /records work-record browser.

DB-read free: routes fetch via records_view; these functions only shape.
Mirrors the roles_view/shadow_view split. The smart-search contract (spec
2026-07-05 phase 5): one input; detection order mac → dut_id → text, where
text is tried as serial, then order, then customer; searching never 404s —
an empty result names the lenses tried.
"""

_KIND_ORDER = ("identity-fault", "fw-flash", "fw-action", "inventory", "sel")


def record_summary(kind, payload):
    """One-line human summary per record kind for the timeline row."""
    p = payload or {}
    if kind == "fw-flash":
        cur, tgt = p.get("current") or "?", p.get("target") or "?"
        line = "%s · %s→%s" % (p.get("terminal") or "?", cur, tgt)
        if p.get("fault_reason"):
            line += " · " + str(p["fault_reason"])
        return line
    if kind == "fw-action":
        return "%s · %s" % (p.get("action") or "?",
                            "ok" if p.get("ok") else (p.get("detail") or "failed"))
    if kind == "inventory":
        n = len(p.get("fru") or {})
        return "%d FRU fields · %d sensors" % (n, len(p.get("sdr_sensors") or []))
    if kind == "sel":
        return "%d new entries (%d total)" % (len(p.get("new") or []),
                                              len(p.get("entries") or []))
    if kind == "identity-fault":
        return p.get("reason") or "identity fault"
    return ""


def build_search(term, *, mac_duts, id_dut, serial_duts, order_duts,
                 customer_duts):
    """Aggregate lens results into grouped hit cards. Route decides which
    lenses ran (from detect_term); a lens that did not run passes []/None
    and is NOT listed in tried."""
    duts = []
    if mac_duts:
        duts.extend(mac_duts)
    if id_dut is not None:
        duts.append(id_dut)
    if serial_duts:
        duts.extend(d for d in serial_duts if d not in duts)
    orders = _group_key_hits(order_duts)
    customers = _group_key_hits(customer_duts)
    # tried = every lens the route actually executed (it passes a list —
    # possibly empty — for executed lenses, None for lenses it skipped).
    tried = [name for name, val in (("mac", mac_duts), ("dut_id", id_dut),
                                    ("serial", serial_duts),
                                    ("order", order_duts),
                                    ("customer", customer_duts))
             if val is not None]
    any_hit = bool(duts or orders or customers)
    return {"term": term, "hits": {"duts": duts, "orders": orders,
                                   "customers": customers},
            "tried": tried, "any": any_hit}


def _group_key_hits(duts_for_key_rows):
    """duts_for_key rows -> [] or a single group [{'duts': rows}] (the route
    queried one exact value; grouping exists so the template renders a card
    per key hit)."""
    rows = duts_for_key_rows or []
    return [{"duts": rows}] if rows else []


def build_biography(dut, assemblies, records):
    """dut = records_view.dut_by_id dict; assemblies = lookup_duts(mac,
    assembly='all') for the dut's p0_mac; records = dut_records rows."""
    other = [a for a in (assemblies or []) if a["dut_id"] != dut["dut_id"]]
    shaped = [dict(r, summary=record_summary(r.get("kind"), r.get("payload")))
              for r in (records or [])]
    kinds_present = sorted({r["kind"] for r in shaped},
                           key=lambda k: (_KIND_ORDER.index(k)
                                          if k in _KIND_ORDER else 99, k))
    return {"dut": dut,
            "serial_unreadable": (dut.get("serial") or "") == "",
            "other_assemblies": other,
            "records": shaped,
            "kinds": kinds_present}


def build_slice(key, value, rows):
    """records_by_key rows -> the order/customer engagement table model.
    Shaped as the precursor of the parked ship-out report."""
    shaped = [dict(r, summary=record_summary(r.get("kind"), r.get("payload")))
              for r in (rows or [])]
    return {"key": key, "value": value, "records": shaped,
            "dut_count": len({r["dut_id"] for r in shaped})}
