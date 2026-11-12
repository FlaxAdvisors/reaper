"""End-to-end integration test for the flax-discover pipeline.

Proves the full chain observe_state -> flax-discover -> devices ->
flax-classify -> desired_reservations -> materializer -> kea.hosts, plus the
VM path. (Since the post-3b demolition the classify cycle writes only
desired_reservations; the materializer is the sole triage kea.hosts writer,
so the chain runs both, with the triage role enforced as a live bang does.)

DB-backed: mirrors the repo skip convention from test_migration_014_devices.py
(try to open a connection from FLAX_TEST_DSN or the libpq PG* env vars, and
pytest.skip if no Postgres is reachable). These tests require the flax DB
migrated to head (incl. mig 014) AND the kea schema (kea.hosts). Without a DB
at all they SKIP; with a DB present but a missing schema/grant they ERROR
(that's a setup problem worth surfacing). The real run happens at deploy.
"""
import os

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.types.json import Jsonb  # noqa: E402

# Test fixtures -- scoped so cleanup never touches real data.
TEST_SWITCH = "rabbit-x"
TEST_PORT = "et6b1"
BMC_MAC = "aa:bb:cc:00:00:64"
NIC_MAC = "aa:bb:cc:00:00:62"
VM_MAC = "02:00:00:00:00:01"
# Supersede test: a NEW chassis arriving at the same (switch, port).
NEW_BMC_MAC = "aa:bb:cc:00:00:c8"
NEW_NIC_MAC = "aa:bb:cc:00:00:c6"
TEST_MACS = (BMC_MAC, NIC_MAC, VM_MAC, NEW_BMC_MAC, NEW_NIC_MAC)


def _dsn() -> str:
    explicit = os.environ.get("FLAX_TEST_DSN")
    if explicit:
        return explicit
    return " ".join([
        f"host={os.environ.get('PGHOST', '127.0.0.1')}",
        f"port={os.environ.get('PGPORT', '5432')}",
        f"user={os.environ.get('PGUSER', 'postgres')}",
        f"password={os.environ.get('PGPASSWORD', '')}",
        f"dbname={os.environ.get('PGDATABASE', 'flax')}",
    ])


def _mac_hex(mac: str) -> str:
    return mac.replace(":", "").replace("-", "").replace(".", "").lower()


def _cleanup(conn) -> None:
    """Remove only the rows this test owns: the test switch's observe_state +
    switch_facts, the test MACs in devices, and their kea.hosts entries."""
    conn.execute(
        "DELETE FROM observe_state WHERE switch = %s", (TEST_SWITCH,))
    conn.execute(
        "DELETE FROM switch_facts WHERE switch = %s", (TEST_SWITCH,))
    conn.execute(
        "DELETE FROM devices WHERE mac = ANY(%s)", (list(TEST_MACS),))
    # kea.hosts keys the MAC as the BYTEA dhcp_identifier (type 0 = hwaddr).
    hexes = [_mac_hex(m) for m in TEST_MACS]
    conn.execute(
        "DELETE FROM kea.hosts WHERE dhcp_identifier_type = 0 "
        "AND dhcp_identifier = ANY(%s::bytea[])",
        ([f"\\x{h}" for h in hexes],))
    conn.execute(
        "DELETE FROM desired_reservations WHERE mac = ANY(%s)",
        (list(TEST_MACS),))


@pytest.fixture()
def conn():
    try:
        c = psycopg.connect(_dsn(), connect_timeout=3, autocommit=True)
    except Exception as exc:  # pragma: no cover - depends on env
        pytest.skip(f"no DB available: {exc}")
    try:
        _cleanup(c)  # best-effort cleanup of stale rows from a prior crash
        yield c
    finally:
        try:
            _cleanup(c)
        except Exception:
            pass
        c.close()


def _seed_observe(conn, switch, port, resolved):
    conn.execute(
        "INSERT INTO observe_state "
        "(switch, port, vars, last_polled, generation, resolved) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (switch, port) DO UPDATE SET resolved = EXCLUDED.resolved",
        (switch, port, Jsonb({}), "2026-06-15T00:00:00Z", 1, Jsonb(resolved)),
    )


def _seed_switch_facts(conn, switch, ports):
    conn.execute(
        "INSERT INTO switch_facts "
        "(switch, driver, polled_at, reachable, generation, port_mask, ports) "
        "VALUES (%s, %s, NOW(), %s, %s, %s, %s) "
        "ON CONFLICT (switch) DO UPDATE SET ports = EXCLUDED.ports",
        (switch, "eos", True, 1, Jsonb({}), Jsonb(ports)),
    )


