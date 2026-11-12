"""Pure diff: which reserved devices are not on their reserved IP.

Inputs are already-fetched rows (db.py does the SQL); keeping the logic pure
makes the kick-trigger exhaustively testable without a database.
"""


def _norm(mac: str) -> str:
    return mac.strip().lower()


def compute_mismatches(lease_rows: list[dict], host_rows: list[dict]) -> list[dict]:
    """Return [{mac, lease_ip, reserved_ip}] for every reserved device whose
    current lease IP differs from (or is absent vs) its reservation.

    lease_rows: [{mac, ip}] from kea.lease4 (active leases).
    host_rows:  [{mac, ip}] from kea.hosts (reservations with an ipv4 address).
    """
    lease_by_mac = {_norm(r["mac"]): r["ip"] for r in lease_rows}
    out = []
    for h in host_rows:
        mac = _norm(h["mac"])
        reserved_ip = h["ip"]
        if reserved_ip is None:
            continue
        lease_ip = lease_by_mac.get(mac)
        if lease_ip != reserved_ip:
            out.append({"mac": mac, "lease_ip": lease_ip,
                        "reserved_ip": reserved_ip})
    return out
