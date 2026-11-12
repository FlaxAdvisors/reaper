"""rackpost: the order's sales order number (rackpost.order_device.sales).

geometry.json may carry "sales" (the business system's sales order, e.g. BC
"S-ORD101031") beside the rackpost order id. Close archives it on every device row
of the order (like customer), so the archive can be searched by either number.
A label, never a key: (order_id, rack, pos) stays the primary key.

Same table, same single writer (flax_control) and grants: no grant change.
See flax-storage-delta.md §13.

Revision ID: 039000000001
Revises: 038000000001
"""
from alembic import op

revision = "039000000001"
down_revision = "038000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE rackpost.order_device ADD COLUMN sales text")
    op.execute("CREATE INDEX order_device_sales ON rackpost.order_device (sales)")


def downgrade() -> None:
    op.execute("DROP INDEX rackpost.order_device_sales")
    op.execute("ALTER TABLE rackpost.order_device DROP COLUMN sales")
