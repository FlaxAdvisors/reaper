"""rackpost: archive each device's identity read (rackpost.order_device.identity).

Close archived a JBOD IOM's fruid-util read and a switch's syseeprom / weutil read
nowhere (order_device had no place for them), so the archive showed those devices
with no serial at all. One nullable jsonb column carries the rackpost drawer's
identity block as rendered rows: {title, serial, rows: [[label, value]], raw:
{name: artifact path}} -- artifact PATHS, never their text (spec §8.2).

Same table, same single writer (flax_control) and grants: no grant change.
See flax-storage-delta.md §13.

Revision ID: 038000000001
Revises: 037000000001
"""
from alembic import op

revision = "038000000001"
down_revision = "037000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE rackpost.order_device ADD COLUMN identity jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE rackpost.order_device DROP COLUMN identity")
