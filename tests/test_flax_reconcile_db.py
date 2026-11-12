"""DB read helpers for flax-reconcile, tested against a real Postgres.

Mirrors the repo skip convention from test_migration_014_devices.py and
test_flax_discover_integration.py: try to open a psycopg connection from
FLAX_TEST_DSN or the libpq PG* env vars; pytest.skip if no DB is reachable.
Requires migrations 001–014 applied (kea schema + devices + switch_facts +
desired_port tables present).  No local Postgres → these tests SKIP (expected).
"""
import os

import pytest

# ---------------------------------------------------------------------------
# PURE unit tests for the port-name canonicalizer — no DB required, run always.
# These prove the et6b1 -> Ethernet6/1 conversion that resolve_location applies
# at the devices.port ingest boundary (the DB-backed resolve_location test below
# SKIPs locally, so this is the guaranteed-local coverage of the conversion).
# ---------------------------------------------------------------------------
from flax_reconcile.portname import to_arista  # noqa: E402
from flax_reconcile.db import port_link_up, read_reservations  # noqa: E402


def test_to_arista_internal_to_canonical():
    assert to_arista("et6b1") == "Ethernet6/1"
    assert to_arista("et1b1") == "Ethernet1/1"
    assert to_arista("et48b4") == "Ethernet48/4"


def test_to_arista_idempotent_on_canonical():
    assert to_arista("Ethernet6/1") == "Ethernet6/1"


def test_to_arista_passthrough_non_arista():
    assert to_arista("swp6") == "swp6"
    assert to_arista("") == ""


# ---------------------------------------------------------------------------
# PURE unit tests for port_link_up — no DB required, run always.
# Verifies link-gate logic: only 'link' == up; everything else (nolink, unknown,
# missing key, None fact) is treated as not-up so absent devices are skipped.
# ---------------------------------------------------------------------------

def test_port_link_up_link():
    assert port_link_up({"link": "link", "mask": "access", "access_vid": 17}) is True


def test_port_link_up_nolink():
    assert port_link_up({"link": "nolink", "mask": "access", "access_vid": 17}) is False


def test_port_link_up_unknown():
    assert port_link_up({"link": "unknown"}) is False


def test_port_link_up_missing_key():
    assert port_link_up({"mask": "access"}) is False


def test_port_link_up_none_fact():
    assert port_link_up(None) is False


# ---------------------------------------------------------------------------
# read_reservations SQL-shape (Task 1: registry capability lookup) -- a fake
# pool/connection (mirrors tests/test_write_ack.py's _FakePool) records the
# executed SQL/params without needing a live DB, so these run always.
# ---------------------------------------------------------------------------
class _RRFakeCursor:
    def fetchall(self):
        return []


class _RRFakeConn:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return _RRFakeCursor()


class _RRFakeCtx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        return False


class _RRFakePool:
    def __init__(self):
        self.conn = _RRFakeConn()

    def connection(self):
        return _RRFakeCtx(self.conn)


def test_read_reservations_none_uses_legacy_literal_verbatim():
    """eligible_sources=None (default) must emit the EXACT pre-registry SQL --
    byte-identical to the string this replaced -- so the fallback path is
    provably unchanged."""
    pool = _RRFakePool()
    read_reservations(pool)
    sql, params = pool.conn.calls[0]
    assert params is None
    assert "COALESCE((user_context::jsonb) ->> 'source', '') <> 'post'" in sql
    assert "ANY(" not in sql


def test_read_reservations_frozenset_uses_any_filter():
    pool = _RRFakePool()
    sources = frozenset({"triage", "legacy-import", ""})
    read_reservations(pool, eligible_sources=sources)
    sql, params = pool.conn.calls[0]
    assert "COALESCE((user_context::jsonb) ->> 'source', '') = ANY(%(sources)s)" in sql
    assert "<> 'post'" not in sql
    assert set(params["sources"]) == sources


psycopg = pytest.importorskip("psycopg")

from flax_reconcile import db  # noqa: E402


TEST_SWITCH = "rabbit-reconcile-test"
TEST_PORT = "Ethernet99/1"
TEST_MAC = "fa:ce:00:00:db:04"
TEST_MAC2 = "fa:ce:00:00:db:05"


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
    hexes = [_mac_hex(m) for m in (TEST_MAC, TEST_MAC2)]
    conn.execute(
        "DELETE FROM kea.lease4 WHERE hwaddr = ANY(%s::bytea[])",
        ([f"\\x{h}" for h in hexes],))
    conn.execute(
        "DELETE FROM kea.hosts "
        "WHERE dhcp_identifier_type = 0 AND dhcp_identifier = ANY(%s::bytea[])",
        ([f"\\x{h}" for h in hexes],))
    conn.execute("DELETE FROM devices WHERE mac = ANY(%s)", ([TEST_MAC, TEST_MAC2],))
    conn.execute(
        "DELETE FROM switch_facts WHERE switch = %s", (TEST_SWITCH,))
    conn.execute(
        "DELETE FROM desired_port WHERE switch = %s", (TEST_SWITCH,))
    conn.execute(
        "DELETE FROM observe_state WHERE switch = %s", (TEST_SWITCH,))


