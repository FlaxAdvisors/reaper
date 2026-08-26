"""observe_state.resolved JSONB for scalar resolved values

flax-observe's PortWorker keeps port_state with both:
  - vars[name].value  — state-machine flag ("found" / "notfound" / "unknown")
  - port_state["bmc_mac"]  — actual resolved scalar ("98:03:9b:a6:fc:24")

Only `vars` was persisted; the scalars stayed in memory. Triage UI then
showed "found" instead of the actual MAC/IP/SN. Add a `resolved` JSONB
column to carry the scalars so triage_compat can render real values.

Revision ID: 006000000001
Revises: 005000000001
"""
from alembic import op

revision = "006000000001"
down_revision = "005000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE observe_state
        ADD COLUMN IF NOT EXISTS resolved JSONB NOT NULL DEFAULT '{}'::jsonb
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE observe_state DROP COLUMN IF EXISTS resolved")
