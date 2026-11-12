"""reconcile_requests work-queue + kea.hosts change trigger + flax_reconcile kea reads

intentional_flap + reconcile_actions already exist (migration 003); this only
adds the operator/auto work-queue, an edge-trigger on reservation changes, and
the Kea-schema read grants flax_reconcile needs to diff leases vs reservations.

Revision ID: 015000000001
Revises: 014000000001
"""
from alembic import op

revision = "015000000001"
down_revision = "014000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE reconcile_requests (
            id            BIGSERIAL PRIMARY KEY,
            ts            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            requested_by  TEXT NOT NULL,
            mac           TEXT NOT NULL,
            switch        TEXT,
            port          TEXT,
            kind          TEXT,
            reason        TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending','claimed','done','stuck')),
            attempts      INT NOT NULL DEFAULT 0,
            next_eligible TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            claimed_at    TIMESTAMPTZ,
            completed_at  TIMESTAMPTZ,
            outcome       TEXT
        )
    """)
    # Partial index: the worker only ever scans actionable rows.
    op.execute("CREATE INDEX reconcile_requests_pending_idx "
               "ON reconcile_requests (next_eligible) "
               "WHERE status = 'pending'")
    # One open request per mac (dedup at the DB level; the enqueue path also
    # checks, but this is the hard guarantee). Partial unique on open states.
    op.execute("CREATE UNIQUE INDEX reconcile_requests_open_mac_idx "
               "ON reconcile_requests (mac) "
               "WHERE status IN ('pending','claimed')")

    # reconcile_requests has no `generation` column, so the shared
    # notify_change() (reads NEW.generation) cannot serve it. Dedicated fn.
    op.execute("""
        CREATE FUNCTION notify_reconcile_request() RETURNS trigger AS $$
        BEGIN PERFORM pg_notify('reconcile_requests', NEW.id::text); RETURN NEW; END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("CREATE TRIGGER reconcile_requests_notify "
               "AFTER INSERT ON reconcile_requests "
               "FOR EACH ROW EXECUTE FUNCTION notify_reconcile_request()")

    # Edge-trigger reconcile when classify changes a reservation.
    op.execute("""
        CREATE FUNCTION notify_kea_hosts_change() RETURNS trigger AS $$
        BEGIN PERFORM pg_notify('kea_hosts_change', ''); RETURN NEW; END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("CREATE TRIGGER kea_hosts_change_notify "
               "AFTER INSERT OR UPDATE ON kea.hosts "
               "FOR EACH ROW EXECUTE FUNCTION notify_kea_hosts_change()")

    # Grants. devices + switch_facts SELECT already granted in 002.
    op.execute("GRANT SELECT, INSERT, UPDATE ON reconcile_requests TO flax_reconcile")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE reconcile_requests_id_seq TO flax_reconcile")
    op.execute("GRANT SELECT, INSERT ON reconcile_requests TO flax_control")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE reconcile_requests_id_seq TO flax_control")
    # Kea-schema reads (mirror 013 for flax_observe).
    op.execute("GRANT USAGE ON SCHEMA kea TO flax_reconcile")
    op.execute("GRANT SELECT ON kea.lease4, kea.lease6, kea.hosts TO flax_reconcile")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS reconcile_requests_notify ON reconcile_requests")
    op.execute("DROP TABLE IF EXISTS reconcile_requests")
    op.execute("DROP FUNCTION IF EXISTS notify_reconcile_request()")
    op.execute("DROP TRIGGER IF EXISTS kea_hosts_change_notify ON kea.hosts")
    op.execute("DROP FUNCTION IF EXISTS notify_kea_hosts_change()")
    op.execute("REVOKE SELECT ON kea.lease4, kea.lease6, kea.hosts FROM flax_reconcile")
    op.execute("REVOKE USAGE ON SCHEMA kea FROM flax_reconcile")
