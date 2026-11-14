"""post: post_order.population -- an order's population lives on its own record.

Order labels (sales, customer, population) are a property of the ORDER, edited
by an explicit Set / Save labels and read by the tile, drawer, IPMI record
stamps and Finish (spec: reaper-devel docs/superpowers/specs/
2026-11-09-post-order-labels-set-and-override-styling-design.md).

Backfill, derived from the data, never per-site literals (the same order
number is different hardware at different sites: RRORD-1033 is barivia-leo
at eindhoven and tec6-spare-tp at lavergne):
  1. an order whose assigned nodes all carry ONE override population gets it;
  2. the header order still without one gets post_settings.population;
  3. post_settings' labels then mirror the header order's record (they are
     kept in step from now on; any earlier drift would otherwise be written
     back onto the record by the first Set).
Additive; no grant change (post_order is already flax_post's).

Revision ID: 044000000001
Revises: 043000000001
"""
from alembic import op

revision = "044000000001"
down_revision = "043000000001"
branch_labels = None
depends_on = None

# Re-runnable; the migration test re-runs it.
BACKFILL = (
    "UPDATE post_order o SET population = n.p FROM ("
    " SELECT assigned_order_no AS o, min(assigned_population) AS p FROM post_node"
    " WHERE assigned_order_no IS NOT NULL AND assigned_population IS NOT NULL"
    " GROUP BY assigned_order_no HAVING count(DISTINCT assigned_population) = 1) n"
    " WHERE o.order_no = n.o AND o.population IS NULL",
    "UPDATE post_order o SET population = s.population FROM post_settings s"
    " WHERE s.id = 1 AND o.order_no = s.order_no AND o.population IS NULL"
    " AND s.population IS NOT NULL",
    "UPDATE post_settings s SET sales = o.sales, customer = o.customer, population = o.population"
    " FROM post_order o WHERE s.id = 1 AND o.order_no = s.order_no",
)


def upgrade() -> None:
    op.execute("ALTER TABLE post_order ADD COLUMN IF NOT EXISTS population text")
    for sql in BACKFILL:
        op.execute(sql)


def downgrade() -> None:
    op.execute("ALTER TABLE post_order DROP COLUMN IF EXISTS population")
