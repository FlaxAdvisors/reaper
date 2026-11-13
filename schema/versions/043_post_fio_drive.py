"""post: every drive's fio numbers, one row per drive x job x metric (post_fio_drive).

flax-post observe writes a run's rows when it judges a terminal fio step (and a one-off
backfill reads the stored fio/alldisks artifacts). The judge reads the same table for
site-wide siblings: same model + size in other nodes, the latest run per serial. It is
also the source for the next baseline mining (spec 2026-11-04 fio-baselines, addendum
2026-11-13). Single writer: flax_post. Idempotent.

Revision ID: 043000000001
Revises: 042000000001
"""
from alembic import op

revision = "043000000001"
down_revision = "042000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS post_fio_drive ("
        " id BIGSERIAL PRIMARY KEY,"
        " bmc_mac TEXT NOT NULL,"
        " run_id TEXT NOT NULL,"
        " serial TEXT, model TEXT, size_gb DOUBLE PRECISION, controller TEXT,"
        " dev TEXT NOT NULL,"
        " job TEXT NOT NULL,"
        " metric TEXT NOT NULL,"
        " value DOUBLE PRECISION NOT NULL,"
        " captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " UNIQUE (bmc_mac, run_id, dev, job, metric))"
    )
    op.execute("CREATE INDEX IF NOT EXISTS post_fio_drive_key ON post_fio_drive (model, size_gb)")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON post_fio_drive TO flax_post")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE post_fio_drive_id_seq TO flax_post")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS post_fio_drive")
