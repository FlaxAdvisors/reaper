"""flax-control: hard-delete reservations + DNS aliases.

Two WebUI features land here:

  1. Delete a reservation. flax-control gains DELETE on kea.hosts so the UI can
     clean up orphaned reservations (devices that are gone). The matching
     kea.ipv6_reservations row is removed by its ON DELETE CASCADE FK (the
     cascade runs as the table owner, so no DELETE grant on ipv6_reservations is
     needed). flax-classify never re-creates a non-occupant's reservation and
     legacy-import is a one-shot CLI, so a deleted orphan stays deleted.

  2. DNS aliases. The public.reservations view gains an `aliases` column — the
     space-joined user_context.aliases list (written by flax-control via the
     existing column-level UPDATE(user_context) grant; no new grant needed for
     writing). flax-classify reads user_context.aliases each cycle and renders
     the extra names into the dnsmasq hosts file. NULL when no aliases (NULLIF on
     the empty join), mirroring operator_note's NULL-when-absent shape.

CREATE OR REPLACE VIEW can only append columns, so `aliases` follows the
migration-020 column list (…, ipv6_address, aliases) unchanged.

Revision ID: 022000000001
Revises: 021000000001
"""
from alembic import op

revision = "022000000001"
down_revision = "021000000001"
branch_labels = None
depends_on = None


# Migration 020's view body, verbatim, plus the trailing `aliases` column.
_VIEW_SQL_WITH_ALIASES = """
    CREATE OR REPLACE VIEW public.reservations AS
    SELECT
      encode(h.dhcp_identifier, 'hex')                           AS mac_hex,
      host('0.0.0.0'::inet + h.ipv4_address)                     AS ipv4_address,
      h.hostname,
      h.dhcp4_subnet_id                                          AS subnet_id,
      (h.user_context::jsonb) -> 'classify' ->> 'switch'         AS switch,
      (h.user_context::jsonb) -> 'classify' ->> 'port'           AS port,
      (h.user_context::jsonb) -> 'classify' ->> 'kind'           AS kind,
      ((h.user_context::jsonb) -> 'classify' ->> 'vid')::int     AS vid,
      (h.user_context::jsonb) ->> 'operator_note'                AS operator_note,
      ((h.user_context::jsonb) -> 'classify' ->> 'generation')::int AS generation,
      ((h.user_context::jsonb) -> 'classify' ->> 'updated_at')   AS updated_at,
      host(r6.address)                                           AS ipv6_address,
      NULLIF(array_to_string(ARRAY(
        SELECT jsonb_array_elements_text((h.user_context::jsonb) -> 'aliases')
      ), ' '), '')                                               AS aliases
    FROM kea.hosts h
    LEFT JOIN kea.ipv6_reservations r6 ON r6.host_id = h.host_id
    WHERE h.dhcp_identifier_type = 0
"""

# Migration-020 view (no aliases column) for rollback.
_VIEW_SQL_020 = """
    CREATE OR REPLACE VIEW public.reservations AS
    SELECT
      encode(h.dhcp_identifier, 'hex')                           AS mac_hex,
      host('0.0.0.0'::inet + h.ipv4_address)                     AS ipv4_address,
      h.hostname,
      h.dhcp4_subnet_id                                          AS subnet_id,
      (h.user_context::jsonb) -> 'classify' ->> 'switch'         AS switch,
      (h.user_context::jsonb) -> 'classify' ->> 'port'           AS port,
      (h.user_context::jsonb) -> 'classify' ->> 'kind'           AS kind,
      ((h.user_context::jsonb) -> 'classify' ->> 'vid')::int     AS vid,
      (h.user_context::jsonb) ->> 'operator_note'                AS operator_note,
      ((h.user_context::jsonb) -> 'classify' ->> 'generation')::int AS generation,
      ((h.user_context::jsonb) -> 'classify' ->> 'updated_at')   AS updated_at,
      host(r6.address)                                           AS ipv6_address
    FROM kea.hosts h
    LEFT JOIN kea.ipv6_reservations r6 ON r6.host_id = h.host_id
    WHERE h.dhcp_identifier_type = 0
"""


def upgrade() -> None:
    # (1) flax-control may now hard-delete a reservation row.
    op.execute("GRANT DELETE ON kea.hosts TO flax_control")
    # (2) expose aliases in the operator view (append-only column).
    op.execute(_VIEW_SQL_WITH_ALIASES)


def downgrade() -> None:
    # Drop + recreate the view without the trailing aliases column (CREATE OR
    # REPLACE cannot remove a column); re-grant SELECT lost on DROP.
    op.execute("DROP VIEW IF EXISTS public.reservations")
    op.execute(_VIEW_SQL_020)
    op.execute("GRANT SELECT ON public.reservations TO flax_control")
    op.execute("REVOKE DELETE ON kea.hosts FROM flax_control")
