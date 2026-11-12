"""Read-only Kea lease lookup by MAC (rackpost Discover's *-leased steps).

rackpost (flax_rack, on the bang) cannot read Postgres; it asks this module,
through GET /api/v1/leases, which lease each of a rack's MACs currently holds.
SELECT only on kea.lease4 (flax_control's grant: migration 016). Latest lease
per MAC, any state -- the caller decides whether an expired one counts.
"""
from .db import get_pool
from .order_reservations import BadMac, _mac_hex

MAX_MACS = 64

# hwaddr is bytea; address is the host-order int4 Kea stores (see queries.leases).
_LEASES_SQL = """
SELECT DISTINCT ON (hwaddr)
       encode(hwaddr, 'hex') AS mac,
       host('0.0.0.0'::inet + address) AS ip,
       expire, state, subnet_id, hostname
  FROM kea.lease4
 WHERE hwaddr = ANY(%(macs)s)
 ORDER BY hwaddr, expire DESC
"""


def leases_by_mac(macs) -> dict[str, dict]:
    """{mac12hex: {ip, expire (ISO), state, subnet_id, hostname}} for the MACs
    that hold a lease. Raises BadMac before any DB access on a malformed MAC
    or more than MAX_MACS of them."""
    if not isinstance(macs, (list, tuple)) or len(macs) > MAX_MACS:
        raise BadMac(f"at most {MAX_MACS} macs")
    hexes = sorted({_mac_hex(m) for m in macs})
    if not hexes:
        return {}
    args = {"macs": [bytes.fromhex(h) for h in hexes]}
    with get_pool().connection() as conn:
        rows = conn.execute(_LEASES_SQL, args).fetchall()
    return {mac: {"ip": ip, "expire": expire.isoformat() if expire else None,
                  "state": state, "subnet_id": subnet, "hostname": host or ""}
            for mac, ip, expire, state, subnet, host in rows}
