"""post_fio_drive: rows from a run's artifacts, the idempotent writer, the site peer query."""
import os
import pathlib

import pytest

from flax_post import fio_drives

FIX = pathlib.Path(__file__).resolve().parent / "fixtures" / "fio"
FIO = (FIX / "two-nvme.fio.txt").read_text()
ALLDISKS = (FIX / "two-nvme.alldisks.txt").read_text()
SMALL = ("SAMSUNG MZVL4256HBJD-00BH1", 256.0)
BIG = ("SAMSUNG MZ1LB3T8HMLA-000MV", 3840.0)


def test_rows_from_real_artifacts():
    rows = fio_drives.rows(FIO, ALLDISKS)
    assert len(rows) == 8  # 2 drives x (bw mbps, iops iops, lat lat_us, lat p99_us)
    by = {(r["dev"], r["job"], r["metric"]): r for r in rows}
    small = by[("nvme0n1", "bw", "mbps")]
    assert (small["model"], small["size_gb"], small["serial"]) == (*SMALL, "TESTSN-FIO-0")
    assert small["controller"] == "nvme" and 500 < small["value"] < 600
    assert by[("nvme1n1", "bw", "mbps")]["size_gb"] == 3840.0
    assert fio_drives.rows(None, ALLDISKS) == [] and fio_drives.rows(FIO, None)[0]["model"] is None


# ---- DB (the per-run scratch database; skips elsewhere) ----
psycopg = pytest.importorskip("psycopg")
MAC = "aa:de:ad:43:00:"


class _Pool:
    def __init__(self, dsn): self.dsn = dsn
    def connection(self): return psycopg.connect(self.dsn, autocommit=True, connect_timeout=3)


@pytest.fixture()
def db(monkeypatch):
    from flax_post import state
    dsn, adm = os.environ.get("FLAX_TEST_POST_DSN"), os.environ.get("FLAX_TEST_ADMIN_DSN")
    if not dsn or not adm:
        pytest.skip("no test DSNs")
    try:
        a = psycopg.connect(adm, autocommit=True, connect_timeout=3)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"no DB: {exc}")
    if a.execute("SELECT to_regclass('public._retention_scratch_marker')").fetchone()[0] is None:
        a.close(); pytest.skip("not the local scratch cluster")
    clean = lambda: a.execute("DELETE FROM post_fio_drive WHERE bmc_mac LIKE %s", (MAC + "%",))
    clean()
    monkeypatch.setattr(state, "get_pool", lambda: _Pool(dsn))
    yield state
    clean(); a.close()


def _row(serial, value, dev="nvme0n1", key=SMALL):
    return {"dev": dev, "serial": serial, "model": key[0], "size_gb": key[1],
            "controller": "nvme", "job": "bw", "metric": "mbps", "value": value}


def test_write_is_idempotent(db):  # RF2
    rows = fio_drives.rows(FIO, ALLDISKS)
    assert db.write_fio_drives(MAC + "01", "r1", rows) == 8
    db.write_fio_drives(MAC + "01", "r1", rows)
    peers = db.fio_peers([SMALL], [])
    assert len(peers[SMALL][("bw", "mbps")]) == 1


def test_peers_latest_run_per_serial_and_exclusions(db):  # RF1, RF3
    db.write_fio_drives(MAC + "01", "r1", [_row("S1", 100.0)])
    db.write_fio_drives(MAC + "01", "r2", [_row("S1", 200.0)])           # S1's later run
    db.write_fio_drives(MAC + "02", "r1", [_row("S2", 300.0)])
    db.write_fio_drives(MAC + "03", "r1", [_row(None, 999.0)])           # no serial: never a peer
    db.write_fio_drives(MAC + "04", "r1", [_row("S4", 400.0, key=BIG)])  # other size
    db.write_fio_drives(MAC + "05", "r1", [_row("MOVED", 50.0)])         # this node's drive, seen elsewhere
    p = db.fio_peers([SMALL, BIG], ["MOVED"])
    assert sorted(p[SMALL][("bw", "mbps")]) == [200.0, 300.0]
    assert p[BIG][("bw", "mbps")] == [400.0]
    assert db.fio_peers([], []) == {}


def test_backfill_writes_every_stored_run_with_fio():
    class Store:
        written = []
        def fio_runs(self): return [("m1", "r1"), ("m2", "r2")]
        def get_artifact(self, mac, run, stage, name):
            if mac == "m2":
                return None  # a run whose fio artifact is gone: skipped
            return FIO if (stage, name) == ("fio", "fio") else ALLDISKS
        def write_fio_drives(self, mac, run, rows):
            self.written.append((mac, run, len(rows))); return len(rows)
    s = Store()
    assert fio_drives.backfill(s) == (1, 8) and s.written == [("m1", "r1", 8)]
