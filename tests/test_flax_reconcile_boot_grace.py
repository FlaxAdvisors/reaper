"""Task 6 (part1-fw-tooling): reconcile boot grace + bin-owned manual claims.

* A reboot marker /run/flax/bmc-reboot/<port> younger than boot_grace_secs
  means a fw bin just rebooted that BMC: the kick ladder may still run the
  non-disruptive bmc_ll rung, but must NOT switch_flap the port.
* A bin-owned heartbeat claim /run/flax/bmc-fw-manual/<port> younger than
  manual_claim_max_age_secs defers the request exactly like bmc_fw's own
  bmc-fw-active claim.

No test here touches /run/flax: helper tests pass dirs explicitly, cycle tests
monkeypatch the claims module's *_DIR constants to tmp_path.
"""
import os
import time

import pytest

from flax_reconcile import claims, kick, config


# ---------------------------------------------------------------------------
# claims.bmc_reboot_recent
# ---------------------------------------------------------------------------
def test_marker_recent_within_grace(tmp_path):
    (tmp_path / "et6b1").write_text("")
    assert claims.bmc_reboot_recent("et6b1", time.time(), 600, reboot_dir=str(tmp_path))


def test_marker_older_than_grace(tmp_path):
    p = tmp_path / "et6b1"; p.write_text("")
    old = time.time() - 601; os.utime(p, (old, old))
    assert not claims.bmc_reboot_recent("et6b1", time.time(), 600, reboot_dir=str(tmp_path))


def test_no_marker_or_no_port(tmp_path):
    assert not claims.bmc_reboot_recent("et6b1", time.time(), 600, reboot_dir=str(tmp_path))
    assert not claims.bmc_reboot_recent("", time.time(), 600, reboot_dir=str(tmp_path))


def test_default_grace():
    assert config.DEFAULTS["boot_grace_secs"] == 600


# ---------------------------------------------------------------------------
# claims.bmc_manual_claim_active (heartbeat, max age 180 s)
# ---------------------------------------------------------------------------
def test_manual_claim_fresh_is_active(tmp_path):
    p = tmp_path / "et6b1"; p.write_text("")
    t = time.time() - 170; os.utime(p, (t, t))
    assert claims.bmc_manual_claim_active("et6b1", time.time(), 180, claim_dir=str(tmp_path))


def test_manual_claim_stale_is_inactive(tmp_path):
    """A bin that died without cleanup stops heartbeating; after 180 s its
    leftover file must no longer block reconcile."""
    p = tmp_path / "et6b1"; p.write_text("")
    t = time.time() - 181; os.utime(p, (t, t))
    assert not claims.bmc_manual_claim_active("et6b1", time.time(), 180, claim_dir=str(tmp_path))


def test_manual_claim_absent_or_no_port(tmp_path):
    assert not claims.bmc_manual_claim_active("et6b1", time.time(), 180, claim_dir=str(tmp_path))
    assert not claims.bmc_manual_claim_active("", time.time(), 180, claim_dir=str(tmp_path))


def test_manual_claim_default_max_age():
    assert config.DEFAULTS["manual_claim_max_age_secs"] == 180
    assert claims.BMC_FW_MANUAL_DIR == "/run/flax/bmc-fw-manual"
    assert claims.BMC_REBOOT_DIR == "/run/flax/bmc-reboot"


# ---------------------------------------------------------------------------
# kick.run_ladder(allow_flap=...)
# ---------------------------------------------------------------------------
def _ladder_calls(monkeypatch, bmc_ll_ok):
    seen = []
    monkeypatch.setattr(kick, "kick_via_bmc_ll", lambda **kw: seen.append("bmc_ll") or bmc_ll_ok)
    monkeypatch.setattr(kick, "kick_via_switch_flap", lambda **kw: seen.append("switch_flap") or True)
    return seen


def test_no_flap_when_disallowed(monkeypatch):
    seen = _ladder_calls(monkeypatch, bmc_ll_ok=False)
    r = kick.run_ladder(pool=None, switches={}, mac="aa:bb:cc:00:00:01", kind="bmc", switch="rabbit-t",
                        port="Ethernet6/1", vid=17, target_ip="10.0.0.9", obmc_user="u", obmc_pass="p",
                        host_creds=[], flap_hold_seconds=0, reason="t", allow_flap=False)
    assert r == (None, False) and seen == ["bmc_ll"]


def test_bmc_ll_still_runs_and_can_succeed_in_grace(monkeypatch):
    seen = _ladder_calls(monkeypatch, bmc_ll_ok=True)
    r = kick.run_ladder(pool=None, switches={}, mac="aa:bb:cc:00:00:01", kind="bmc", switch="rabbit-t",
                        port="Ethernet6/1", vid=17, target_ip="10.0.0.9", obmc_user="u", obmc_pass="p",
                        host_creds=[], flap_hold_seconds=0, reason="t", allow_flap=False)
    assert r == ("bmc_ll", True) and seen == ["bmc_ll"]


