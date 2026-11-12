"""SECURITY DEFINER fn for flax-classify bulk lease release on stale sweep.

flax_classify deletes stale kea.hosts reservations each cycle but holds no
grant on kea.lease4/lease6, and a raw lease DELETE fires Kea's SECURITY
INVOKER trigger (func_lease4_adel / lease6 equivalent) which touches the
lease stat tables -- so a direct grant would have to cascade. Mirror
migration 019: encapsulate the bulk DELETE in a SECURITY DEFINER function
owned by the migration superuser; flax_classify gets only EXECUTE. The
function is UNCONDITIONAL (unlike flax_release_stale_lease's self-guard):
the caller only passes hwaddrs / v6 addresses whose reservations it just
deleted, so the matching leases are genuinely stale and must go. SET
search_path = kea, public makes `kea` visible to the trigger's UNQUALIFIED
isJsonSupported() call.

Revision ID: 023000000001
Revises: 022000000001
"""
from alembic import op

revision = "023000000001"
down_revision = "022000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION kea.flax_classify_release_leases(
            p_hwaddrs bytea[], p_v6_addrs text[])
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = kea, public
        AS $func$
        BEGIN
            IF array_length(p_hwaddrs, 1) IS NOT NULL THEN
                DELETE FROM kea.lease4 WHERE hwaddr = ANY(p_hwaddrs);
            END IF;
            IF array_length(p_v6_addrs, 1) IS NOT NULL THEN
                DELETE FROM kea.lease6 WHERE address = ANY(p_v6_addrs);
            END IF;
        END;
        $func$;
    """)
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "kea.flax_classify_release_leases(bytea[], text[]) TO flax_classify"
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "kea.flax_classify_release_leases(bytea[], text[])"
    )
