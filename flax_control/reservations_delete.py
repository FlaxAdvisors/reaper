"""Hard-delete a kea.hosts reservation from the WebUI.

Scope (see the scoping discussion): this is for cleaning up reservations whose
device is GONE and will not re-appear. flax-classify only ever (re)upserts a
reservation for a currently-observed occupant, and legacy-import is a one-shot
CLI — so a deleted orphan stays deleted. If the operator mis-judges and deletes a
row whose device is still live, classify simply re-creates it on the next cycle
(~30s), so the action is self-correcting and only "sticks" for truly-gone devices.

This is the one write where flax-control needs a real DELETE: migration 022 grants
`DELETE ON kea.hosts TO flax_control`. The matching kea.ipv6_reservations row is
removed automatically by its `ON DELETE CASCADE` FK (the cascade runs as the table
owner, so no DELETE grant on ipv6_reservations is required).

MAC lives in `dhcp_identifier` (BYTEA) where `dhcp_identifier_type = 0` — the same
identity used by flax_control/operator_notes.
"""
import re

from .db import get_pool

_MAC_HEX_RE = re.compile(r"^[0-9a-fA-F]{12}$")


class NotFound(Exception):
    """Raised when mac_hex is not a valid 12-char MAC (cannot identify a row)."""


def delete_reservation(mac_hex: str) -> bool:
    """DELETE the MAC-keyed kea.hosts reservation. Return True iff a row was
    removed (False if it was already gone — idempotent). Raises NotFound for
    malformed mac_hex before touching Postgres."""
    if not _MAC_HEX_RE.match(mac_hex):
        raise NotFound(mac_hex)
    with get_pool().connection() as conn:
        cur = conn.execute(
            "DELETE FROM kea.hosts "
            "WHERE dhcp_identifier = decode(%s, 'hex') "
            "  AND dhcp_identifier_type = 0",
            (mac_hex,))
        return cur.rowcount > 0
