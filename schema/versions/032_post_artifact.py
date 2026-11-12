"""Durable per-stage qualification evidence, keyed by node identity + run.

Backs the Qualify/Done wave (docs/superpowers/specs/2026-07-14-post-qualify-done-design.md
§6.2). The live post_state row keeps only refs+digests; big blobs (dmidecode,
hwinfo, full SDR/mem, full+digested SEL) land here, one row per artifact, keyed
by (bmc_mac, run_id, stage, name) so a re-qual under a new run_id never clobbers
prior evidence and a blade pull preserves history. Single-writer grant to the
engine role flax_post; the viewer reads it. Idempotent.

Revision ID: 032000000001
Revises: 031000000001
"""
from alembic import op

revision = "032000000001"
down_revision = "031000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS post_artifact ("
        " id BIGSERIAL PRIMARY KEY,"
        " bmc_mac TEXT NOT NULL,"
        " serial TEXT, order_no TEXT,"
        " run_id TEXT NOT NULL,"
        " stage TEXT NOT NULL,"
        " name TEXT NOT NULL,"
        " kind TEXT NOT NULL,"
        " content TEXT,"
        " bytes INT,"
        " captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " UNIQUE (bmc_mac, run_id, stage, name))"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON post_artifact TO flax_post")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE post_artifact_id_seq TO flax_post")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS post_artifact")