def test_flap_allowed_by_default(monkeypatch):
    seen = _ladder_calls(monkeypatch, bmc_ll_ok=False)
    r = kick.run_ladder(pool=None, switches={}, mac="aa:bb:cc:00:00:01", kind="bmc", switch="rabbit-t",
                        port="Ethernet6/1", vid=17, target_ip="10.0.0.9", obmc_user="u", obmc_pass="p",
                        host_creds=[], flap_hold_seconds=0, reason="t")
    assert r == ("switch_flap", True)
    assert seen == ["bmc_ll", "switch_flap"]


# ---------------------------------------------------------------------------
# DB-backed cycle tests (REAL local postgres; fixture-only names from
# test_flax_reconcile_cycle.py's allowlist -- NEVER real switch names).
# ---------------------------------------------------------------------------
psycopg = pytest.importorskip("psycopg")

from flax_reconcile.cycle import Reconciler  # noqa: E402
from flax_reconcile import claims as claims_mod  # noqa: E402

SWITCHES = ("rabbit-cycletest-a",)
MACS = ("aa:bb:cc:00:00:01",)
_CFG = {"flap_hold_seconds": 2, "kick_cooldown_secs": 60,
        "max_attempts": 3, "bmc_ll_probe_interval_secs": 60}


class FakeLadder:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)
        return self.result


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


def _cleanup(conn) -> None:
    hexes = [m.replace(":", "") for m in MACS]
    conn.execute("DELETE FROM kea.lease4 WHERE hwaddr = ANY(%s::bytea[])",
                 ([f"\\x{h}" for h in hexes],))
    conn.execute("DELETE FROM kea.hosts WHERE dhcp_identifier_type = 0 "
                 "AND dhcp_identifier = ANY(%s::bytea[])",
                 ([f"\\x{h}" for h in hexes],))
    conn.execute("DELETE FROM devices WHERE mac = ANY(%s)", (list(MACS),))
    conn.execute("DELETE FROM reconcile_requests WHERE mac = ANY(%s)", (list(MACS),))
    conn.execute("DELETE FROM reconcile_flap_state WHERE mac = ANY(%s)", (list(MACS),))
    conn.execute("DELETE FROM reconcile_actions WHERE switch = ANY(%s)", (list(SWITCHES),))
    conn.execute("DELETE FROM intentional_flap WHERE switch = ANY(%s)", (list(SWITCHES),))
    conn.execute("DELETE FROM switch_facts WHERE switch = ANY(%s)", (list(SWITCHES),))
    conn.execute("DELETE FROM desired_port WHERE switch = ANY(%s)", (list(SWITCHES),))
    conn.execute("DELETE FROM audit.events WHERE service = 'flax-reconcile' "
                 "AND switch = ANY(%s)", (list(SWITCHES),))
    conn.execute("DELETE FROM observe_state WHERE switch = ANY(%s)", (list(SWITCHES),))


class _FlaxDB:
    def __init__(self, conn, pool):
        self.conn = conn
        self.pool = pool


@pytest.fixture()
def flax_db():
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


@pytest.fixture()
def run_dirs(tmp_path, monkeypatch):
    """Point every /run/flax dir reconcile reads at tmp_path."""
    d = {name: tmp_path / name for name in ("active", "manual", "reboot")}
    for p in d.values():
        p.mkdir()
    monkeypatch.setattr(claims_mod, "BMC_FW_ACTIVE_DIR", str(d["active"]))
    monkeypatch.setattr(claims_mod, "BMC_FW_MANUAL_DIR", str(d["manual"]))
    monkeypatch.setattr(claims_mod, "BMC_REBOOT_DIR", str(d["reboot"]))
    return d


def _seed_bmc_mismatch(c):
    # BMC reserved 172.16.0.5, leased .99 -> mismatch on Ethernet6/1 (et6b1).
    c.execute("INSERT INTO kea.hosts (dhcp_identifier, dhcp_identifier_type, ipv4_address) "
              "VALUES (decode('aabbcc000001','hex'), 0, 2886729733)")
    c.execute("INSERT INTO kea.lease4 (address, hwaddr, state) "
              "VALUES (2886729827, decode('aabbcc000001','hex'), 0)")
    c.execute("INSERT INTO devices (mac, switch, port, kind, latched, last_seen, generation) "
              "VALUES ('aa:bb:cc:00:00:01','rabbit-cycletest-a','Ethernet6/1','bmc','{}'::jsonb, NOW(), 1)")


def _age(path, secs):
    path.write_text("")
    t = time.time() - secs
    os.utime(path, (t, t))


