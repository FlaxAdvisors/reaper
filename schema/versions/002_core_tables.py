"""core write-target tables: devices, switch_facts, desired_port

Revision ID: 002000000001
Revises: 001000000001
"""
from alembic import op

revision = "002000000001"
down_revision = "001000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE devices (
            mac                    TEXT PRIMARY KEY,
            switch                 TEXT NOT NULL,
            port                   TEXT NOT NULL,
            kind                   TEXT NOT NULL CHECK (kind IN ('bmc','host','vm')),
            first_seen             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            classification_source  TEXT,
            latched                JSONB NOT NULL DEFAULT '{}'::jsonb,
            polled                 JSONB NOT NULL DEFAULT '{}'::jsonb,
            generation             BIGINT NOT NULL DEFAULT 1,
            updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX devices_family_idx ON devices ((latched->>'family'))")
    op.execute("CREATE INDEX devices_serial_idx ON devices ((latched->>'serial'))")
    op.execute("CREATE INDEX devices_switch_port_idx ON devices (switch, port)")
    op.execute("CREATE INDEX devices_kind_idx ON devices (kind)")

    op.execute("""
        CREATE TABLE switch_facts (
            switch        TEXT PRIMARY KEY,
            driver        TEXT NOT NULL,
            polled_at     TIMESTAMPTZ NOT NULL,
            reachable     BOOLEAN NOT NULL,
            generation    BIGINT NOT NULL,
            port_mask     JSONB NOT NULL,
            ports         JSONB NOT NULL
        )
    """)

    op.execute("""
        CREATE TABLE desired_port (
            switch        TEXT NOT NULL,
            port          TEXT NOT NULL,
            desired_vid   INT NOT NULL,
            occupants     JSONB NOT NULL,
            generation    BIGINT NOT NULL,
            wrote_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (switch, port)
        )
    """)

    # Grants
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON devices TO flax_discover")
    op.execute("GRANT SELECT ON devices TO flax_classify, flax_observe, flax_reconcile, flax_control")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON switch_facts TO flax_switch_sense")
    op.execute("GRANT SELECT ON switch_facts TO flax_discover, flax_classify, flax_reconcile, flax_observe, flax_control")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON desired_port TO flax_classify")
    op.execute("GRANT SELECT ON desired_port TO flax_reconcile, flax_control")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS desired_port")
    op.execute("DROP TABLE IF EXISTS switch_facts")
    op.execute("DROP TABLE IF EXISTS devices")
