"""Grant flax_classify USAGE on kea's hosts_host_id_seq.

The INSERT into kea.hosts via the kea_hosts.py upsert relies on the
host_id SERIAL default, which requires USAGE on its sequence. Migration
009 granted table-level INSERT but missed the sequence — discovered at
Plan 5 deploy time when flax-classify hit
`InsufficientPrivilege: permission denied for sequence hosts_host_id_seq`.

Pre-condition: kea-admin db-init pgsql has run (the sequence exists).

Revision ID: 010000000001
Revises: 009000000001
"""
from alembic import op

revision = "010000000001"
down_revision = "009000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT USAGE ON SEQUENCE kea.hosts_host_id_seq TO flax_classify")


def downgrade() -> None:
    op.execute("REVOKE USAGE ON SEQUENCE kea.hosts_host_id_seq FROM flax_classify")
