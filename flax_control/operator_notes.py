"""Read-merge-write helper for kea.hosts.user_context.operator_note.

The PATCH /api/v1/reservations/<mac>/operator_note endpoint dispatches
to update_operator_note. The read-merge-write pattern preserves any
other keys in user_context (notably user_context.classify, written by
flax-classify on every cycle).

Concurrency model: last-write-wins. The operator_note is informational;
optimistic concurrency would be overkill for v1. If two operators write
the same row within a tiny window, the later write survives.

Real kea.hosts schema notes (discovered during Plan 5 deploy):
  - MAC lives in `dhcp_identifier` (BYTEA) where `dhcp_identifier_type=0`
    (1=DUID, 2=circuit_id, 3=client_id, 4=flex).
  - `user_context` is TEXT, not JSONB — read parses JSON; write stores
    the JSON dump as text.
"""
import json
import re

from .db import get_pool

_MAC_HEX_RE = re.compile(r"^[0-9a-fA-F]{12}$")


class NotFound(Exception):
    """Raised when the requested mac doesn't exist in kea.hosts."""


def update_operator_note(mac_hex: str, note: str) -> None:
    """Read user_context; set or clear operator_note; UPDATE.

    `note` is a free-form string. Empty string CLEARS the key (so the
    `reservations` view's COALESCE-based operator_note column shows NULL).
    """
    if not _MAC_HEX_RE.match(mac_hex):
        # Bad-hex input cannot identify a resource; surface as 404 instead
        # of letting Postgres raise DataError ("invalid hexadecimal data")
        # which would bubble up as a generic 500.
        raise NotFound(mac_hex)
    with get_pool().connection() as conn:
        cur = conn.execute(
            "SELECT user_context FROM kea.hosts "
            "WHERE dhcp_identifier = decode(%s, 'hex') "
            "  AND dhcp_identifier_type = 0",
            (mac_hex,))
        row = cur.fetchone()
        if row is None:
            raise NotFound(mac_hex)
        # user_context is TEXT (Kea convention). Parse → mutate → re-dump.
        raw = row[0]
        ctx = json.loads(raw) if raw else {}
        if note:
            ctx["operator_note"] = note
        else:
            ctx.pop("operator_note", None)
        conn.execute(
            "UPDATE kea.hosts SET user_context = %s "
            "WHERE dhcp_identifier = decode(%s, 'hex') "
            "  AND dhcp_identifier_type = 0",
            (json.dumps(ctx), mac_hex))
