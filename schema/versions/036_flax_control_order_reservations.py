"""flax-control: INSERT/UPDATE on kea.hosts for order:* reservations (rackpost).

rackpost (customer-rack orders) reserves BMC/host/IOM addresses through
flax-control's /api/v1/order-reservations routes rather than through the
materializer (spec 2026-10-29 section 6). flax_control/order_reservations.py
fences every write to rows whose user_context.source is `order:<ORDER>[/<rack>]`
and refuses a MAC or IP that any other source holds; the materializer attributes
such a source as NOT OURS and never plans against it.

Grants: table-wide INSERT, UPDATE (the 009 column grant UPDATE(user_context)
stays as it was) and the host_id sequence for the INSERT's default. DELETE is
migration 022's and is left alone in both directions. See flax-storage-delta.md.

Revision ID: 036000000001
Revises: 035000000001
"""
from alembic import op

revision = "036000000001"
down_revision = "035000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT INSERT, UPDATE ON kea.hosts TO flax_control")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE kea.hosts_host_id_seq TO flax_control")


def downgrade() -> None:
    op.execute("REVOKE USAGE, SELECT ON SEQUENCE kea.hosts_host_id_seq FROM flax_control")
    op.execute("REVOKE INSERT, UPDATE ON kea.hosts FROM flax_control")
    # A table-wide REVOKE UPDATE also drops the column-level UPDATE(user_context)
    # migration 009 granted (operator notes, aliases); put it back.
    op.execute("GRANT UPDATE (user_context) ON kea.hosts TO flax_control")
