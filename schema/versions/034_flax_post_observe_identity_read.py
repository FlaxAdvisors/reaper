"""Narrowed observe identity view, granted to flax_post.

flax_post/observe/ipmi.py must confirm that the BMC reservation it is about to
attribute a probe answer to is really the blade on that port. A departed blade
keeps its kea reservation but loses its lease, so the `reservation_ip` fallback
can reach whatever the SUCCESSOR now answers on; pairing that answer with the
slot's current host reservation files the successor's identity under the
departed blade. flax-observe is the witness that breaks the tie.

Post is granted the VIEW, never `observe_state` itself. The view is the
published contract, so flax-observe stays free to restructure `resolved` behind
it and post never learns observe's row shape. Views are security-definer by
default here (no SECURITY INVOKER), so SELECT on the view does not require
SELECT on the base table. Migration 026 (flax_post -> switch_facts) is the
shape precedent; the narrowing is the part that is new.

`role_source` records how the BMC ROLE was confirmed and says NOTHING about how
`nic_mac` was obtained: flax_observe/role_confirm.py falls back to a heuristic
NIC (flax_switch_sense/classify.py computes bmc_mac - 2) whenever no MAC on the
port answered a host login, which is the normal case for a powered-off post
blade. Do not read it as "this NIC was observed".

Idempotent (CREATE OR REPLACE / GRANT are repeatable).

Revision ID: 034000000001
Revises: 033000000001
"""
from alembic import op

revision = "034000000001"
down_revision = "033000000001"
branch_labels = None
depends_on = None

_VIEW = """
CREATE OR REPLACE VIEW observe_identity AS
SELECT switch,
       port,
       resolved->>'bmc_mac'    AS bmc_mac,
       resolved->>'nic_mac'    AS nic_mac,
       resolved->>'chassis_sn' AS chassis_sn,
       resolved->>'source'     AS role_source,
       last_polled
FROM observe_state
"""


def upgrade() -> None:
    op.execute(_VIEW)
    op.execute("GRANT SELECT ON observe_identity TO flax_post")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON observe_identity FROM flax_post")
    op.execute("DROP VIEW IF EXISTS observe_identity")