def _classify_and_materialize(tmp_path):
    """One flax-classify cycle, then one materializer pass applying the
    triage lane -- the two steps that together take devices to kea.hosts.

    The test port is declared a triage port through the geometry tokens
    (as __main__ loads them from geometry.json): with no geometry, phase_for
    resolves every port to post and the triage lane rightly writes nothing."""
    from flax_classify.db import build_pool as classify_pool
    from flax_classify.cycle import run_one_cycle as classify_cycle
    from flax_classify.materializer import run_cycle as materialize
    pool = classify_pool(_dsn())
    classify_cycle(pool, geom_tokens={TEST_SWITCH: {"et6/1"}},
                   dns_hosts_path=str(tmp_path / "flax-triage-devices"))
    materialize(pool, {"triage"}, enforced_roles=frozenset({"triage"}))


def test_known_family_end_to_end(conn, tmp_path):
    from flax_discover.db import build_pool
    from flax_discover.cycle import Discoverer

    (tmp_path / "leopard.txt").write_text("Leopard-DDR4\n")

    _seed_switch_facts(
        conn, TEST_SWITCH,
        {"Ethernet6/1": {"access_vid": 16, "junk_macs": []}})
    _seed_observe(
        conn, TEST_SWITCH, TEST_PORT,
        {"bmc_mac": BMC_MAC, "nic_mac": NIC_MAC,
         "chassis_sn": "SN1", "product_name": "Leopard-DDR4"})

    disco = Discoverer(family_map_dir=str(tmp_path), base_secs=30, max_secs=3600)
    disco.run_one_cycle(build_pool(_dsn()), now=0.0)

    fam = conn.execute(
        "SELECT latched->>'family' FROM devices WHERE mac = %s", (BMC_MAC,)
    ).fetchone()[0]
    assert fam == "leopard"

    _classify_and_materialize(tmp_path)

    names = {r[0] for r in conn.execute(
        "SELECT hostname FROM kea.hosts WHERE hostname LIKE 'leopard6b1%'"
    ).fetchall()}
    assert "leopard6b1" in names
    assert "leopard6b1-bmc" in names


def test_vm_end_to_end(conn, tmp_path):
    from flax_discover.db import build_pool
    from flax_discover.cycle import Discoverer

    (tmp_path / "leopard.txt").write_text("Leopard-DDR4\n")

    _seed_switch_facts(
        conn, TEST_SWITCH,
        {"Ethernet6/1": {
            "access_vid": 16,
            "junk_macs": [{"mac": VM_MAC, "reason": "x"}]}})
    _seed_observe(
        conn, TEST_SWITCH, TEST_PORT,
        {"bmc_mac": BMC_MAC, "product_name": "Leopard-DDR4"})

    Discoverer(family_map_dir=str(tmp_path), base_secs=30,
               max_secs=3600).run_one_cycle(build_pool(_dsn()), now=0.0)
    _classify_and_materialize(tmp_path)

    row = conn.execute(
        "SELECT hostname, '0.0.0.0'::inet + ipv4_address FROM kea.hosts "
        "WHERE hostname = 'leopard6b1vm1'"
    ).fetchone()
    assert row is not None
    assert str(row[1]) == "172.16.6.201"


def _run_discover(tmp_path):
    from flax_discover.db import build_pool
    from flax_discover.cycle import Discoverer
    (tmp_path / "leopard.txt").write_text("Leopard-DDR4\n")
    return Discoverer(family_map_dir=str(tmp_path), base_secs=30,
                      max_secs=3600).run_one_cycle(build_pool(_dsn()), now=0.0)


def _macs_at_port(conn):
    return {r[0] for r in conn.execute(
        "SELECT mac FROM devices WHERE switch = %s AND port = %s",
        (TEST_SWITCH, TEST_PORT)).fetchall()}


def test_bmc_swap_supersedes_old_chassis(conn, tmp_path):
    """A different BMC arriving at (switch, port) evicts the prior
    occupant's bmc + host + vm rows, keeping only the new chassis."""
    # Cycle 1: enroll the original chassis (bmc + host + vm).
    _seed_switch_facts(
        conn, TEST_SWITCH,
        {"Ethernet6/1": {"access_vid": 16,
                         "junk_macs": [{"mac": VM_MAC, "reason": "x"}]}})
    _seed_observe(
        conn, TEST_SWITCH, TEST_PORT,
        {"bmc_mac": BMC_MAC, "nic_mac": NIC_MAC,
         "product_name": "Leopard-DDR4"})
    _run_discover(tmp_path)
    assert _macs_at_port(conn) == {BMC_MAC, NIC_MAC, VM_MAC}

    # Cycle 2: a DIFFERENT bmc/nic appears, old VM gone from the FDB.
    _seed_switch_facts(
        conn, TEST_SWITCH,
        {"Ethernet6/1": {"access_vid": 16, "junk_macs": []}})
    _seed_observe(
        conn, TEST_SWITCH, TEST_PORT,
        {"bmc_mac": NEW_BMC_MAC, "nic_mac": NEW_NIC_MAC,
         "product_name": "Leopard-DDR4"})
    summary = _run_discover(tmp_path)

    # Old bmc + old host + old vm are gone; only the new chassis remains.
    assert _macs_at_port(conn) == {NEW_BMC_MAC, NEW_NIC_MAC}
    assert summary["superseded"] >= 1


