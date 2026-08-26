"""Plan 5 Kea cutover: drop classify_proposals shadow; grant flax_classify
on kea.hosts; create operator-friendly reservations view.

Pre-condition: kea-admin db-init pgsql has run, so kea.hosts exists.
The apply_kea role's cutover playbook runs db-init before this
migration.

Revision ID: 009000000001
Revises: 008000000001
"""
from alembic import op

revision = "009000000001"
down_revision = "008000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plan 4's shadow table is no longer needed -- flax-classify writes
    # directly to kea.hosts now.
    op.execute("DROP TABLE IF EXISTS classify_proposals")

    # flax_classify becomes the writer of reservation rows.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON kea.hosts TO flax_classify")
    op.execute("GRANT USAGE ON SCHEMA kea TO flax_classify")

    # flax_control reads kea.hosts, and may UPDATE the user_context
    # column only -- the column-level grant blocks any other column
    # mutation from the UI's PATCH endpoint. Note: this restricts WHICH
    # column may be UPDATEd, NOT what JSON sub-keys may be written
    # within user_context. The PATCH endpoint must do read-merge-write
    # to preserve user_context.classify; see flax_control/operator_notes.
    op.execute("GRANT SELECT ON kea.hosts TO flax_control")
    op.execute("GRANT UPDATE (user_context) ON kea.hosts TO flax_control")
    op.execute("GRANT USAGE ON SCHEMA kea TO flax_control")

    # Operator-friendly view: flattens user_context for the /reservations
    # UI so flax-control queries don't have to know the JSON shape.
    # NOTE: real kea.hosts schema discovered during Plan 5 deploy:
    #  - MAC lives in dhcp_identifier (BYTEA) WHERE dhcp_identifier_type=0
    #    (1=DUID, 2=circuit_id, 3=client_id, 4=flex). Filter to type=0
    #    so the view shows only MAC-keyed reservations.
    #  - user_context is TEXT, not JSONB — cast on read.
    op.execute("""
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
        WHERE dhcp_identifier_type = 0   -- MAC-keyed only
    """)
    op.execute("GRANT SELECT ON public.reservations TO flax_control")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS public.reservations")
    op.execute("REVOKE ALL ON kea.hosts FROM flax_control")
    op.execute("REVOKE ALL ON kea.hosts FROM flax_classify")
    # Recreate classify_proposals at minimal shape so flax-classify can
    # be rolled back without losing the upsert target. Matches the body
    # of migration 007 (drop the index + role since 007's downgrade
    # would handle those).
    op.execute("""
        CREATE TABLE IF NOT EXISTS classify_proposals (
            mac           TEXT PRIMARY KEY,
            switch        TEXT NOT NULL,
            port          TEXT NOT NULL,
            kind          TEXT NOT NULL CHECK (kind IN ('bmc', 'host', 'vm')),
            vid           INTEGER NOT NULL,
            ipv4_address  INET NOT NULL,
            hostname      TEXT NOT NULL,
            generation    BIGINT NOT NULL DEFAULT 1,
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS classify_proposals_switch_port_idx
        ON classify_proposals (switch, port)
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON classify_proposals TO flax_classify")
    op.execute("GRANT SELECT ON classify_proposals TO flax_control")
    op.execute("GRANT SELECT ON classify_proposals TO flax_reconcile")
