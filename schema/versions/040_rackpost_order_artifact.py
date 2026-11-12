"""rackpost: the archived files themselves (rackpost.order_artifact).

Until now the archive kept artifact PATHS into the order dir, which lives on the
one bang that ran the order (the HA pair does not sync order dirs) and is not
readable by people browsing the archive. Close now sends the files: every artifact
the archived rungs and identity blocks reference, plus the order's documents
(process report, customer CSVs). Measured on RRORD-101: 458 files, 6.8 MB raw,
about 0.8 MB gzip'd. Content is stored gzip'd; Postgres replication carries it.

  status   stored (content set) | too_large (over the per-file / per-order cap,
           path only) | missing (not on disk at Close, path only)

Single writer: flax_control (SELECT, INSERT, DELETE; a re-close replaces), like the
other rackpost.* tables. Readers (post fleet viewer) use the postgres superuser.
See flax-storage-delta.md §13.

Revision ID: 040000000001
Revises: 039000000001
"""
from alembic import op

revision = "040000000001"
down_revision = "039000000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE rackpost.order_artifact (
            order_id      text NOT NULL,
            rack          text NOT NULL DEFAULT '',
            path          text NOT NULL,
            status        text NOT NULL CHECK (status IN ('stored', 'too_large', 'missing')),
            size          bigint,
            sha256        text,
            content_type  text,
            content       bytea,
            PRIMARY KEY (order_id, rack, path),
            CHECK ((status = 'stored') = (content IS NOT NULL))
        )""")
    op.execute("GRANT SELECT, INSERT, DELETE ON rackpost.order_artifact TO flax_control")


def downgrade() -> None:
    op.execute("DROP TABLE rackpost.order_artifact")
