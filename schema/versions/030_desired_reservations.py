"""Desired reservations tables (shadow materialization phase 2).

Revision ID: 030000000001
Revises: 029000000001
"""
from alembic import op

revision = "030000000001"
down_revision = "029000000001"
branch_labels = None
depends_on = None

_READERS = ("flax_control", "flax_reconcile", "flax_observe",
            "flax_switch_sense", "flax_discover", "flax_post")


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS desired_reservations (
            mac text PRIMARY KEY,
            owner_role text NOT NULL,
            kind text NOT NULL CHECK (kind IN ('bmc','host','vm')),
            hostname text,
            ipv4 text,
            ipv6 text,
            vid integer,
            switch text,
            port text,
            attrs jsonb NOT NULL DEFAULT '{}',
            generation bigint NOT NULL DEFAULT 1,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS mac_ownership_events (
            id bigserial PRIMARY KEY,
            at timestamptz NOT NULL DEFAULT now(),
            mac text NOT NULL,
            from_role text,
            to_role text NOT NULL,
            switch text,
            port text
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS materializer_plan (
            id bigserial PRIMARY KEY,
            ts timestamptz NOT NULL DEFAULT now(),
            owner_role text NOT NULL,
            action text NOT NULL CHECK (action IN ('upsert','delete','purge_handoff','summary')),
            mac text NOT NULL,
            detail jsonb NOT NULL DEFAULT '{}'
        )
    """)
    for tbl in ("desired_reservations", "mac_ownership_events", "materializer_plan"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tbl} TO flax_classify")
        for svc in _READERS:
            op.execute(f"GRANT SELECT ON {tbl} TO {svc}")

    # bigserial PK sequences exist only after the CREATE TABLEs above; a
    # table-level GRANT does not imply sequence USAGE, so flax_classify's
    # INSERTs into these two tables would fail with InsufficientPrivilege
    # without this (same repeat-of-the-incident shape as migration 010's
    # kea.hosts.host_id sequence miss -- see that migration's docstring).
    op.execute(
        "GRANT USAGE, SELECT ON SEQUENCE "
        "mac_ownership_events_id_seq, materializer_plan_id_seq TO flax_classify"
    )


def downgrade():
    op.execute("DROP TABLE IF EXISTS materializer_plan")
    op.execute("DROP TABLE IF EXISTS mac_ownership_events")
    op.execute("DROP TABLE IF EXISTS desired_reservations")
