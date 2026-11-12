"""Attach notify_change() to observe_state so flax-classify wakes on changes.

Task 8 (flax_classify/listen.py) does LISTEN observe_state, but no trigger
ever fired on that channel -- flax-observe wrote to observe_state every
cycle and the listener silently never woke. Without this, flax-classify
falls back to its 30s periodic cycle (Task 10), defeating the
LISTEN/debounce architecture.

This migration:
1. Extends notify_change() with an observe_state branch (composite key
   switch:port, mirroring the desired_port shape from 005).
2. Attaches the trigger to observe_state, mirroring switch_facts's
   attachment in migration 004.

CREATE OR REPLACE rewrites the whole function, so the upgrade body must
re-state every prior branch (devices, switch_facts, desired_port) plus
the new observe_state branch. The downgrade reinstates the exact 005-era
body and drops the trigger.

Revision ID: 008000000001
Revises: 007000000001
"""
from alembic import op

revision = "008000000001"
down_revision = "007000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION notify_change() RETURNS trigger AS $$
        DECLARE
            row_key TEXT;
        BEGIN
            IF TG_TABLE_NAME = 'devices' THEN
                row_key := NEW.mac;
            ELSIF TG_TABLE_NAME = 'switch_facts' THEN
                row_key := NEW.switch;
            ELSIF TG_TABLE_NAME = 'desired_port' THEN
                row_key := NEW.switch || ':' || NEW.port;
            ELSIF TG_TABLE_NAME = 'observe_state' THEN
                row_key := NEW.switch || ':' || NEW.port;
            ELSE
                row_key := '';
            END IF;
            PERFORM pg_notify(TG_TABLE_NAME, NEW.generation::text || ':' || row_key);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute(
        "CREATE TRIGGER observe_state_notify AFTER INSERT OR UPDATE ON observe_state "
        "FOR EACH ROW EXECUTE FUNCTION notify_change()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS observe_state_notify ON observe_state")
    # Revert notify_change() to the 005-era body (no observe_state branch).
    op.execute("""
        CREATE OR REPLACE FUNCTION notify_change() RETURNS trigger AS $$
        DECLARE
            row_key TEXT;
        BEGIN
            IF TG_TABLE_NAME = 'devices' THEN
                row_key := NEW.mac;
            ELSIF TG_TABLE_NAME = 'switch_facts' THEN
                row_key := NEW.switch;
            ELSIF TG_TABLE_NAME = 'desired_port' THEN
                row_key := NEW.switch || ':' || NEW.port;
            ELSE
                row_key := '';
            END IF;
            PERFORM pg_notify(TG_TABLE_NAME, NEW.generation::text || ':' || row_key);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
