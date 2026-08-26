"""Add customer to post_settings (operator context, paired with order_no) and
post_node (durable stamp for later DB lookup). Idempotent.

Revision ID: 028000000001
Revises: 027000000001
"""
from alembic import op

revision = "028000000001"
down_revision = "027000000001"
branch_labels = None
depends_on = None

_STATEMENTS = (
    "ALTER TABLE post_settings ADD COLUMN IF NOT EXISTS customer TEXT",
    "ALTER TABLE post_node ADD COLUMN IF NOT EXISTS customer TEXT",
)


def upgrade() -> None:
    for stmt in _STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("ALTER TABLE post_settings DROP COLUMN IF EXISTS customer")
    op.execute("ALTER TABLE post_node DROP COLUMN IF EXISTS customer")
