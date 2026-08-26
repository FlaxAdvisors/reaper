"""Populate public.lease_events from Postgres triggers on kea.lease4/lease6
instead of the libdhcp_run_script shell hook.

The Plan 5 run_script hook (flax-discover-hook) ran INSIDE the minimal Kea
container, which has no psql, and depended on Kea env-var names that were
not set on the events that fired -- so it failed on every lease event
("psql: not found" / "missing LEASE4_HWADDR") and lease_events never
populated. Kea already writes every committed lease to Postgres natively,
so AFTER INSERT/UPDATE triggers on kea.lease4 / kea.lease6 generate the
lease_events rows; the existing lease_events_notify trigger then fires
pg_notify('lease_events', ...) for flax-discover's LISTEN.

  - SECURITY DEFINER: the triggers fire as the `kea` role (Kea owns its
    lease tables) but must INSERT into public.lease_events; running as the
    function owner (postgres) avoids a direct grant to kea.
  - Only state = 0 (assigned/active) leases generate events; declined (1)
    and expired-reclaimed (2) are skipped.
  - Renewals (UPDATE where the client identifier is unchanged) are skipped
    so the log carries new/changed bindings, not periodic churn.
  - v4 address is a host-order BIGINT -> inet via ('0.0.0.0'::inet + addr);
    v6 address is already inet. mac is encode(hwaddr,'hex'); v6 falls back
    to the DUID when hwaddr is absent.

Idempotent (CREATE OR REPLACE / DROP TRIGGER IF EXISTS) so it is safe to
re-run.

Revision ID: 012000000001
Revises: 011000000001
"""
from alembic import op

revision = "012000000001"
down_revision = "011000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION public.kea_lease4_to_lease_events()
        RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF NEW.state <> 0 OR NEW.hwaddr IS NULL THEN
                RETURN NULL;
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.hwaddr IS NOT DISTINCT FROM NEW.hwaddr THEN
                RETURN NULL;  -- renewal of the same device, not a new binding
            END IF;
            INSERT INTO public.lease_events (query_type, family, mac, ip)
            VALUES (lower(TG_OP), 4, encode(NEW.hwaddr, 'hex'),
                    ('0.0.0.0'::inet + NEW.address));
            RETURN NULL;
        END;
        $$
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION public.kea_lease6_to_lease_events()
        RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF NEW.state <> 0 THEN
                RETURN NULL;
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.duid IS NOT DISTINCT FROM NEW.duid THEN
                RETURN NULL;
            END IF;
            INSERT INTO public.lease_events (query_type, family, mac, ip)
            VALUES (lower(TG_OP), 6,
                    COALESCE(encode(NEW.hwaddr, 'hex'), encode(NEW.duid, 'hex')),
                    NEW.address);
            RETURN NULL;
        END;
        $$
    """)
    op.execute("DROP TRIGGER IF EXISTS lease4_to_lease_events ON kea.lease4")
    op.execute("""
        CREATE TRIGGER lease4_to_lease_events
        AFTER INSERT OR UPDATE ON kea.lease4
        FOR EACH ROW EXECUTE FUNCTION public.kea_lease4_to_lease_events()
    """)
    op.execute("DROP TRIGGER IF EXISTS lease6_to_lease_events ON kea.lease6")
    op.execute("""
        CREATE TRIGGER lease6_to_lease_events
        AFTER INSERT OR UPDATE ON kea.lease6
        FOR EACH ROW EXECUTE FUNCTION public.kea_lease6_to_lease_events()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS lease4_to_lease_events ON kea.lease4")
    op.execute("DROP TRIGGER IF EXISTS lease6_to_lease_events ON kea.lease6")
    op.execute("DROP FUNCTION IF EXISTS public.kea_lease4_to_lease_events()")
    op.execute("DROP FUNCTION IF EXISTS public.kea_lease6_to_lease_events()")
