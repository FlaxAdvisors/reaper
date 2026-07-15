"""Strip /32 suffix from public.reservations.ipv4_address.

Migration 009's view used (inet)::text which includes /32 for hosts.
Use host(inet) to get the bare dotted-quad string — matches Plan 4.5's
host() fix for the classify_proposals view.

Revision ID: 011000000001
Revises: 010000000001
"""
from alembic import op

revision = "011000000001"
down_revision = "010000000001"
branch_labels = None
depends_on = None


_VIEW_SQL = """
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

_VIEW_SQL_OLD = """
    CREATE OR REPLACE VIEW public.reservations AS
    SELECT
      encode(dhcp_identifier, 'hex')                            AS mac_hex,
      ('0.0.0.0'::inet + ipv4_address)::text                    AS ipv4_address,
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
    op.execute(_VIEW_SQL)


def downgrade() -> None:
    op.execute(_VIEW_SQL_OLD)
