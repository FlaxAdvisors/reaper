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
    deleted_empty bigint := 0;
    deleted_duts bigint := 0;
BEGIN
    -- NULL is not a safe default for any of these: LIMIT NULL means NO
    -- LIMIT, so a NULL max_deletes would delete every victim uncapped, and a
    -- NULL dry_run would be caught by "IF NOT dry_run" below evaluating to
    -- NULL (falsy) -- silently *skipping* the delete rather than raising --
    -- which is worse than surprising, it is silently wrong. Reject all three
    -- explicitly rather than let a mistyped operator call fall through.
    IF keep IS NULL OR max_deletes IS NULL OR dry_run IS NULL THEN
        RAISE EXCEPTION 'keep, dry_run and max_deletes must not be NULL (got %, %, %)',
            keep, dry_run, max_deletes;
    END IF;

    IF keep < 1 OR max_deletes < 1 THEN
        RAISE EXCEPTION 'keep and max_deletes must be >= 1 (got %, %)', keep, max_deletes;
    END IF;

    CREATE TEMP TABLE _retain_victims (id bigint PRIMARY KEY, at timestamptz, empty boolean)
        ON COMMIT DROP;

    -- Superseded empty-serial DUTs: the same NIC has a real-serial pairing
    -- whose newest record is newer. Every record of such a DUT is a victim
    -- (spec D4) -- its identity is unknown, so its history cannot be trusted
    -- to any assembly.
    CREATE TEMP TABLE _retain_superseded (dut_id bigint PRIMARY KEY) ON COMMIT DROP;
    INSERT INTO _retain_superseded (dut_id)
    SELECT e.dut_id
      FROM dut e
      JOIN LATERAL (SELECT max(at) AS newest FROM work_records w WHERE w.dut_id = e.dut_id) en
        ON true
     WHERE e.serial = ''
       AND en.newest IS NOT NULL
       AND EXISTS (
            SELECT 1 FROM dut r
             JOIN LATERAL (SELECT max(at) AS newest FROM work_records w WHERE w.dut_id = r.dut_id) rn
               ON true
             WHERE r.p0_mac = e.p0_mac AND r.serial <> ''
               AND rn.newest IS NOT NULL AND rn.newest > en.newest);

    -- `empty` here (true) is what makes the RETURN's `empty_serial_records`
    -- count ONLY these superseded-DUT victims. An ordinary empty-serial DUT
    -- that is simply outside its keep-5 window (D3: no birth pin, not yet
    -- superseded) is inserted with empty=false below and so is reported
    -- under `records`, not `empty_serial_records` -- do not read
    -- `empty_serial_records` as "all records ever deleted from an
    -- empty-serial DUT".
    INSERT INTO _retain_victims (id, at, empty)
    SELECT w.id, w.at, true
      FROM work_records w JOIN _retain_superseded s ON s.dut_id = w.dut_id;

    -- Ordinary victims: outside the newest `keep` of their (dut_id, kind,
    -- keys) group, and not the birth record of their (dut_id, kind). An
    -- empty-serial DUT gets no birth pin (spec D3). NOTE: `empty` is
    -- hardcoded false on the INSERT below regardless of the `ranked.empty`
    -- flag used in the WHERE clause -- these are ordinary trims, always
    -- counted under `records`, never under `empty_serial_records` (see the
    -- comment above the superseded-DUT insert).
    WITH ranked AS (
        SELECT w.id, w.at, d.serial = '' AS empty,
               row_number() OVER (PARTITION BY w.dut_id, w.kind, w.keys
                                  ORDER BY w.at DESC, w.id DESC) AS rn_new,
               row_number() OVER (PARTITION BY w.dut_id, w.kind
                                  ORDER BY w.at ASC, w.id ASC) AS rn_old
          FROM work_records w
          JOIN dut d ON d.dut_id = w.dut_id
         WHERE w.dut_id NOT IN (SELECT dut_id FROM _retain_superseded)
    )
    INSERT INTO _retain_victims (id, at, empty)
    SELECT id, at, false FROM ranked
     WHERE rn_new > keep AND (empty OR rn_old > 1);

    SELECT greatest(count(*) - max_deletes, 0) INTO over_cap FROM _retain_victims;

    IF NOT dry_run THEN
        WITH capped AS (
            SELECT id, empty FROM _retain_victims ORDER BY at ASC, id ASC LIMIT max_deletes
        ), gone AS (
            DELETE FROM work_records w USING capped c WHERE w.id = c.id RETURNING c.empty
        )
        SELECT count(*) FILTER (WHERE NOT empty), count(*) FILTER (WHERE empty)
          INTO deleted_records, deleted_empty FROM gone;

        -- DUT rows left with no records. A superseded empty-serial pairing
        -- goes with its records (spec D4: "and the row with them"); the 24h
        -- age gate is only for a pairing a writer minted seconds ago and has
        -- not appended to yet (spec D5), which a superseded row is not.
        WITH gone AS (
            DELETE FROM dut d
             WHERE NOT EXISTS (SELECT 1 FROM work_records w WHERE w.dut_id = d.dut_id)
               AND (d.dut_id IN (SELECT dut_id FROM _retain_superseded)
                    OR d.first_seen < now() - interval '24 hours')
            RETURNING 1
        )
        SELECT count(*) INTO deleted_duts FROM gone;
    ELSE
        WITH capped AS (
            SELECT empty FROM _retain_victims ORDER BY at ASC, id ASC LIMIT max_deletes
        )
        SELECT count(*) FILTER (WHERE NOT empty), count(*) FILTER (WHERE empty)
          INTO deleted_records, deleted_empty FROM capped;
        -- What WOULD be record-less after this run. A superseded pairing counts
        -- only when the cap covers ALL of its records -- the wet branch deletes
        -- its row only if nothing is left behind, and dry must say the same
        -- (spec §4: a dry run's counts are exactly what a real run deletes).
        WITH capped AS (
            SELECT id FROM _retain_victims ORDER BY at ASC, id ASC LIMIT max_deletes
        )
        SELECT count(*) INTO deleted_duts
          FROM dut d
         WHERE (d.dut_id IN (SELECT dut_id FROM _retain_superseded)
                AND NOT EXISTS (SELECT 1 FROM work_records w
                                  LEFT JOIN capped c ON c.id = w.id
                                 WHERE w.dut_id = d.dut_id AND c.id IS NULL))
            OR (d.first_seen < now() - interval '24 hours'
                AND NOT EXISTS (SELECT 1 FROM work_records w WHERE w.dut_id = d.dut_id));
    END IF;

    -- 'empty_serial_records' counts only records of a SUPERSEDED empty-serial
    -- DUT (spec D4); ordinary keep-5 trimming of a not-yet-superseded
    -- empty-serial DUT (spec D3) is counted under 'records' instead.
    RETURN QUERY VALUES ('records', deleted_records),
                        ('empty_serial_records', deleted_empty),
                        ('dut_rows', deleted_duts),
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