def test_same_bmc_rerun_is_idempotent(conn, tmp_path):
    """Re-running with the SAME bmc deletes nothing (no transient wipe)."""
    _seed_switch_facts(
        conn, TEST_SWITCH,
        {"Ethernet6/1": {"access_vid": 16,
                         "junk_macs": [{"mac": VM_MAC, "reason": "x"}]}})
    _seed_observe(
        conn, TEST_SWITCH, TEST_PORT,
        {"bmc_mac": BMC_MAC, "nic_mac": NIC_MAC,
         "product_name": "Leopard-DDR4"})
    _run_discover(tmp_path)
    before = _macs_at_port(conn)
    assert before == {BMC_MAC, NIC_MAC, VM_MAC}

    summary = _run_discover(tmp_path)  # identical inputs
    assert _macs_at_port(conn) == before
    assert summary["superseded"] == 0


def test_first_enrollment_supersedes_nothing(conn, tmp_path):
    """No existing bmc at the port -> first enrollment evicts nothing."""
    _seed_switch_facts(
        conn, TEST_SWITCH,
        {"Ethernet6/1": {"access_vid": 16, "junk_macs": []}})
    _seed_observe(
        conn, TEST_SWITCH, TEST_PORT,
        {"bmc_mac": BMC_MAC, "nic_mac": NIC_MAC,
         "product_name": "Leopard-DDR4"})
    summary = _run_discover(tmp_path)
    assert _macs_at_port(conn) == {BMC_MAC, NIC_MAC}
    assert summary["superseded"] == 0


def test_sweep_vacant_port_deletes_when_all_rows_older_than_debounce(conn):
    from flax_discover.db import build_pool
    from flax_discover.persistence import sweep_vacant_port
    conn.execute("INSERT INTO devices (mac, switch, port, kind, latched, last_seen) "
                 "VALUES (%s,%s,%s,'bmc','{}'::jsonb, now() - interval '1 hour')",
                 (BMC_MAC, TEST_SWITCH, TEST_PORT))
    conn.execute("INSERT INTO devices (mac, switch, port, kind, latched, last_seen) "
                 "VALUES (%s,%s,%s,'host','{}'::jsonb, now() - interval '1 hour')",
                 (NIC_MAC, TEST_SWITCH, TEST_PORT))
    pool = build_pool(_dsn())
    try:
        deleted = sweep_vacant_port(pool, switch=TEST_SWITCH, port=TEST_PORT,
                                    debounce_secs=600)
    finally:
        pool.close()
    assert deleted == 2
    cnt = conn.execute("SELECT count(*) FROM devices WHERE switch=%s AND port=%s",
                       (TEST_SWITCH, TEST_PORT)).fetchone()[0]
    assert cnt == 0


def test_sweep_vacant_port_keeps_port_with_one_recent_row(conn):
    from flax_discover.db import build_pool
    from flax_discover.persistence import sweep_vacant_port
    # one old, one fresh -> max(last_seen) is fresh -> NOT past debounce -> keep both.
    conn.execute("INSERT INTO devices (mac, switch, port, kind, latched, last_seen) "
                 "VALUES (%s,%s,%s,'bmc','{}'::jsonb, now() - interval '1 hour')",
                 (BMC_MAC, TEST_SWITCH, TEST_PORT))
    conn.execute("INSERT INTO devices (mac, switch, port, kind, latched, last_seen) "
                 "VALUES (%s,%s,%s,'host','{}'::jsonb, now())",
                 (NIC_MAC, TEST_SWITCH, TEST_PORT))
    pool = build_pool(_dsn())
    try:
        deleted = sweep_vacant_port(pool, switch=TEST_SWITCH, port=TEST_PORT,
                                    debounce_secs=600)
    finally:
        pool.close()
    assert deleted == 0
    cnt = conn.execute("SELECT count(*) FROM devices WHERE switch=%s AND port=%s",
                       (TEST_SWITCH, TEST_PORT)).fetchone()[0]
    assert cnt == 2
