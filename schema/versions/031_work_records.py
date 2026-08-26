"""Phase-4 work-record store: dut pairing registry + append-only work_records.

Spec: docs/superpowers/specs/2026-07-05-flax-phase4-work-records-design.md.
`dut` (NOT `device` — flax_discover's `devices` intake table already exists)
is the DUT assembly registry: one row per (p0_mac, serial) pairing, insert-
only; the UNIQUE constraint is the re-pairing detector (a card moving to a
new chassis is a new dut_id; the old assembly's records are never touched).
`work_records` is the append-only event log every role's producers write
owner-tagged rows into (post now; triage/integra later — zero schema work).

Append-only is GRANT-ENFORCED: flax_post gets INSERT+SELECT only; no role
holds UPDATE or DELETE. Retention, if ever needed, becomes one designated
sweeper's DELETE grant in a later migration.

Revision ID: 031000000001
Revises: 030000000001
"""
from alembic import op

revision = "031000000001"
down_revision = "030000000001"
branch_labels = None
depends_on = None

_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS dut (
        dut_id bigserial PRIMARY KEY,
        p0_mac text NOT NULL,
        serial text NOT NULL DEFAULT '',
        first_seen timestamptz NOT NULL DEFAULT now(),
        UNIQUE (p0_mac, serial)
    )""",
    """CREATE TABLE IF NOT EXISTS work_records (
        id bigserial PRIMARY KEY,
        dut_id bigint NOT NULL REFERENCES dut(dut_id),
        owner_role text NOT NULL,
        kind text NOT NULL,
        stage text,
        keys jsonb NOT NULL DEFAULT '{}',
        payload jsonb NOT NULL DEFAULT '{}',
        at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS work_records_dut_at_idx"
    " ON work_records (dut_id, at DESC)",
    "CREATE INDEX IF NOT EXISTS work_records_kind_at_idx"
    " ON work_records (kind, at DESC)",
    "CREATE INDEX IF NOT EXISTS work_records_owner_idx"
    " ON work_records (owner_role)",
    "CREATE INDEX IF NOT EXISTS work_records_keys_order_idx"
    " ON work_records ((keys->>'order'))",
    "CREATE INDEX IF NOT EXISTS work_records_keys_customer_idx"
    " ON work_records ((keys->>'customer'))",
    # One definition of "current assembly" recency, shared by API and psql:
    # a dut's recency is its latest record (dut itself is insert-only; no
    # last_seen column to maintain).
    """CREATE OR REPLACE VIEW dut_current AS
        SELECT d.dut_id, d.p0_mac, d.serial, d.first_seen, lr.last_record_at
        FROM dut d
        LEFT JOIN (
            SELECT dut_id, max(at) AS last_record_at
            FROM work_records GROUP BY dut_id
        ) lr ON lr.dut_id = d.dut_id""",
    # Append-only writer: INSERT+SELECT, deliberately NO UPDATE/DELETE.
    "GRANT SELECT, INSERT ON dut, work_records TO flax_post",
    "GRANT SELECT ON dut_current TO flax_post",
    # Read-only retrieval (phase-5 browser rides the same grant).
    "GRANT SELECT ON dut, work_records, dut_current TO flax_control",
    # bigserial sequences: table GRANT does not imply sequence USAGE
    # (migration 010/030 incident shape).
    "GRANT USAGE, SELECT ON SEQUENCE dut_dut_id_seq, work_records_id_seq"
    " TO flax_post",
)


def upgrade():
    for stmt in _STATEMENTS:
        op.execute(stmt)


def downgrade():
    op.execute("DROP VIEW IF EXISTS dut_current")
    op.execute("DROP TABLE IF EXISTS work_records")
    op.execute("DROP TABLE IF EXISTS dut")
