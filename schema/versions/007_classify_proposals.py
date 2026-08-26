"""Add classify_proposals table for flax-classify shadow output.

This table is the shadow-mode landing pad for flax-classify's (switch,
port, mac, kind, vid) -> (ipv4_address, hostname) outputs. Once Kea is
installed (Plan 5), classify writes to kea.hosts directly and a follow-up
migration drops this table.

The flax_classify role itself is already created (LOGIN) in migration 001;
this migration only adds the table and its grants. Single-writer pattern:
flax_classify is the only role with INSERT/UPDATE/DELETE; flax_control and
flax_reconcile get SELECT-only.

Revision ID: 007000000001
Revises: 006000000001
"""
from alembic import op

revision = "007000000001"
down_revision = "006000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE classify_proposals (
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
    op.execute(
        "CREATE INDEX classify_proposals_switch_port_idx "
        "ON classify_proposals (switch, port)"
    )

    # Single-writer: only flax_classify mutates this table.
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON classify_proposals "
        "TO flax_classify"
    )
    # Readers
    op.execute("GRANT SELECT ON classify_proposals TO flax_control")
    op.execute("GRANT SELECT ON classify_proposals TO flax_reconcile")

    # flax_classify needs read on its inputs. switch_facts is already
    # granted in 002; observe_state is owned by flax_observe (003) and was
    # not previously granted to classify -- add it here so the classifier
    # can read resolved BMC MACs / host attributes.
    op.execute("GRANT SELECT ON observe_state TO flax_classify")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON observe_state FROM flax_classify")
    op.execute("DROP TABLE IF EXISTS classify_proposals")
