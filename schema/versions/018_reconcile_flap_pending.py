"""Per-flap in-flight marker for interrupted-flap self-heal.

flap() is shutdown -> sleep -> no-shutdown in two separate eAPI/ssh batches. If
the process dies between them (SIGKILL on `systemctl stop`, crash, restart) the
port is stranded admin-down and the DUT goes unreachable. A row is written here
BEFORE the shutdown and DELETEd after the no-shutdown completes; any row still
present at the next startup (or at SIGTERM with a flap in-flight) marks a port
reconcile was mid-flapping, so the self-heal pass can precisely re-issue the
no-shutdown.

Why a dedicated table rather than reusing intentional_flap: intentional_flap is
the flax-observe freeze signal (migration 004 NOTIFY) keyed on (switch, port)
and is intentionally left in place for observe to expire -- it is NOT a
pending-completion flag. A separate marker keeps the freeze contract intact and
lets recovery key on (switch, port) with the mac for the audit row.

The persisted switch_facts.ports[*].link collapses admin-down ('disabled') into
'nolink' alongside a genuinely absent device ('notconnect'), so a facts-only
scan cannot tell a reconcile-stranded port from an empty one. This marker is the
precise signal; the admin-down scan is only a best-effort fallback.

Revision ID: 018000000001
Revises: 017000000001
"""
from alembic import op

revision = "018000000001"
down_revision = "017000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE reconcile_flap_pending (
            switch    TEXT NOT NULL,
            port      TEXT NOT NULL,
            mac       TEXT,
            set_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (switch, port)
        )
    """)
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON reconcile_flap_pending "
        "TO flax_reconcile"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS reconcile_flap_pending")
