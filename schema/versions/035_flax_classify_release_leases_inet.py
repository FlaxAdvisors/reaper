"""Compare kea.lease6.address as inet in flax_classify_release_leases.

023 predates Kea schema 22, which made kea.lease6.address inet. Its
`address = ANY(p_v6_addrs)` (text[]) raises `operator does not exist:
inet = text` on every call that carries a v6 address, and because the
function is one statement the error also rolls back the lease4 DELETE that
already ran. So every classify sweep (post and triage) removed reservations
but left their leases, and a dead lease held a slot's reserved IP against the
incoming BMC (ALLOC_ENGINE_V4_DISCOVER_ADDRESS_CONFLICT) until it expired.

Same signature, so the GRANT to flax_classify and both callers in
flax_classify/kea_hosts.py are untouched; CREATE OR REPLACE keeps the owner,
SECURITY DEFINER and existing privileges. The callers' v6 addresses come from
host(ipv6_reservations.address), so the ::inet[] cast cannot fail on real data.

Revision ID: 035000000001
Revises: 034000000001
"""
from alembic import op

revision = "035000000001"
down_revision = "034000000001"
branch_labels = None
depends_on = None

_FN = """
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
            DELETE FROM kea.lease6 WHERE address = ANY({v6});
        END IF;
    END;
    $func$;
"""


def upgrade() -> None:
    op.execute(_FN.format(v6="p_v6_addrs::inet[]"))


def downgrade() -> None:
    # 023's body verbatim -- broken on Kea schema >= 22, restored for symmetry.
    op.execute(_FN.format(v6="p_v6_addrs"))
