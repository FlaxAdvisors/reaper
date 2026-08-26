"""lease_events, audit.events, NOTIFY triggers

Revision ID: 004000000001
Revises: 003000000001
"""
from alembic import op

revision = "004000000001"
down_revision = "003000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE lease_events (
            id            BIGSERIAL PRIMARY KEY,
            ts            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            query_type    TEXT NOT NULL,
            family        INT NOT NULL CHECK (family IN (4, 6)),
            mac           TEXT NOT NULL,
            ip            INET,
            processed     BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)
    op.execute("CREATE INDEX lease_events_unprocessed_idx ON lease_events (id) WHERE NOT processed")
    op.execute("GRANT INSERT ON lease_events TO flax_kea_hook")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE lease_events_id_seq TO flax_kea_hook")
    op.execute("GRANT SELECT, UPDATE ON lease_events TO flax_discover")
    op.execute("GRANT SELECT ON lease_events TO flax_control")

    op.execute("""
        CREATE TABLE audit.events (
            id            BIGSERIAL PRIMARY KEY,
            ts            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            service       TEXT NOT NULL,
            kind          TEXT NOT NULL,
            mac           TEXT,
            switch        TEXT,
            port          TEXT,
            payload       JSONB NOT NULL
        )
    """)
    op.execute("CREATE INDEX events_ts_idx ON audit.events (ts DESC)")
    op.execute("CREATE INDEX events_service_ts_idx ON audit.events (service, ts DESC)")
    op.execute("CREATE INDEX events_mac_ts_idx ON audit.events (mac, ts DESC) WHERE mac IS NOT NULL")
    op.execute("CREATE INDEX events_kind_ts_idx ON audit.events (kind, ts DESC)")
    op.execute("GRANT INSERT ON audit.events TO "
               "flax_switch_sense, flax_discover, flax_classify, flax_reconcile, flax_observe")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE audit.events_id_seq TO "
               "flax_switch_sense, flax_discover, flax_classify, flax_reconcile, flax_observe")
    op.execute("GRANT SELECT ON audit.events TO flax_control")

    # NOTIFY trigger function for write-target tables. Payload is generation:key.
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
    for table in ("devices", "switch_facts", "desired_port"):
        op.execute(
            f"CREATE TRIGGER {table}_notify AFTER INSERT OR UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION notify_change()"
        )

    # Separate trigger for lease_events (no generation column; payload is just mac)
    op.execute("""
        CREATE OR REPLACE FUNCTION notify_lease_event() RETURNS trigger AS $$
        BEGIN
            PERFORM pg_notify('lease_events', NEW.mac);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute(
        "CREATE TRIGGER lease_events_notify AFTER INSERT ON lease_events "
        "FOR EACH ROW EXECUTE FUNCTION notify_lease_event()"
    )

    # Separate trigger for intentional_flap so consumers see DELETEs too
    op.execute("""
        CREATE OR REPLACE FUNCTION notify_intentional_flap() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                PERFORM pg_notify('intentional_flap', 'delete:' || OLD.switch || ':' || OLD.port);
            ELSE
                PERFORM pg_notify('intentional_flap', 'set:' || NEW.switch || ':' || NEW.port);
            END IF;
            RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute(
        "CREATE TRIGGER intentional_flap_notify AFTER INSERT OR UPDATE OR DELETE ON intentional_flap "
        "FOR EACH ROW EXECUTE FUNCTION notify_intentional_flap()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS intentional_flap_notify ON intentional_flap")
    op.execute("DROP TRIGGER IF EXISTS lease_events_notify ON lease_events")
    for table in ("devices", "switch_facts", "desired_port"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_notify ON {table}")
    op.execute("DROP FUNCTION IF EXISTS notify_intentional_flap()")
    op.execute("DROP FUNCTION IF EXISTS notify_lease_event()")
    op.execute("DROP FUNCTION IF EXISTS notify_change()")
    op.execute("DROP TABLE IF EXISTS audit.events")
    op.execute("DROP TABLE IF EXISTS lease_events")
