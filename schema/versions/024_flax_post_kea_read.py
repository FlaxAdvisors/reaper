"""Create the flax_post role and grant it read access to Kea host/lease tables.

flax-post (Eindhoven Post Servers viewer) queries kea.hosts (source='post'
reservations) joined to kea.lease4 directly, with its own read-only login role.
Idempotent: CREATE ROLE guarded, grants are repeatable.

Revision ID: 024000000001
Revises: 023000000001
"""
from alembic import op

revision = "024000000001"
down_revision = "023000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'flax_post') THEN "
        "    CREATE ROLE flax_post LOGIN; "
        "  END IF; "
        "END $$;"
    )
    op.execute("GRANT USAGE ON SCHEMA kea TO flax_post")
    op.execute("GRANT SELECT ON kea.hosts, kea.lease4 TO flax_post")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON kea.hosts, kea.lease4 FROM flax_post")
    op.execute("REVOKE USAGE ON SCHEMA kea FROM flax_post")
