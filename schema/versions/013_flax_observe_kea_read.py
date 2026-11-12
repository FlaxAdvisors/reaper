"""Grant flax_observe read access to Kea's lease/host tables.

flax-observe now resolves BMC/host IPs from Kea's Postgres backend
(host_probe.lookup_kea_ip: kea.lease4 live lease, then kea.hosts
reservation) instead of the retired dnsmasq.leases / dhcp-hosts files.
Without USAGE on schema kea + SELECT on those tables the lookup fails with
"permission denied for schema kea" and observe comes up blind (every
observe_state.bmc_ip blank). Idempotent grants.

Revision ID: 013000000001
Revises: 012000000001
"""
from alembic import op

revision = "013000000001"
down_revision = "012000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT USAGE ON SCHEMA kea TO flax_observe")
    op.execute("GRANT SELECT ON kea.lease4, kea.lease6, kea.hosts TO flax_observe")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON kea.lease4, kea.lease6, kea.hosts FROM flax_observe")
    op.execute("REVOKE USAGE ON SCHEMA kea FROM flax_observe")
