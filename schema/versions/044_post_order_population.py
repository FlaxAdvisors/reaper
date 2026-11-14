"""post: post_order.population -- an order's population lives on its own record.

Order labels (sales, customer, population) are a property of the ORDER, edited
by an explicit Set / Save labels and read by the tile, drawer, IPMI record
stamps and Finish (spec: reaper-devel docs/superpowers/specs/
2026-11-09-post-order-labels-set-and-override-styling-design.md).

Backfill: the two orders the old per-field header saves mislabelled get the
population they were built with, only where none is set yet. Additive; no
grant change (post_order is already flax_post's).

Revision ID: 044000000001
Revises: 043000000001
"""
from alembic import op

revision = "044000000001"
down_revision = "043000000001"
branch_labels = None
depends_on = None

# Re-runnable; the migration test re-runs it.
BACKFILL = (
    "UPDATE post_order SET population = 'barivia-leo' "
    "WHERE order_no = 'RRORD-1033' AND population IS NULL",
    "UPDATE post_order SET population = 'ovh-bare-tp' "
    "WHERE order_no = 'RRORD-100' AND population IS NULL",
)


def upgrade() -> None:
    op.execute("ALTER TABLE post_order ADD COLUMN IF NOT EXISTS population text")
    for sql in BACKFILL:
        op.execute(sql)


def downgrade() -> None:
    op.execute("ALTER TABLE post_order DROP COLUMN IF EXISTS population")
