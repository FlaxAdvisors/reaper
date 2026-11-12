"""Grant flax_classify SELECT on post_settings (continuous post reservation lane).

The flax_classify post lane reads the active order_no from the post_settings
singleton to derive reservation hostnames. flax_classify already writes kea.hosts
but had no read on the post tables. Idempotent (GRANT/REVOKE repeatable).

Revision ID: 027000000001
Revises: 026000000001
"""
from alembic import op

revision = "027000000001"
down_revision = "026000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON post_settings TO flax_classify")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON post_settings FROM flax_classify")
