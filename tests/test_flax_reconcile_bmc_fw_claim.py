"""Tests for the BMC-FW claim sentinel helper (flax_reconcile.claims)
and the gate it enforces in the reconcile cycle's kick loop.

The helper tests are pure-filesystem (no DB required).
The gate test monkeypatches claims.bmc_fw_claim_active so it runs without
a real Postgres connection, mirroring the install-gate test's pattern
(test_cycle_skips_kick_for_no_lease_port_mid_install in
test_flax_reconcile_cycle.py).
"""
import pytest

from flax_reconcile import claims  # module created in Step 3


# ---------------------------------------------------------------------------
# Helper unit tests (no DB needed)
# ---------------------------------------------------------------------------

def test_claim_active_true_when_sentinel_present(tmp_path):
    d = tmp_path / "bmc-fw-active"
    d.mkdir()
    (d / "et6b1").write_text("")
    assert claims.bmc_fw_claim_active("et6b1", claim_dir=str(d)) is True


def test_claim_active_false_when_absent(tmp_path):
    d = tmp_path / "bmc-fw-active"
    d.mkdir()
    assert claims.bmc_fw_claim_active("et6b1", claim_dir=str(d)) is False


def test_claim_active_false_for_empty_port(tmp_path):
    d = tmp_path / "bmc-fw-active"
    d.mkdir()
    assert claims.bmc_fw_claim_active("", claim_dir=str(d)) is False


# ---------------------------------------------------------------------------
# Gate test: cycle skips the kick ladder when BMC-FW claim is active
#
# Uses monkeypatch to inject bmc_fw_claim_active (mirrors install-gate test's
# monkeypatch of db.read_installing_ports) so no real Postgres is needed.
# The DB fixture is skipped automatically when no DB is reachable.
# ---------------------------------------------------------------------------

psycopg = pytest.importorskip("psycopg")

import os  # noqa: E402

from flax_reconcile.cycle import Reconciler  # noqa: E402
from flax_reconcile import claims as claims_mod  # noqa: E402


class FakeLadder:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)
        return self.result


SWITCHES = ("rabbit-fiesta",)
MACS = ("aa:bb:cc:dd:ee:01",)


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
    hexes = [_mac_hex(m) for m in MACS]
    conn.execute(
        "DELETE FROM kea.lease4 WHERE hwaddr = ANY(%s::bytea[])",
        ([f"\\x{h}" for h in hexes],))
    conn.execute(
        "DELETE FROM kea.hosts "
        "WHERE dhcp_identifier_type = 0 AND dhcp_identifier = ANY(%s::bytea[])",
        ([f"\\x{h}" for h in hexes],))
    conn.execute("DELETE FROM devices WHERE mac = ANY(%s)", (list(MACS),))
    conn.execute("DELETE FROM reconcile_requests WHERE mac = ANY(%s)", (list(MACS),))
    conn.execute("DELETE FROM reconcile_flap_state WHERE mac = ANY(%s)", (list(MACS),))
    conn.execute("DELETE FROM reconcile_actions WHERE switch = ANY(%s)", (list(SWITCHES),))
    conn.execute("DELETE FROM intentional_flap WHERE switch = ANY(%s)", (list(SWITCHES),))
    conn.execute("DELETE FROM switch_facts WHERE switch = ANY(%s)", (list(SWITCHES),))
    conn.execute(
        "DELETE FROM audit.events WHERE service = 'flax-reconcile' AND switch = ANY(%s)",
        (list(SWITCHES),))


class _FlaxDB:
    def __init__(self, conn, pool):
        self.conn = conn
        self.pool = pool


@pytest.fixture()
def flax_db_bmc_fw():
    from flax_reconcile import db
    try:
        c = psycopg.connect(_dsn(), connect_timeout=3, autocommit=True)
    except Exception as exc:  # pragma: no cover - depends on env
        pytest.skip(f"no DB available: {exc}")
    try:
        pool = db.build_pool(_dsn())
    except Exception as exc:  # pragma: no cover - depends on env
        c.close()
        pytest.skip(f"no DB available: {exc}")
    try:
        _cleanup(c)
        yield _FlaxDB(c, pool)
    finally:
        try:
            _cleanup(c)
        except Exception:
            pass
        try:
            pool.close()
        except Exception:
            pass
        c.close()


def test_cycle_skips_kick_when_bmc_fw_claim_active(flax_db_bmc_fw, monkeypatch):
    """When a BMC-FW claim sentinel is active for a port, the kick ladder is
    NOT called for that port's queued request — mirrors the install-gate test's
    monkeypatch pattern for db.read_installing_ports.

    The cycle claims the next request from the queue and checks the claim before
    calling run_ladder. cycle.py converts req["port"] to internal form (et6b1)
    via portname.to_internal before calling bmc_fw_claim_active, so the
    monkeypatch matches on the internal form. The ladder must not be called and
    the request must be deferred (pending, attempts=1)."""
    c = flax_db_bmc_fw.conn
    # Reservation + wrong-IP lease so there IS a mismatch and a request gets enqueued.
    c.execute("INSERT INTO kea.hosts (dhcp_identifier, dhcp_identifier_type, ipv4_address) "
              "VALUES (decode('aabbccddee01','hex'), 0, 2886729733)")
    c.execute("INSERT INTO kea.lease4 (address, hwaddr, state) "
              "VALUES (2886729827, decode('aabbccddee01','hex'), 0)")
    c.execute("INSERT INTO devices (mac, switch, port, kind, latched, last_seen, generation) "
              "VALUES ('aa:bb:cc:dd:ee:01','rabbit-fiesta','Ethernet6/1','bmc','{}'::jsonb,NOW(),1)")

    # Inject: bmc_fw_claim_active returns True for et6b1 (internal form).
    # cycle.py converts req["port"] via portname.to_internal before calling
    # bmc_fw_claim_active, so the sentinel is always checked on the slash-free
    # internal form regardless of whether the request arrived as Ethernet6/1
    # (auto path) or et6b1 (operator path).
    monkeypatch.setattr(claims_mod, "bmc_fw_claim_active",
                        lambda port, claim_dir=None: port == "et6b1")

    ladder = FakeLadder(("switch_flap", True))
    r = Reconciler(switches={}, config={"flap_hold_seconds": 2, "kick_cooldown_secs": 60,
                                        "max_attempts": 3, "bmc_ll_probe_interval_secs": 60},
                   obmc_user="u", obmc_pass="p", host_creds=[], ladder=ladder)
    summary = r.run_one_cycle(flax_db_bmc_fw.pool)

    # Mismatch is detected and enqueued (1), then claimed but SKIPPED (kicked=1
    # counts the claimed request, ladder.calls must be empty).
    assert summary["enqueued"] == 1
    # ladder must NOT have been called — BMC-FW claim blocked it
    assert ladder.calls == [], (
        f"ladder was called despite BMC-FW claim: {ladder.calls}")
    # Request deferred (pending, attempts=1) — not discarded, retried later
    row = c.execute("SELECT status, attempts FROM reconcile_requests "
                    "WHERE mac='aa:bb:cc:dd:ee:01'").fetchone()
    assert row == ("pending", 1), f"expected (pending, 1) got {row}"
