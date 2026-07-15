"""Per-MAC flap-state table for the reconcile convergence circuit-breaker.

Tracks how many times a MAC has transitioned (flapped) within a rolling
window so the reconciler can back off and eventually fault a MAC that
oscillates persistently.  Separate from reconcile_requests: requests are
ephemeral work items; flap_state is a long-lived counter record updated in
place each time the circuit-breaker evaluates a MAC.

Revision ID: 017000000001
Revises: 016000000001
"""
from alembic import op

revision = "017000000001"
down_revision = "016000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE reconcile_flap_state (
            mac             TEXT PRIMARY KEY,
            last_flap_at    TIMESTAMPTZ,
            flaps_in_window INT NOT NULL DEFAULT 0,
            window_start    TIMESTAMPTZ,
            backoff_until   TIMESTAMPTZ,
            faulted         BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON reconcile_flap_state TO flax_reconcile"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS reconcile_flap_state")
