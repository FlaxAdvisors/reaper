"""notify_change: dispatch row_key by TG_TABLE_NAME

Plan 1's notify_change() unconditionally accessed NEW.mac. That works for the
devices table (mac is PK) but PL/pgSQL evaluates the COALESCE eagerly and
errors out on switch_facts and desired_port (no mac column). Bug surfaced
when flax-switch-sense in Plan 2 became the first writer of switch_facts.

Fix: switch on TG_TABLE_NAME so each table builds its row_key from its own
primary-key columns.

Revision ID: 005000000001
Revises: 004000000001
"""
from alembic import op

revision = "005000000001"
down_revision = "004000000001"
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
            ELSE
                row_key := '';
            END IF;
            PERFORM pg_notify(TG_TABLE_NAME, NEW.generation::text || ':' || row_key);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)


def downgrade() -> None:
    # Restore Plan 1's broken version. Practical effect: triggers fail on
    # switch_facts + desired_port writes again. We don't expect to downgrade
    # past this; defining for completeness.
    op.execute("""
        CREATE OR REPLACE FUNCTION notify_change() RETURNS trigger AS $$
        DECLARE
            row_key TEXT;
        BEGIN
            row_key := COALESCE(
                NEW.mac,
                NEW.switch || COALESCE(':' || NEW.port, ''),
                ''
            );
            PERFORM pg_notify(TG_TABLE_NAME, NEW.generation::text || ':' || row_key);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
