"""Grant flax_post SELECT on switch_facts (Post UI consumes link/MAC for rabbit-edam).

The Post UI's consume layer reads switch_facts (writer: flax-switch-sense) for
rabbit-edam link state + MACs. The flax_post role had kea read (024) and post_*
write (025) but was never granted switch_facts read, so GET /api/v1/blades
raised InsufficientPrivilege. Idempotent (GRANT/REVOKE are repeatable).

Revision ID: 026000000001
Revises: 025000000001
"""
from alembic import op

revision = "026000000001"
down_revision = "025000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON switch_facts TO flax_post")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON switch_facts FROM flax_post")
