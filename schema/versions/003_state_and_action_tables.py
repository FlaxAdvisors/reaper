"""observe_state, intentional_flap, reconcile_actions, consumer_acks

Revision ID: 003000000001
Revises: 002000000001
"""
from alembic import op

revision = "003000000001"
down_revision = "002000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE observe_state (
            switch        TEXT NOT NULL,
            port          TEXT NOT NULL,
            vars          JSONB NOT NULL,
            last_polled   TIMESTAMPTZ NOT NULL,
            generation    BIGINT NOT NULL,
            PRIMARY KEY (switch, port)
        )
    """)

    op.execute("""
        CREATE TABLE intentional_flap (
            switch        TEXT NOT NULL,
            port          TEXT NOT NULL,
            hold_seconds  INT NOT NULL,
            reason        TEXT NOT NULL,
            mac           TEXT,
            set_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (switch, port)
        )
    """)

    op.execute("""
        CREATE TABLE reconcile_actions (
            id            BIGSERIAL PRIMARY KEY,
            ts            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            switch        TEXT NOT NULL,
            port          TEXT NOT NULL,
            action        TEXT NOT NULL,
            detail        JSONB NOT NULL,
            outcome       TEXT NOT NULL,
            reason        TEXT
        )
    """)
    op.execute("CREATE INDEX reconcile_actions_ts_idx ON reconcile_actions (ts DESC)")
    op.execute("CREATE INDEX reconcile_actions_switch_port_ts_idx ON reconcile_actions (switch, port, ts DESC)")

    op.execute("""
        CREATE TABLE consumer_acks (
            consumer      TEXT NOT NULL,
            source        TEXT NOT NULL,
            generation    BIGINT NOT NULL,
            action        TEXT NOT NULL CHECK (action IN ('applied','noop','deferred','failed','skipped')),
            consumed_at   TIMESTAMPTZ NOT NULL,
            detail        TEXT,
            PRIMARY KEY (consumer, source)
        )
    """)

    # Grants
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON observe_state TO flax_observe")
    op.execute("GRANT SELECT ON observe_state TO flax_control")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON intentional_flap TO flax_reconcile")
    op.execute("GRANT SELECT ON intentional_flap TO flax_switch_sense, flax_observe, flax_control")
    op.execute("GRANT SELECT, INSERT ON reconcile_actions TO flax_reconcile")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE reconcile_actions_id_seq TO flax_reconcile")
    op.execute("GRANT SELECT ON reconcile_actions TO flax_control")
    # Each consumer service writes its own row(s)
    op.execute("GRANT SELECT, INSERT, UPDATE ON consumer_acks TO "
               "flax_switch_sense, flax_discover, flax_classify, flax_reconcile, flax_observe")
    op.execute("GRANT SELECT ON consumer_acks TO flax_control")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS consumer_acks")
    op.execute("DROP TABLE IF EXISTS reconcile_actions")
    op.execute("DROP TABLE IF EXISTS intentional_flap")
    op.execute("DROP TABLE IF EXISTS observe_state")
