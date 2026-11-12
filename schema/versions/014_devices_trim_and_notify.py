"""Trim devices to the thin-discover shape, add a guarded NOTIFY trigger,
and grant flax_discover read on observe_state.

WHY TRIM (polled + classification_source):
    Migration 002 created `devices` with `polled JSONB` and
    `classification_source TEXT` for an abandoned "fat discover" design where
    discover would carry polled state and own classification. Nothing ever
    wrote those columns. In the shipped architecture flax-discover is thin
    (mac/switch/port/kind/latched/generation only), flax-observe owns all
    polled/observed state (observe_state), and flax-classify owns
    classification. Drop the dead columns so the table reflects reality.

WHY THE TRIGGER IS GUARDED:
    Migration 008 added a `devices` branch to the generic notify_change()
    function, but no trigger was ever attached to the devices table, so a
    device write produced no NOTIFY on channel `devices`. flax-classify will
    `LISTEN devices` (added in a later step) to wake when a device's
    family/vm_n changes; this migration provides the trigger that channel
    needs. We attach a NEW dedicated trigger (`devices_notify`) here.

    It must be guarded: flax-discover refreshes `last_seen` (and bumps
    `generation`) on *every* poll cycle for every still-present device. An
    unguarded AFTER UPDATE trigger would pg_notify on every cycle and (once
    the consumer exists) wake flax-classify continuously, defeating the
    LISTEN/debounce design. The guard returns early (no notify) when an
    UPDATE changes only last_seen / generation -- i.e. when latched, switch,
    port and kind are all unchanged. INSERTs and any classification-relevant
    UPDATE (latched, location or kind) still fire.

    A plain LANGUAGE plpgsql function is sufficient (no SECURITY DEFINER):
    flax-discover owns its writes to public.devices, so the trigger runs in a
    context that can already pg_notify -- unlike the cross-schema lease_events
    case in migration 012.

OBSERVE_STATE GRANT GAP:
    flax-discover needs to read observe_state but migration 003 (and later)
    never granted it. Add GRANT SELECT ON observe_state TO flax_discover.

Idempotent (DROP ... IF EXISTS / CREATE OR REPLACE / ADD/DROP COLUMN IF
[NOT] EXISTS) so re-running is safe.

Revision ID: 014000000001
Revises: 013000000001
"""
from alembic import op

revision = "014000000001"
down_revision = "013000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Trim the dead fat-discover columns.
    op.execute("ALTER TABLE devices DROP COLUMN IF EXISTS polled")
    op.execute("ALTER TABLE devices DROP COLUMN IF EXISTS classification_source")

    # 2. Dedicated, guarded NOTIFY trigger function for devices.
    op.execute("""
        CREATE OR REPLACE FUNCTION public.devices_notify()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            -- Guard intentionally ignores generation/last_seen (per-cycle
            -- churn) but DOES fire on latched/switch/port/kind changes: an
            -- UPDATE that touches only last_seen/generation must NOT wake
            -- flax-classify, while any classification-relevant change does.
            IF TG_OP = 'UPDATE'
               AND NEW.latched IS NOT DISTINCT FROM OLD.latched
               AND NEW.switch  IS NOT DISTINCT FROM OLD.switch
               AND NEW.port    IS NOT DISTINCT FROM OLD.port
               AND NEW.kind    IS NOT DISTINCT FROM OLD.kind THEN
                RETURN NULL;
            END IF;
            PERFORM pg_notify('devices', NEW.generation::text || ':' || NEW.mac);
            RETURN NULL;
        END;
        $$
    """)
    op.execute("DROP TRIGGER IF EXISTS devices_notify ON devices")
    op.execute("""
        CREATE TRIGGER devices_notify
        AFTER INSERT OR UPDATE ON devices
        FOR EACH ROW EXECUTE FUNCTION public.devices_notify()
    """)

    # 3. Grant gap: flax-discover reads observe_state (never granted in 003).
    op.execute("GRANT SELECT ON observe_state TO flax_discover")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON observe_state FROM flax_discover")
    op.execute("DROP TRIGGER IF EXISTS devices_notify ON devices")
    op.execute("DROP FUNCTION IF EXISTS public.devices_notify()")
    op.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS classification_source TEXT")
    op.execute(
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS "
        "polled JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