@pytest.fixture()
def conn():
    try:
        c = psycopg.connect(_dsn(), connect_timeout=3, autocommit=True)
    except Exception as exc:  # pragma: no cover - depends on env
        pytest.skip(f"no DB available: {exc}")
    try:
        _cleanup(c)
        yield c
    finally:
        try:
            _cleanup(c)
        except Exception:
            pass
        c.close()


# ---------------------------------------------------------------------------
# read_active_leases + read_reservations
# ---------------------------------------------------------------------------

def test_read_active_leases_and_reservations(conn):
    """Seed one lease4 row and one hosts row; verify both are returned."""
    mac_hex = _mac_hex(TEST_MAC)
    # kea.lease4: address 172.17.0.99 = 2886795363
    conn.execute(
        "INSERT INTO kea.lease4 (address, hwaddr, state) "
        "VALUES (2886795363, decode(%s, 'hex'), 0)",
        (mac_hex,))
    # kea.hosts: reserved IP 172.17.0.5 = 2886795269
    conn.execute(
        "INSERT INTO kea.hosts (dhcp_identifier, dhcp_identifier_type, ipv4_address) "
        "VALUES (decode(%s, 'hex'), 0, 2886795269)",
        (mac_hex,))

    pool = db.build_pool(_dsn())
    try:
        leases = db.read_active_leases(pool)
        hosts = db.read_reservations(pool)
    finally:
        pool.close()

    assert {"mac": TEST_MAC, "ip": "172.17.0.99"} in leases
    assert {"mac": TEST_MAC, "ip": "172.17.0.5"} in hosts


def test_inactive_lease_is_excluded(conn):
    """A lease4 row with state != 0 must not appear in read_active_leases."""
    mac_hex = _mac_hex(TEST_MAC2)
    conn.execute(
        "INSERT INTO kea.lease4 (address, hwaddr, state) "
        "VALUES (2886729828, decode(%s, 'hex'), 1)",
        (mac_hex,))

    pool = db.build_pool(_dsn())
    try:
        leases = db.read_active_leases(pool)
    finally:
        pool.close()

    assert not any(r["mac"] == TEST_MAC2 for r in leases)


def test_read_reservations_excludes_source_post(conn):
    """A kea.hosts row marked user_context.source='post' is owned by the post
    workflow on a switch reconcile does not observe; reconcile must NOT see it
    (else its convergence flap-storms a mis-resolved port). Symmetric to
    flax-classify's stale-sweep source=post guard."""
    mac_hex = _mac_hex(TEST_MAC2)
    # 172.17.25.3 = 2886801667
    conn.execute(
        "INSERT INTO kea.hosts (dhcp_identifier, dhcp_identifier_type, "
        "ipv4_address, user_context) VALUES (decode(%s, 'hex'), 0, 2886801667, %s)",
        (mac_hex, '{"source": "post"}'))

    pool = db.build_pool(_dsn())
    try:
        hosts = db.read_reservations(pool)
    finally:
        pool.close()

    assert not any(r["mac"] == TEST_MAC2 for r in hosts)


# ---------------------------------------------------------------------------
# resolve_location
# ---------------------------------------------------------------------------

def test_resolve_location_returns_switch_port_kind(conn):
    # Seed devices.port in the REAL internal short form flax-discover writes
    # (et99b1); resolve_location must return it canonicalized to Arista
    # (Ethernet99/1 == TEST_PORT) so the kick flap + sentinel are valid.
    conn.execute(
        "INSERT INTO devices (mac, switch, port, kind, latched) "
        "VALUES (%s, %s, %s, %s, '{}'::jsonb)",
        (TEST_MAC, TEST_SWITCH, "et99b1", "bmc"))

    pool = db.build_pool(_dsn())
    try:
        loc = db.resolve_location(pool, TEST_MAC)
    finally:
        pool.close()

    assert loc == {"switch": TEST_SWITCH, "port": TEST_PORT, "kind": "bmc"}


def test_resolve_location_missing_returns_empty(conn):
    pool = db.build_pool(_dsn())
    try:
        loc = db.resolve_location(pool, "de:ad:00:00:00:00")
    finally:
        pool.close()

    assert loc == {}


# ---------------------------------------------------------------------------
# read_desired_ports
# ---------------------------------------------------------------------------

def test_read_desired_ports_returns_rows(conn):
    from psycopg.types.json import Jsonb
    conn.execute(
        "INSERT INTO desired_port (switch, port, desired_vid, occupants, generation) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (switch, port) DO UPDATE SET desired_vid = EXCLUDED.desired_vid",
        (TEST_SWITCH, TEST_PORT, 17, Jsonb({"bmc": TEST_MAC}), 1))

    pool = db.build_pool(_dsn())
    try:
        rows = db.read_desired_ports(pool)
    finally:
        pool.close()

    match = [r for r in rows if r["switch"] == TEST_SWITCH and r["port"] == TEST_PORT]
    assert len(match) == 1
    assert match[0]["desired_vid"] == 17
    assert match[0]["occupants"] == {"bmc": TEST_MAC}


