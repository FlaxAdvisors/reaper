"""Grant flax_classify write access to kea.ipv6_reservations and expose the
reserved IPv6 address in public.reservations.

flax_classify becomes the writer of v6 reservation rows (mirroring its
existing kea.hosts grant from migration 009) so each classify cycle can
mint a per-device IPv6 reservation alongside the v4 one. It needs DML on
kea.ipv6_reservations plus USAGE/SELECT on the reservation_id sequence
(the INSERT relies on the column default nextval()).

The public.reservations view (last redefined verbatim by migration 011)
gains an `ipv6_address` column via a LEFT JOIN on kea.ipv6_reservations
keyed by host_id. The join is LEFT so v4-only reservations (no v6 row yet)
still appear with a NULL ipv6_address. kea.hosts is aliased `h` here so the
join key `h.host_id` is unambiguous against ipv6_reservations.host_id; all
existing columns/expressions are preserved exactly as in migration 011.

Revision ID: 020000000001
Revises: 019000000001
"""
from alembic import op

revision = "020000000001"
down_revision = "019000000001"
branch_labels = None
depends_on = None


# Current view + the new ipv6_address column. Body matches migration 011
# verbatim (same SELECT list, same WHERE) with kea.hosts aliased `h`, plus
# the LEFT JOIN and host(r6.address) projection.
_VIEW_SQL = """
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

# Pre-020 view (migration 011 form, unaliased, no ipv6_address) for rollback.
_VIEW_SQL_PRE020 = """
    CREATE OR REPLACE VIEW public.reservations AS
    SELECT
      encode(dhcp_identifier, 'hex')                            AS mac_hex,
      host('0.0.0.0'::inet + ipv4_address)                      AS ipv4_address,
      hostname,
      dhcp4_subnet_id                                           AS subnet_id,
      (user_context::jsonb) -> 'classify' ->> 'switch'          AS switch,
      (user_context::jsonb) -> 'classify' ->> 'port'            AS port,
      (user_context::jsonb) -> 'classify' ->> 'kind'            AS kind,
      ((user_context::jsonb) -> 'classify' ->> 'vid')::int      AS vid,
      (user_context::jsonb) ->> 'operator_note'                 AS operator_note,
      ((user_context::jsonb) -> 'classify' ->> 'generation')::int AS generation,
      ((user_context::jsonb) -> 'classify' ->> 'updated_at')    AS updated_at
    FROM kea.hosts
    WHERE dhcp_identifier_type = 0
"""


def upgrade() -> None:
    # flax_classify becomes the writer of v6 reservation rows.
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON kea.ipv6_reservations "
        "TO flax_classify"
    )
    op.execute(
        "GRANT USAGE, SELECT ON SEQUENCE "
        "kea.ipv6_reservations_reservation_id_seq TO flax_classify"
    )
    # CREATE OR REPLACE VIEW cannot drop/reorder existing columns, only append,
    # so ipv6_address must come last in the SELECT list -- which it does.
    op.execute(_VIEW_SQL)


def downgrade() -> None:
    # Roll the view back to the 011 form first: CREATE OR REPLACE VIEW cannot
    # remove the trailing column, so drop and recreate.
    op.execute("DROP VIEW IF EXISTS public.reservations")
    op.execute(_VIEW_SQL_PRE020)
    op.execute("GRANT SELECT ON public.reservations TO flax_control")
    op.execute(
        "REVOKE USAGE, SELECT ON SEQUENCE "
        "kea.ipv6_reservations_reservation_id_seq FROM flax_classify"
    )
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON kea.ipv6_reservations "
        "FROM flax_classify"
    )
