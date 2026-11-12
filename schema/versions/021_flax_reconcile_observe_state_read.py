"""Grant flax_reconcile SELECT on observe_state for the install-aware kick.

flax_reconcile gates the no-lease kick on install-state (db.read_installing_ports
reads observe_state for hosts mid-PXE-install: nodepxe=found, inventory!=found)
so a mid-install host's port is not flapped (which would interrupt the install
and never converge -> flap-storm). flax_reconcile was never granted any access
to observe_state (migration 003 granted only flax_observe/flax_control; 007/014
added flax_classify/flax_discover) -- without this grant the new read crashes
with permission-denied. SELECT-only mirrors the read-side grants for the other
consumers. Idempotent: GRANT/REVOKE of an existing/absent privilege is a no-op.

Revision ID: 021000000001
Revises: 020000000001
"""
from alembic import op

revision = "021000000001"
down_revision = "020000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON observe_state TO flax_reconcile")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON observe_state FROM flax_reconcile")