# ---------------------------------------------------------------------------
# read_switch_facts_ports
# ---------------------------------------------------------------------------

def test_read_switch_facts_ports_returns_keyed_dict(conn):
    from psycopg.types.json import Jsonb
    ports = {
        TEST_PORT: {"mask": "access", "access_vid": 16},
        "Ethernet1/1": {"mask": "trunk"},
    }
    conn.execute(
        "INSERT INTO switch_facts "
        "(switch, driver, polled_at, reachable, generation, port_mask, ports) "
        "VALUES (%s, 'eos', NOW(), TRUE, 1, '{}'::jsonb, %s) "
        "ON CONFLICT (switch) DO UPDATE SET ports = EXCLUDED.ports, reachable = TRUE",
        (TEST_SWITCH, Jsonb(ports)))

    pool = db.build_pool(_dsn())
    try:
        sf = db.read_switch_facts_ports(pool)
    finally:
        pool.close()

    assert (TEST_SWITCH, TEST_PORT) in sf
    assert sf[(TEST_SWITCH, TEST_PORT)] == {"mask": "access", "access_vid": 16,
                                            "link": None, "admin": None}
    assert sf[(TEST_SWITCH, "Ethernet1/1")] == {"mask": "trunk", "access_vid": None,
                                                "link": None, "admin": None}


def test_unreachable_switch_excluded(conn):
    from psycopg.types.json import Jsonb
    conn.execute(
        "INSERT INTO switch_facts "
        "(switch, driver, polled_at, reachable, generation, port_mask, ports) "
        "VALUES (%s, 'eos', NOW(), FALSE, 1, '{}'::jsonb, %s) "
        "ON CONFLICT (switch) DO UPDATE SET reachable = FALSE",
        (TEST_SWITCH, Jsonb({TEST_PORT: {"mask": "access", "access_vid": 99}})))

    pool = db.build_pool(_dsn())
    try:
        sf = db.read_switch_facts_ports(pool)
    finally:
        pool.close()

    assert (TEST_SWITCH, TEST_PORT) not in sf


# ---------------------------------------------------------------------------
# read_installing_ports
# ---------------------------------------------------------------------------

def _insert_observe(conn, port, vars_obj):
    from psycopg.types.json import Jsonb
    conn.execute(
        "INSERT INTO observe_state (switch, port, vars, last_polled, generation) "
        "VALUES (%s, %s, %s, NOW(), 1) "
        "ON CONFLICT (switch, port) DO UPDATE SET vars = EXCLUDED.vars",
        (TEST_SWITCH, port, Jsonb(vars_obj)))


def test_read_installing_ports_filters_on_install_state(conn):
    """A port whose host has fetched the Live ISO (nodepxe=found) but has NOT
    completed install (inventory!=found) is 'installing'. nodepxe=found +
    inventory=found is done (excluded); nodepxe=unknown is not installing
    (excluded). Ports are returned to_arista-canonicalized (et7b2 -> Ethernet7/2)
    so they compare 1:1 with cycle's loc.port (resolve_location applies to_arista)."""
    # installing: nodepxe found, inventory not found
    _insert_observe(conn, "et7b2", {
        "nodepxe": {"value": "found", "since": "2026-06-19T00:00:00Z"},
        "inventory": {"value": "notfound", "since": "2026-06-19T00:00:00Z"},
    })
    # done: nodepxe found, inventory found -> excluded
    _insert_observe(conn, "et8b1", {
        "nodepxe": {"value": "found", "since": "2026-06-19T00:00:00Z"},
        "inventory": {"value": "found", "since": "2026-06-19T00:00:00Z"},
    })
    # not installing: nodepxe unknown -> excluded (inventory missing entirely)
    _insert_observe(conn, "et9b1", {
        "nodepxe": {"value": "unknown", "since": None},
    })

    pool = db.build_pool(_dsn())
    try:
        installing = db.read_installing_ports(pool)
    finally:
        pool.close()

    assert (TEST_SWITCH, "Ethernet7/2") in installing   # to_arista-canonicalized
    assert (TEST_SWITCH, "et8b1") not in installing
    assert (TEST_SWITCH, "Ethernet8/1") not in installing
    assert (TEST_SWITCH, "et9b1") not in installing


def test_read_installing_ports_missing_inventory_var_is_installing(conn):
    """nodepxe=found with NO inventory var at all -> COALESCE('',...) <> 'found'
    so the host is still installing (Live ISO fetched, inventory not yet run)."""
    _insert_observe(conn, "et7b2", {
        "nodepxe": {"value": "found", "since": "2026-06-19T00:00:00Z"},
    })
    pool = db.build_pool(_dsn())
    try:
        installing = db.read_installing_ports(pool)
    finally:
        pool.close()
    assert (TEST_SWITCH, "Ethernet7/2") in installing
