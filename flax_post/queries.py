"""Post-device reservations (kea.hosts, source='post') joined to active DHCP leases.

Post devices are reserved by flax_classify/post_reserve.py
(user_context.source='post'); `post_devices` reports each reservation plus
whether Kea has handed out an active lease for it (kea.lease4, state=0).

The post switch IS observed. (It was not when this module was written, and that
claim outlived the fact: rabbit-edam carries 48 observe_state rows, 46 with both
bmc_mac and nic_mac, measured 2026-09-20.) `post_observed` below reads
flax-observe's identity witness so the producer can tell a reservation that
still belongs to the blade on the port from one a departed blade left behind.
"""
from .db import get_pool

# Canonical colon-lowercase MAC from a bytea column (identical to flax_control/queries.py).
_MAC_SQL = ("regexp_replace(encode(%s, 'hex'), "
            "'(..)(..)(..)(..)(..)(..)', E'\\\\1:\\\\2:\\\\3:\\\\4:\\\\5:\\\\6')")

_SQL = (
    "SELECT " + (_MAC_SQL % "h.dhcp_identifier") + " AS mac, "
    "host(('0.0.0.0'::inet) + h.ipv4_address) AS reservation_ip, "
    "host(('0.0.0.0'::inet) + l.address) AS lease_ip, "
    "h.hostname AS hostname, "
    "h.dhcp4_subnet_id AS vid, "
    "(h.user_context::jsonb)->'classify'->>'switch' AS switch, "
    "(h.user_context::jsonb)->'classify'->>'port' AS port, "
    "(h.user_context::jsonb)->'classify'->>'kind' AS kind, "
    "l.state AS lease_state, "
    "l.expire AS lease_expires "
    "FROM kea.hosts h "
    # Scope the lease join to the reservation's OWN ip (l.address = h.ipv4_address),
    # not just its mac. A device reclassified across subnets (e.g. vid25->vid24)
    # can have a stale, not-yet-expired lease on its OLD ip; a mac-only join would
    # duplicate the reservation onto that stale lease and the viewer would surface
    # the wrong bmc_ip (breaks the detail panel, PWR/IDNT/INV/POP, and SOL).
    "LEFT JOIN kea.lease4 l ON l.hwaddr = h.dhcp_identifier AND l.state = 0 "
    "                      AND l.address = h.ipv4_address "
    "WHERE h.dhcp_identifier_type = 0 "
    "  AND COALESCE((h.user_context::jsonb)->>'source', '') = 'post' "
    "ORDER BY (h.user_context::jsonb)->'classify'->>'switch' NULLS LAST, "
    "         (h.user_context::jsonb)->'classify'->>'port' NULLS LAST, mac"
)


def _row_to_device(row) -> dict:
    (mac, reservation_ip, lease_ip, hostname, vid,
     switch, port, kind, _, lease_expires) = row
    return {
        "mac": mac,
        "reservation_ip": reservation_ip,
        "lease_ip": lease_ip,
        "hostname": hostname,
        "vid": vid,
        "switch": switch,
        "port": port,
        "kind": kind,
        "leased": lease_ip is not None,
        "lease_expires": lease_expires.isoformat() if lease_expires else None,
    }


def post_devices() -> list[dict]:
    """All source='post' reservations with active-lease presence, sorted switch/port."""
    with get_pool().connection() as conn:
        cur = conn.execute(_SQL)
        rows = cur.fetchall()
    return [_row_to_device(r) for r in rows]


# The NARROWED contract, not observe_state itself (migration 034). Post is a
# consumer of flax-observe's published identity, so observe stays free to
# restructure `resolved` behind the view. See docs/flax-storage-delta.md.
_OBSERVED_SQL = (
    "SELECT port, bmc_mac, nic_mac, chassis_sn "
    "FROM observe_identity WHERE switch = %s"
)


def post_observed(switch: str) -> dict:
    """flax-observe's per-port identity for one switch -> {port: {...}}.

    Values may be None: a port observe knows about but has not resolved yet
    carries no bmc_mac/nic_mac. Callers must treat "unknown" as "not
    confirmed" -- never as "matches"."""
    with get_pool().connection() as conn:
        cur = conn.execute(_OBSERVED_SQL, (switch,))
        rows = cur.fetchall()
    return {port: {"bmc_mac": bmc_mac, "nic_mac": nic_mac, "chassis_sn": chassis_sn}
            for port, bmc_mac, nic_mac, chassis_sn in rows}
