"""SECURITY DEFINER fn for flax-reconcile stale-lease release.

flax_reconcile holds only SELECT on kea.lease4 (not DELETE), and the raw
DELETE fired Kea's SECURITY INVOKER trigger func_lease4_adel which touches
the lease stat tables -- so granting DELETE directly would have to cascade
those grants too. Instead encapsulate the self-guarding DELETE in a
SECURITY DEFINER function owned by the migration's superuser (postgres):
the body runs with the owner's full privileges, flax_reconcile gets only
EXECUTE. SET search_path = kea, public on the function makes `kea` visible
to the trigger's UNQUALIFIED isJsonSupported() call.

The self-guard (l.address <> h.ipv4_address, joined against kea.hosts on the
mac's reservation) is preserved: a converged lease is never deleted, and a
mac with no reservation is left alone.

Revision ID: 019000000001
Revises: 018000000001
"""
from alembic import op

revision = "019000000001"
down_revision = "018000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION kea.flax_release_stale_lease(p_mac bytea)
        RETURNS SETOF text
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = kea, public
        AS $func$
            DELETE FROM kea.lease4 l
             USING kea.hosts h
             WHERE l.hwaddr = p_mac
               AND h.dhcp_identifier = p_mac
               AND h.dhcp_identifier_type = 0
               AND l.address <> h.ipv4_address
            RETURNING host(('0.0.0.0'::inet + l.address));
        $func$;
    """)
    op.execute(
        "GRANT EXECUTE ON FUNCTION kea.flax_release_stale_lease(bytea) "
        "TO flax_reconcile"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS kea.flax_release_stale_lease(bytea)")
