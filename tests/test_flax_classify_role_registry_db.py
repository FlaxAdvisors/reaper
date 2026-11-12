"""publish_roles: full-replace roles/role_universe in one transaction,
idempotent on unchanged definitions.

DB-backed: uses the same self-skip convention as
tests/test_flax_classify_desired_port.py (try FLAX_TEST_DSN or libpq env
vars, pytest.skip if no Postgres is reachable). _dsn()/_build_pool copied
from that file with credit. Requires the flax DB migrated to head (roles/
role_universe tables from migration 029, flax_classify role having
SELECT/INSERT/UPDATE/DELETE on them).

These tests SKIP locally (no DB); they run at deploy on the bang host.
"""
import json
import os

import pytest

psycopg = pytest.importorskip("psycopg")

from flax_classify.role_registry import load_role_dir

# Reuse Task 2's fixture builder (tests/test_flax_classify_role_registry.py):
# a flat-list-of-{"port","switch"} geometry fixture (matches the real
# geometry.json shape consumed by load_phase_geometry) and the
# triage/post roles.d pair.
from test_flax_classify_role_registry import GEOM, _write_integra, _write_roles


# ── fixtures ────────────────────────────────────────────────────────────────

def _dsn() -> str:
    explicit = os.environ.get("FLAX_TEST_DSN")
    if explicit:
        return explicit
    return " ".join([
        "host=" + os.environ.get("PGHOST", "127.0.0.1"),
        "port=" + os.environ.get("PGPORT", "5432"),
        "user=" + os.environ.get("PGUSER", "postgres"),
        "password=" + os.environ.get("PGPASSWORD", ""),
        "dbname=" + os.environ.get("PGDATABASE", "flax"),
    ])


def _cleanup(conn) -> None:
    conn.execute("DELETE FROM role_universe")
    conn.execute("DELETE FROM roles")


def _build_pool(dsn: str):
    from flax_classify.db import build_pool
    return build_pool(dsn)


@pytest.fixture()
def pool():
    """Scoped pool + cleanup of both registry tables; SKIP if no DB."""
    try:
        c = psycopg.connect(_dsn(), connect_timeout=2, autocommit=True)
    except Exception as exc:
        pytest.skip("no DB available: " + str(exc))
    try:
        _cleanup(c)
    finally:
        c.close()

    p = _build_pool(_dsn())
    try:
        yield p
    finally:
        try:
            with p.connection() as conn:
                _cleanup(conn)
        except Exception:
            pass
        p.close()


@pytest.fixture
def roles_dir(tmp_path):
    geom = tmp_path / "geometry.json"
    geom.write_text(json.dumps(GEOM))
    return _write_roles(tmp_path, geom)


# ── tests ───────────────────────────────────────────────────────────────────

def test_publish_roles_roundtrip_and_idempotent(pool, roles_dir):
    from flax_classify.role_registry import publish_roles

    defs = load_role_dir(str(roles_dir))
    gen1 = publish_roles(pool, defs)
    gen2 = publish_roles(pool, defs)  # unchanged -> same generation
    assert gen2 == gen1

    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT role, generation FROM roles ORDER BY role").fetchall()
        assert [r[0] for r in rows] == ["post", "triage"]
        assert all(r[1] == gen1 for r in rows)

        kinds = {r[0] for r in
                 conn.execute("SELECT kind FROM role_universe").fetchall()}
        assert {"switch", "prefix", "port", "catch_all"} <= kinds

    defs["post"]["policy"]["vid"]["default"] = 25  # changed -> bumps
    assert publish_roles(pool, defs) == gen1 + 1


# --- phase 6: integra registry publish (scratch db) -------------------------

