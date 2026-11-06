"""rackpost: which tool closed an archived order (rackpost.order_device.closed_by).

The order archive (037-040) now also holds orders closed from the Post UI's
"Finish order" (spec 2026-11-06-post-per-node-order-and-finish). A post Finish and
a rackpost Close of the same order id + rack would otherwise replace each other;
flax-control refuses a replace that crosses tools (409). Existing rows are
rackpost's. Same table, same single writer (flax_control): no grant change.
See flax-storage-delta.md §13.

Revision ID: 041000000001
Revises: 040000000001
"""
from alembic import op

revision = "041000000001"
down_revision = "040000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE rackpost.order_device ADD COLUMN closed_by text NOT NULL "
               "DEFAULT 'rackpost' CHECK (closed_by IN ('rackpost', 'post'))")


def downgrade() -> None:
    op.execute("ALTER TABLE rackpost.order_device DROP COLUMN closed_by")