def _req_row(c):
    return c.execute("SELECT status, attempts FROM reconcile_requests "
                     "WHERE mac='aa:bb:cc:00:00:01'").fetchone()


def test_cycle_in_grace_passes_allow_flap_false(flax_db, run_dirs, monkeypatch):
    _seed_bmc_mismatch(flax_db.conn)
    monkeypatch.setattr(claims_mod, "bmc_reboot_recent", lambda *a, **k: True)
    ladder = FakeLadder(("bmc_ll", True))
    r = Reconciler(switches={}, config=_CFG, obmc_user="u", obmc_pass="p",
                   host_creds=[], ladder=ladder)
    r.run_one_cycle(flax_db.pool)
    assert len(ladder.calls) == 1
    assert ladder.calls[0]["allow_flap"] is False


def test_cycle_outside_grace_passes_allow_flap_true(flax_db, run_dirs, monkeypatch):
    _seed_bmc_mismatch(flax_db.conn)
    monkeypatch.setattr(claims_mod, "bmc_reboot_recent", lambda *a, **k: False)
    ladder = FakeLadder(("switch_flap", True))
    r = Reconciler(switches={}, config=_CFG, obmc_user="u", obmc_pass="p",
                   host_creds=[], ladder=ladder)
    r.run_one_cycle(flax_db.pool)
    assert len(ladder.calls) == 1
    assert ladder.calls[0]["allow_flap"] is True


def test_cycle_real_marker_fresh_vs_expired(flax_db, run_dirs):
    """End to end through the real marker reader: a fresh et6b1 marker
    (internal form, converted from the request's Ethernet6/1) suppresses the
    flap; one older than boot_grace_secs flaps normally again."""
    _seed_bmc_mismatch(flax_db.conn)
    _age(run_dirs["reboot"] / "et6b1", 30)
    ladder = FakeLadder((None, False))
    r = Reconciler(switches={}, config=_CFG, obmc_user="u", obmc_pass="p",
                   host_creds=[], ladder=ladder)
    r.run_one_cycle(flax_db.pool)
    assert [c["allow_flap"] for c in ladder.calls] == [False]
    # (None, False) in grace -> the existing failure path: deferred, not done.
    assert _req_row(flax_db.conn) == ("pending", 1)

    _cleanup(flax_db.conn)
    _seed_bmc_mismatch(flax_db.conn)
    _age(run_dirs["reboot"] / "et6b1", 601)
    ladder2 = FakeLadder(("switch_flap", True))
    r2 = Reconciler(switches={}, config=_CFG, obmc_user="u", obmc_pass="p",
                    host_creds=[], ladder=ladder2)
    r2.run_one_cycle(flax_db.pool)
    assert [c["allow_flap"] for c in ladder2.calls] == [True]
    assert _req_row(flax_db.conn)[0] == "done"


def test_cycle_grace_window_follows_config(flax_db, run_dirs):
    _seed_bmc_mismatch(flax_db.conn)
    _age(run_dirs["reboot"] / "et6b1", 120)
    ladder = FakeLadder(("switch_flap", True))
    r = Reconciler(switches={}, config=dict(_CFG, boot_grace_secs=60),
                   obmc_user="u", obmc_pass="p", host_creds=[], ladder=ladder)
    r.run_one_cycle(flax_db.pool)
    assert [c["allow_flap"] for c in ladder.calls] == [True]


def test_cycle_fresh_manual_claim_defers(flax_db, run_dirs):
    _seed_bmc_mismatch(flax_db.conn)
    _age(run_dirs["manual"] / "et6b1", 30)
    ladder = FakeLadder(("switch_flap", True))
    r = Reconciler(switches={}, config=_CFG, obmc_user="u", obmc_pass="p",
                   host_creds=[], ladder=ladder)
    summary = r.run_one_cycle(flax_db.pool)
    assert summary["enqueued"] == 1
    assert ladder.calls == []
    assert _req_row(flax_db.conn) == ("pending", 1)


def test_cycle_stale_manual_claim_ignored(flax_db, run_dirs):
    _seed_bmc_mismatch(flax_db.conn)
    _age(run_dirs["manual"] / "et6b1", 181)
    ladder = FakeLadder(("switch_flap", True))
    r = Reconciler(switches={}, config=_CFG, obmc_user="u", obmc_pass="p",
                   host_creds=[], ladder=ladder)
    r.run_one_cycle(flax_db.pool)
    assert len(ladder.calls) == 1
    assert _req_row(flax_db.conn)[0] == "done"


