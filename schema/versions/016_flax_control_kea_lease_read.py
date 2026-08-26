"""Grant flax_control SELECT on kea.lease4/lease6 for the /leases dashboard page.

flax_control already holds USAGE on schema kea + SELECT on kea.hosts (for the
reservations view), but the new /leases admin page queries kea.lease4/lease6
directly (active leases + per-subnet counts) and got "permission denied for
table lease4". Mirror migrations 013/015 (flax_observe/flax_reconcile kea reads).
Idempotent grants.

Revision ID: 016000000001
Revises: 015000000001
"""
from alembic import op

revision = "016000000001"
down_revision = "015000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON kea.lease4, kea.lease6 TO flax_control")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON kea.lease4, kea.lease6 FROM flax_control")
