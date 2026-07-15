"""Role registry tables (spine migration phase 1).

Revision ID: 029000000001
Revises: 028000000001
"""
from alembic import op

revision = "029000000001"
down_revision = "028000000001"
branch_labels = None
depends_on = None

_READERS = ("flax_control", "flax_reconcile", "flax_observe",
            "flax_switch_sense", "flax_discover", "flax_post")


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS roles (
            role text PRIMARY KEY,
            definition jsonb NOT NULL,
            generation bigint NOT NULL,
            loaded_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS role_universe (
            role text NOT NULL REFERENCES roles(role) ON DELETE CASCADE,
            kind text NOT NULL CHECK (kind IN ('switch','prefix','port','catch_all')),
            switch text,
            port text
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS role_universe_claim_uq
        ON role_universe (role, kind, coalesce(switch,''), coalesce(port,''))
    """)
    for tbl in ("roles", "role_universe"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tbl} TO flax_classify")
        for svc in _READERS:
            op.execute(f"GRANT SELECT ON {tbl} TO {svc}")


def downgrade():
    op.execute("DROP TABLE IF EXISTS role_universe")
    op.execute("DROP TABLE IF EXISTS roles")
