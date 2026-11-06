"""post: per-node order assignment, per-run results, the open/finished order
registry, and the narrowed view flax_classify reads for the hostname prefix.

- post_settings.sales: the header's sales order number (a label, never a key).
- post_node.assigned_*: a tile's own order/sales/customer/population; applies
  only while assigned_serial equals the node's vouched serial (a moved mezz card
  carries the BMC MAC to another blade).
- post_run: every finished run's full result, keyed (bmc_mac, run_id), so Finish
  can archive a node's latest run under an order even after a later run.
- post_order: an order is OPEN iff its row has finished_at NULL.
- post_node_assignment (view): (bmc_mac, order_no) for serial-matched
  assignments only; the ONLY thing flax_classify may read (cross-lane, see
  flax-storage-delta.md §14). No grant on post_node itself.

Revision ID: 042000000001
Revises: 041000000001
"""
from alembic import op

revision = "042000000001"
down_revision = "041000000001"
branch_labels = None
depends_on = None

# Re-runnable (ON CONFLICT DO NOTHING); the migration test re-runs it.
BACKFILL_RUNS = """
INSERT INTO post_run (bmc_mac, run_id, serial, order_no, verdict, finished_at, result)
SELECT n.bmc_mac, n.vars->'result'->>'run_id',
       COALESCE(n.vars->'result'->>'serial', n.serial),
       n.vars->'result'->>'order_no', n.vars->'result'->>'verdict',
       to_timestamp((n.vars->'result'->>'finished_at')::bigint),
       n.vars->'result'
FROM post_node n
WHERE n.vars->'result'->>'run_id' IS NOT NULL
  AND n.vars->'result'->>'finished_at' ~ '^[0-9]+$'
ON CONFLICT (bmc_mac, run_id) DO NOTHING
"""

SEED_ORDERS = """
INSERT INTO post_order (order_no)
SELECT DISTINCT o FROM (
  SELECT order_no AS o FROM post_settings
  UNION SELECT order_no FROM post_state
) s WHERE o IS NOT NULL AND o <> ''
ON CONFLICT (order_no) DO NOTHING
"""


def upgrade() -> None:
    op.execute("ALTER TABLE post_settings ADD COLUMN sales text")
    op.execute("ALTER TABLE post_node "
               "ADD COLUMN assigned_order_no text, ADD COLUMN assigned_sales text, "
               "ADD COLUMN assigned_customer text, ADD COLUMN assigned_population text, "
               "ADD COLUMN assigned_serial text, ADD COLUMN assigned_at timestamptz")
    op.execute("CREATE TABLE post_run ("
               " bmc_mac text NOT NULL, run_id text NOT NULL, serial text, order_no text,"
               " verdict text, finished_at timestamptz, result jsonb NOT NULL,"
               " PRIMARY KEY (bmc_mac, run_id))")
    op.execute("CREATE INDEX post_run_order_no ON post_run (order_no)")
    op.execute("CREATE INDEX post_run_serial ON post_run (serial)")
    op.execute("CREATE TABLE post_order ("
               " order_no text PRIMARY KEY, sales text, customer text,"
               " opened_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,"
               " finished_counts jsonb)")
    op.execute("CREATE VIEW post_node_assignment AS "
               "SELECT bmc_mac, assigned_order_no AS order_no FROM post_node "
               "WHERE assigned_order_no IS NOT NULL AND assigned_serial IS NOT NULL "
               "AND assigned_serial IS NOT DISTINCT FROM serial")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON post_run, post_order TO flax_post")
    op.execute("GRANT SELECT ON post_node_assignment TO flax_classify")
    op.execute(BACKFILL_RUNS)
    op.execute(SEED_ORDERS)


def downgrade() -> None:
    op.execute("DROP VIEW post_node_assignment")
    op.execute("DROP TABLE post_order")
    op.execute("DROP TABLE post_run")
    op.execute("ALTER TABLE post_node "
               "DROP COLUMN assigned_order_no, DROP COLUMN assigned_sales, "
               "DROP COLUMN assigned_customer, DROP COLUMN assigned_population, "
               "DROP COLUMN assigned_serial, DROP COLUMN assigned_at")
    op.execute("ALTER TABLE post_settings DROP COLUMN sales")
