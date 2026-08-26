"""schemas and per-service roles

Revision ID: 001000000001
Revises:
Create Date: 2026-05-22
"""
from alembic import op

revision = "001000000001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS audit")
    op.execute("CREATE SCHEMA IF NOT EXISTS kea")

    # Per-service login roles. Passwords supplied at deploy time via
    # ALTER ROLE statements run from the ansible role (not in this migration --
    # passwords don't belong in source control).
    for role in (
        "flax_switch_sense",
        "flax_discover",
        "flax_classify",
        "flax_reconcile",
        "flax_observe",
        "flax_control",
        "flax_kea_hook",
        "kea",
    ):
        op.execute(
            f"DO $$ BEGIN "
            f"  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
            f"    CREATE ROLE {role} LOGIN; "
            f"  END IF; "
            f"END $$;"
        )

    # Read-only role used by flax-control to render UI pages.
    op.execute("GRANT USAGE ON SCHEMA public, audit, kea TO flax_control")
    op.execute("GRANT USAGE ON SCHEMA audit TO "
               "flax_switch_sense, flax_discover, flax_classify, "
               "flax_reconcile, flax_observe")


def downgrade() -> None:
    # Reverse-order drops; reassign owned objects to postgres first if needed.
    for role in (
        "flax_switch_sense", "flax_discover", "flax_classify",
        "flax_reconcile", "flax_observe", "flax_control",
        "flax_kea_hook", "kea",
    ):
        op.execute(f"DROP ROLE IF EXISTS {role}")
    op.execute("DROP SCHEMA IF EXISTS kea CASCADE")
    op.execute("DROP SCHEMA IF EXISTS audit CASCADE")
