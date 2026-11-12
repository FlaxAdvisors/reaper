"""Pure merge of one device's story across the single-writer tables into a
single newest-first timeline. Read-only; no DB import."""


def _item(when, source, what):
    # Sources arrive with heterogeneous `when` types: most are ISO strings
    # (_iso()'d upstream), but mac_ownership_events.at comes through as a raw
    # datetime. Normalise to an ISO string so the newest-first sort never
    # compares datetime against str (TypeError) and the template shows a
    # consistent format.
    if when is not None and hasattr(when, "isoformat"):
        when = when.isoformat()
    return {"when": when, "source": source, "what": what}


def build_biography(*, first_seen, observe, reconcile_actions,
                    ownership_events, work_records):
    items = []
    if first_seen:
        items.append(_item(first_seen, "device", "First seen on port"))
    if observe:
        resolved = observe.get("resolved") or {}
        sn = resolved.get("chassis_sn") or resolved.get("serial")
        if sn:
            items.append(_item(observe.get("last_polled"), "observe",
                               f"Serial {sn} resolved from BMC"))
    for a in reconcile_actions or []:
        detail = a.get("detail") or {}
        vid = detail.get("vid")
        what = a.get("action", "action")
        if vid is not None:
            what = f"{what} vid {vid}"
        items.append(_item(a.get("ts"), "reconcile", what))
    for ev in ownership_events or []:
        at, _mac, frm, to = ev[0], ev[1], ev[2], ev[3]
        items.append(_item(at, "ownership", f"Ownership {frm or '∅'} → {to or '∅'}"))
    for r in work_records or []:
        stage = r.get("stage") or r.get("kind") or "record"
        role = r.get("owner_role") or ""
        items.append(_item(r.get("at"), "record", f"{stage} · {role}".strip(" ·")))
    # Newest first; None whens sort last (empty string sorts before real ISO,
    # so invert by treating None as "").
    items.sort(key=lambda i: (i["when"] or ""), reverse=True)
    return items
