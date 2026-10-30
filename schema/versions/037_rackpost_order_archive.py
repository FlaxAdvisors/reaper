"""rackpost: the order archive (schema rackpost, four tables), written on Close.

When an order rack closes, rackpost (flax_rack) sends one archive payload to
flax-control's POST /api/v1/order-archive; flax_control/order_archive.py
replaces that (order_id, rack)'s rows in one transaction. Postgres keeps what a
later "which order shipped this serial / MAC" question needs: per device its
final verdict, per MAC its role, per part its serial, per rung its verdict and
artifact PATHS (the logs stay in the order dir; spec §8.2).

  order_device     one row per geometry device, JBOD IOM and switch
  order_mac        (pos, role, mac); role host|bmc|iom1|iom2|fabric_a|fabric_b|switch
  order_component  Done's component rows (kind, model, serial, slot)
  order_rung       the latest run per (pos, stage, rung)

Single writer: flax_control (SELECT, INSERT, DELETE; no UPDATE, a re-archive
replaces). Readers (post fleet viewer) use the postgres superuser, so no other
role is granted anything. See flax-storage-delta.md §13.

Revision ID: 037000000001
Revises: 036000000001
"""
from alembic import op

revision = "037000000001"
down_revision = "036000000001"
branch_labels = None
depends_on = None

TABLES = ("order_device", "order_mac", "order_component", "order_rung")


def upgrade() -> None:
    op.execute("CREATE SCHEMA rackpost")
    op.execute("""
        CREATE TABLE rackpost.order_device (
            order_id        text NOT NULL,
            rack            text NOT NULL DEFAULT '',
            customer        text NOT NULL,
            site            text,
            pos             text NOT NULL,
            ou              int,
            kind            text,
            part            text,
            hostname        text,
            chassis_serial  text,
            final_verdict   text,
            ha_mode         text,
            closed_at       timestamptz NOT NULL,
            order_dir       text,
            PRIMARY KEY (order_id, rack, pos)
        )""")
    op.execute("CREATE INDEX order_device_customer_serial "
               "ON rackpost.order_device (customer, chassis_serial)")
    op.execute("""
        CREATE TABLE rackpost.order_mac (
            order_id  text NOT NULL,
            rack      text NOT NULL DEFAULT '',
            pos       text NOT NULL,
            role      text NOT NULL,
            mac       macaddr NOT NULL
        )""")
    op.execute("CREATE INDEX order_mac_order ON rackpost.order_mac (order_id, rack)")
    op.execute("CREATE INDEX order_mac_mac ON rackpost.order_mac (mac)")
    op.execute("""
        CREATE TABLE rackpost.order_component (
            order_id  text NOT NULL,
            rack      text NOT NULL DEFAULT '',
            pos       text NOT NULL,
            kind      text NOT NULL,
            model     text,
            serial    text,
            slot      text
        )""")
    op.execute("CREATE INDEX order_component_order ON rackpost.order_component (order_id, rack)")
    op.execute("CREATE INDEX order_component_serial ON rackpost.order_component (serial)")
    op.execute("""
        CREATE TABLE rackpost.order_rung (
            order_id     text NOT NULL,
            rack         text NOT NULL DEFAULT '',
            pos          text NOT NULL,
            stage        text NOT NULL,
            rung         text NOT NULL,
            verdict      text,
            finished_at  timestamptz,
            artifacts    text
        )""")
    op.execute("CREATE INDEX order_rung_order ON rackpost.order_rung (order_id, rack)")
    op.execute("GRANT USAGE ON SCHEMA rackpost TO flax_control")
    for t in TABLES:
        op.execute(f"GRANT SELECT, INSERT, DELETE ON rackpost.{t} TO flax_control")


def downgrade() -> None:
    op.execute("DROP SCHEMA rackpost CASCADE")