def test_publish_integra_roundtrip_and_port_claim(pool, roles_dir):
    """Publish the 3-role set (triage+post+integra) and prove integra lands
    in the registry exactly as spec'd: a roles row whose definition JSON
    round-trips every capability/policy/record_key/universe field, a queryable
    'port' claim row for (<scratch switch>, et32/1), and a generation bump.

    Uses a SCRATCH switch name ("hare-1", never the real eindhoven uplink
    switch) so this DB-writing file names no live switch -- see
    test_no_real_switch_names_in_db_fixtures for the live-wipe rationale. The
    real eindhoven value is covered by the (guard-exempt, non-DB) template and
    registry tests; the publish machinery is switch-agnostic.
    """
    from flax_classify.role_registry import publish_roles

    scratch_switch = "hare-1"

    # Baseline 2-role publish from the clean scratch DB (generation 1).
    two_role = load_role_dir(str(roles_dir))
    gen2 = publish_roles(pool, two_role)

    # Add integra (Task 2's DRY helper) on a scratch switch; publish 3-role set.
    _write_integra(roles_dir, switch=scratch_switch)
    three_role = load_role_dir(str(roles_dir))
    gen3 = publish_roles(pool, three_role)
    assert gen3 == gen2 + 1  # content changed -> generation bumps

    with pool.connection() as conn:
        roles = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT role, definition, generation FROM roles").fetchall()}
        assert set(roles) == {"triage", "post", "integra"}
        assert all(g == gen3 for _, g in roles.values())

        # definition (jsonb -> dict) round-trips the spec fields verbatim.
        definition = roles["integra"][0]
        assert definition["capabilities"] == {
            "reconcile_switch": False, "dhcp_reservations": False,
            "firmware": False}
        assert definition["policy"]["vid"]["default"] == 31
        assert definition["record_keys"] == ["customer", "order"]
        assert definition["universe"]["ports"] == {scratch_switch: ["et32/1"]}

        # the port claim is queryable the way the app reads role_universe:
        # a normalized ('port', switch, token) row attributed to integra.
        claim = conn.execute(
            "SELECT role FROM role_universe "
            "WHERE kind='port' AND switch=%s AND port=%s",
            (scratch_switch, "et32/1")).fetchone()
        assert claim is not None and claim[0] == "integra"


def test_post_profile_declares_observe_capability(pool, roles_dir):
    """post.json publishes capabilities.observe=true so flax_observe enrolls
    its universe.switches. Opt-in: integra/triage must NOT carry it."""
    from flax_classify.role_registry import publish_roles
    publish_roles(pool, load_role_dir(str(roles_dir)))
    with pool.connection() as conn:
        defs = {r[0]: r[1] for r in conn.execute(
            "SELECT role, definition FROM roles").fetchall()}
    assert defs["post"]["capabilities"]["observe"] is True
    assert not defs["triage"]["capabilities"].get("observe")


def test_publish_roles_republishes_when_only_ports_from_geometry_changes(pool, roles_dir, tmp_path):
    """roles.d unchanged, geometry.json (ports_from) grown by one port: the
    stored definitions compare equal, but role_universe must still be rewritten
    (2026-09-11: a 24-port triage universe survived two geometry deploys)."""
    import json
    from flax_classify.role_registry import load_role_dir, publish_roles
    geo = tmp_path / "geometry.json"
    geo.write_text(json.dumps([{"port": "et6b1", "ou": "12L", "switch": "rabbit-test-rr"}]))
    (roles_dir / "triage.json").write_text(json.dumps({
        "role": "triage", "universe": {"ports_from": str(geo)},
        "capabilities": {}, "policy": {"vid": {"default": 16}}}))
    gen1 = publish_roles(pool, load_role_dir(str(roles_dir)))
    geo.write_text(json.dumps([{"port": "et6b1", "ou": "12L", "switch": "rabbit-test-rr"},
                               {"port": "et26b2", "ou": "33R", "switch": "rabbit-test-rr"}]))
    defs2 = load_role_dir(str(roles_dir))
    gen2 = publish_roles(pool, defs2)
    assert gen2 == gen1 + 1
    with pool.connection() as conn:
        rows = {(r[0], r[1]) for r in conn.execute(
            "SELECT switch, port FROM role_universe WHERE role='triage' AND kind='port'").fetchall()}
    assert ("rabbit-test-rr", "et26/2") in rows
    assert publish_roles(pool, defs2) == gen2          # and idempotent again