def test_cycle_bmc_fw_active_claim_still_defers(flax_db, run_dirs):
    """bmc_fw's own claim is existence-only (no age): even an old file defers."""
    _seed_bmc_mismatch(flax_db.conn)
    _age(run_dirs["active"] / "et6b1", 3600)
    ladder = FakeLadder(("switch_flap", True))
    r = Reconciler(switches={}, config=_CFG, obmc_user="u", obmc_pass="p",
                   host_creds=[], ladder=ladder)
    r.run_one_cycle(flax_db.pool)
    assert ladder.calls == []
    assert _req_row(flax_db.conn) == ("pending", 1)


# ---------------------------------------------------------------------------
# Part 2 / C: grace also after the port's last link-up and observe's last
# `bmcmac found` (spec 2026-09-27 §5), read from observe_state.vars.
# ---------------------------------------------------------------------------
def test_boot_signal_link_up_recent():
    now = time.time()
    assert claims.boot_signal_recent({"link_up": now - 30, "bmc_found": None},
                                     now, 600) == "link_up"


def test_boot_signal_bmc_found_recent():
    now = time.time()
    assert claims.boot_signal_recent({"link_up": now - 9999, "bmc_found": now - 5},
                                     now, 600) == "bmcmac_found"


def test_boot_signal_old_or_missing():
    now = time.time()
    assert claims.boot_signal_recent({"link_up": now - 601, "bmc_found": now - 601},
                                     now, 600) is None
    assert claims.boot_signal_recent(None, now, 600) is None
    assert claims.boot_signal_recent({}, now, 600) is None


def _seed_observe(c, *, link_since, bmcmac="found", bmcmac_since="2026-01-01T00:00:00Z"):
    import json
    vars_ = {"linkstate": {"value": "link", "since": link_since},
             "bmcmac": {"value": bmcmac, "since": bmcmac_since}}
    c.execute("INSERT INTO observe_state (switch, port, vars, resolved, last_polled, generation) "
              "VALUES ('rabbit-cycletest-a', 'et6b1', %s::jsonb, '{}'::jsonb, NOW(), 1) "
              "ON CONFLICT (switch, port) DO UPDATE SET vars = EXCLUDED.vars",
              (json.dumps(vars_),))


def _iso(secs_ago):
    import datetime
    return (datetime.datetime.utcnow() - datetime.timedelta(seconds=secs_ago)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_read_boot_signals_parses_vars(flax_db):
    from flax_reconcile import db
    _seed_observe(flax_db.conn, link_since=_iso(30), bmcmac_since=_iso(10))
    try:
        sig = db.read_boot_signals(flax_db.pool)[("rabbit-cycletest-a", "et6b1")]
        assert abs(sig["link_up"] - (time.time() - 30)) < 5
        assert abs(sig["bmc_found"] - (time.time() - 10)) < 5
    finally:
        flax_db.conn.execute("DELETE FROM observe_state WHERE switch='rabbit-cycletest-a'")


def test_read_boot_signals_held_bmcmac_is_not_found(flax_db):
    from flax_reconcile import db
    _seed_observe(flax_db.conn, link_since=_iso(3000), bmcmac="held",
                  bmcmac_since=_iso(10))
    try:
        sig = db.read_boot_signals(flax_db.pool)[("rabbit-cycletest-a", "et6b1")]
        assert sig["bmc_found"] is None
    finally:
        flax_db.conn.execute("DELETE FROM observe_state WHERE switch='rabbit-cycletest-a'")


def test_cycle_recent_link_up_is_grace(flax_db, run_dirs):
    """No reboot marker, but the port came up 30 s ago -> bmc_ll only."""
    _seed_bmc_mismatch(flax_db.conn)
    _seed_observe(flax_db.conn, link_since=_iso(30))
    try:
        ladder = FakeLadder((None, False))
        r = Reconciler(switches={}, config=_CFG, obmc_user="u", obmc_pass="p",
                       host_creds=[], ladder=ladder)
        r.run_one_cycle(flax_db.pool)
        assert [c["allow_flap"] for c in ladder.calls] == [False]
    finally:
        flax_db.conn.execute("DELETE FROM observe_state WHERE switch='rabbit-cycletest-a'")


def test_cycle_old_signals_flap_normally(flax_db, run_dirs):
    """Presence proof for the test above: same seed, signals older than the
    window -> the flap is allowed."""
    _seed_bmc_mismatch(flax_db.conn)
    _seed_observe(flax_db.conn, link_since=_iso(3000), bmcmac_since=_iso(3000))
    try:
        ladder = FakeLadder(("switch_flap", True))
        r = Reconciler(switches={}, config=_CFG, obmc_user="u", obmc_pass="p",
                       host_creds=[], ladder=ladder)
        r.run_one_cycle(flax_db.pool)
        assert [c["allow_flap"] for c in ladder.calls] == [True]
    finally:
        flax_db.conn.execute("DELETE FROM observe_state WHERE switch='rabbit-cycletest-a'")
