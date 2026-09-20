"""Rolling retention for the work-record store: one SECURITY DEFINER policy
function, no DELETE grant to any role.

Spec: docs/superpowers/specs/2026-09-19-work-records-rolling-retention-design.md.
Migration 031 anticipated this ("Retention, if ever needed, becomes one
designated sweeper's DELETE grant in a later migration"); the sweeper is
narrowed from a DELETE grant to this function, so the policy is the only
deletion path and ad-hoc deletes stay grant-blocked. Deviation from
docs/flax-storage-impl.md's append-only statement is recorded in
docs/flax-storage-delta.md.

Keeps, per (dut_id, kind, keys), the newest `keep` records, plus the earliest
record of each (dut_id, kind) as the birth record. Empty-serial DUT rules and
the zero-record DUT sweep are added in the same function by task 2.

Revision ID: 033000000001
Revises: 032000000001
"""
from alembic import op

revision = "033000000001"
down_revision = "032000000001"
branch_labels = None
depends_on = None

_FUNCTION = """
CREATE OR REPLACE FUNCTION work_records_retain(
        keep int DEFAULT 5,
        dry_run boolean DEFAULT true,
        max_deletes int DEFAULT 20000)
    RETURNS TABLE(action text, n bigint)
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = public, pg_temp
AS $fn$
DECLARE
    over_cap bigint := 0;
    deleted_records bigint := 0;
BEGIN
    IF keep < 1 OR max_deletes < 1 THEN
        RAISE EXCEPTION 'keep and max_deletes must be >= 1 (got %, %)', keep, max_deletes;
    END IF;

    CREATE TEMP TABLE _retain_victims (id bigint PRIMARY KEY, at timestamptz) ON COMMIT DROP;

    -- Victims: outside the newest `keep` of their (dut_id, kind, keys) group
    -- AND not the earliest record of their (dut_id, kind) (the birth record).
    WITH ranked AS (
        SELECT w.id, w.at,
               row_number() OVER (PARTITION BY w.dut_id, w.kind, w.keys
                                  ORDER BY w.at DESC, w.id DESC) AS rn_new,
               row_number() OVER (PARTITION BY w.dut_id, w.kind
                                  ORDER BY w.at ASC, w.id ASC) AS rn_old
          FROM work_records w
    )
    INSERT INTO _retain_victims (id, at)
    SELECT id, at FROM ranked WHERE rn_new > keep AND rn_old > 1;

    SELECT greatest(count(*) - max_deletes, 0) INTO over_cap FROM _retain_victims;

    IF NOT dry_run THEN
        WITH capped AS (
            SELECT id FROM _retain_victims ORDER BY at ASC, id ASC LIMIT max_deletes
        ), gone AS (
            DELETE FROM work_records w USING capped c WHERE w.id = c.id RETURNING 1
        )
        SELECT count(*) INTO deleted_records FROM gone;
    ELSE
        SELECT least(count(*), max_deletes) INTO deleted_records FROM _retain_victims;
    END IF;

    RETURN QUERY VALUES ('records', deleted_records),
                        ('empty_serial_records', 0::bigint),
                        ('dut_rows', 0::bigint),
                        ('capped_remaining', over_cap);
END;
$fn$;
"""

_GRANTS = (
    "REVOKE ALL ON FUNCTION work_records_retain(int, boolean, int) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION work_records_retain(int, boolean, int) TO flax_post",
)


def upgrade() -> None:
    op.execute(_FUNCTION)
    for stmt in _GRANTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS work_records_retain(int, boolean, int)")
