"""Pure join of active leases (live DHCP truth) against reservations (desired).
A lease with no matching reservation is 'unreserved' — either a legit triage-
pool lease or an anomaly (and a stale unreserved lease can block a new
reservation until dhcp_release). Highlighted + sorted first."""


def build_leases(leases, reservations):
    res_by_mac = {}
    for r in reservations:
        m = (r.get("mac") or "").lower()
        if m:
            res_by_mac[m] = r
    rows, unreserved = [], 0
    for lz in leases:
        mac = (lz.get("mac") or "")
        res = None if mac.startswith("duid:") else res_by_mac.get(mac.lower())
        matched = res is not None
        if not matched:
            unreserved += 1
        rows.append({**lz, "matched": matched, "reservation": res})
    # Stable sort: unreserved (False<True inverted) first, input IP order kept.
    rows.sort(key=lambda d: d["matched"])
    return {"rows": rows, "unreserved_count": unreserved, "total": len(leases)}
