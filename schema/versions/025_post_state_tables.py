# schema/versions/025_post_state_tables.py
"""Two-tier post store: live post_state (by port) + durable post_node (by identity) + post_settings.

Backs the reworked Post UI (docs/Post-UI-Design.md §3.3) on the HA cluster,
replacing the JSON file store. Single DB role flax_post (from 024) gets write;
engine-vs-viewer single-writer is by convention. Idempotent.

Revision ID: 025000000001
Revises: 024000000001
"""
from alembic import op

revision = "025000000001"
down_revision = "024000000001"
branch_labels = None
depends_on = None

_TABLES = ("post_state", "post_node", "post_settings")


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS post_state ("
        " port TEXT PRIMARY KEY,"
        " switch TEXT NOT NULL DEFAULT 'rabbit-edam',"
        " bmc_mac TEXT, serial TEXT, order_no TEXT,"
        " vars JSONB NOT NULL DEFAULT '{}'::jsonb,"
        " generation BIGINT NOT NULL DEFAULT 1,"
        " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS post_node ("
        " bmc_mac TEXT PRIMARY KEY,"
        " serial TEXT, host_mac TEXT, order_no TEXT,"
        " last_switch TEXT, last_port TEXT,"
        " vars JSONB NOT NULL DEFAULT '{}'::jsonb,"
        " generation BIGINT NOT NULL DEFAULT 1,"
        " first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS post_settings ("
        " id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),"
        " order_no TEXT, population TEXT,"
        " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
    )
    op.execute("INSERT INTO post_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
    for tbl in _TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tbl} TO flax_post")


def downgrade() -> None:
    for tbl in _TABLES:
        op.execute(f"DROP TABLE IF EXISTS {tbl}")
