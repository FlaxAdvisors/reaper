"""The fio drive reference guide (/guides/fio-drives.html): history + the site's collected drives."""
import os

import pytest

from flax_post import fio_guide

SMALL = ("SAMSUNG MZVL4256HBJD-00BH1", 256.0)
BASE = {"suite": "v1", "generated": "2026-11-13", "source": "bang-fiesta:/export/bangs",
        "groups": [
            {"model": SMALL[0], "size_gb": 256.0, "controller": None, "job": "bw", "metric": "mbps",
             "n": 8, "mean": 2500.0, "std": 100.0, "clipped": 0, "slow_dropped": 6,
             "from": "2026-11-09", "to": "2026-11-12"},
            {"model": SMALL[0], "size_gb": 256.0, "controller": "nvme", "job": "bw", "metric": "mbps",
             "n": 8, "mean": 2500.0, "std": 100.0, "clipped": 0, "slow_dropped": 6},
            {"model": "HGST HUS726060AL", "size_gb": 6000.0, "controller": None, "job": "lat",
             "metric": "p99_us", "n": 545, "mean": 130000.0, "std": 40000.0, "clipped": 3,
             "slow_dropped": 0}]}


def _r(serial, value, at, run="r1", mac="m1", key=SMALL, job="bw", metric="mbps"):
    return {"bmc_mac": mac, "run_id": run, "serial": serial, "model": key[0], "size_gb": key[1],
            "controller": "nvme", "dev": "nvme0n1", "job": job, "metric": metric, "value": value,
            "captured_at": at}


ROWS = [_r("A", 400.0, "2026-11-10T01:00:00+00:00", run="r1", mac="m1"),
        _r("A", 2450.0, "2026-11-11T01:00:00+00:00", run="r2", mac="m1"),   # A's latest: fast
        _r("B", 2600.0, "2026-11-10T02:00:00+00:00", run="r3", mac="m2"),
        _r("C", 350.0, "2026-11-12T02:00:00+00:00", run="r4", mac="m3"),     # outside 1σ
        _r(None, 999.0, "2026-11-12T03:00:00+00:00", run="r5", mac="m4"),    # no serial: a run, not a drive
        _r("N1", 3100.0, "2026-11-12T04:00:00+00:00", run="r6", mac="m5", key=("NEW NVME 512", 512.0))]


def test_summary_joins_history_and_the_sites_drives():
    s = {(e["model"], e["size_gb"]): e for e in fio_guide.summarize(BASE, ROWS)}
    e = s[SMALL]
    assert e["status"] == "history" and (e["drives"], e["runs"], e["nodes"]) == (3, 5, 4)
    assert e["last"] == "2026-11-12"
    bw = e["metrics"][("bw", "mbps")]
    assert bw["latest_median"] == 2450.0 and bw["outside"] == 1 and bw["latest_n"] == 3
    assert (bw["history"]["n"], bw["history"]["slow_dropped"], bw["used"]) == (8, 6, True)


def test_a_collected_only_key_joins_history_at_the_next_mining():
    s = {(e["model"], e["size_gb"]): e for e in fio_guide.summarize(BASE, ROWS)}
    e = s[("NEW NVME 512", 512.0)]
    assert e["status"] == "collected only" and e["metrics"][("bw", "mbps")]["history"] is None
    assert e["metrics"][("bw", "mbps")]["outside"] is None


def test_history_only_keys_are_listed_and_a_wide_group_is_not_used():
    s = {(e["model"], e["size_gb"]): e for e in fio_guide.summarize(BASE, ROWS)}
    e = s[("HGST HUS726060AL", 6000.0)]
    assert e["status"] == "history only" and e["drives"] == 0
    assert e["metrics"][("lat", "p99_us")]["used"] is False  # std/mean 31% > the spread cap


def test_render_escapes_and_names_the_site_and_rules():
    rows = ROWS + [_r("X", 1.0, "2026-11-12T05:00:00+00:00", key=("<SCRIPT>", 1.0))]
    h = fio_guide.render(BASE, rows, site="lavergne", generated="2026-11-13 12:00 UTC")
    assert "<SCRIPT>" not in h and "&lt;SCRIPT&gt;" in h
    assert "lavergne" in h and "2026-11-13 12:00 UTC" in h and "bang-fiesta:/export/bangs" in h
    assert "1σ" in h and "10%" in h and "collected only" in h and "2,500 ± 100" in h
    assert h.index("NEW NVME 512") < h.index("HGST HUS726060AL")  # collected keys first


def test_main_writes_the_page_atomically(tmp_path, monkeypatch):
    out = tmp_path / "guides" / "fio-drives.html"
    out.parent.mkdir()
    out.write_text("old")
    monkeypatch.setenv("SITE_NAME", "eindhoven")
    assert fio_guide.main(["--out", str(out)], rows=ROWS, baseline=BASE) == 0
    t = out.read_text()
    assert t.startswith("<!doctype html>") and "eindhoven" in t
    assert [p.name for p in out.parent.iterdir()] == ["fio-drives.html"]
    assert oct(out.stat().st_mode & 0o777) == "0o644"


# ---- DB: the rows the guide reads ----
psycopg = pytest.importorskip("psycopg")
MAC = "aa:de:ad:44:00:"


class _Pool:
    def __init__(self, dsn): self.dsn = dsn
    def connection(self): return psycopg.connect(self.dsn, autocommit=True, connect_timeout=3)


def test_fio_drive_rows_reads_every_row(monkeypatch):
    from flax_post import state
    dsn, adm = os.environ.get("FLAX_TEST_POST_DSN"), os.environ.get("FLAX_TEST_ADMIN_DSN")
    if not dsn or not adm:
        pytest.skip("no test DSNs")
    a = psycopg.connect(adm, autocommit=True, connect_timeout=3)
    if a.execute("SELECT to_regclass('public._retention_scratch_marker')").fetchone()[0] is None:
        a.close(); pytest.skip("not the local scratch cluster")
    a.execute("DELETE FROM post_fio_drive WHERE bmc_mac LIKE %s", (MAC + "%",))
    monkeypatch.setattr(state, "get_pool", lambda: _Pool(dsn))
    try:
        r = {"dev": "nvme0n1", "serial": "S9", "model": SMALL[0], "size_gb": 256.0,
             "controller": "nvme", "job": "bw", "metric": "mbps", "value": 2400.0}
        state.write_fio_drives(MAC + "01", "run9", [r], captured_at="2026-11-11T01:00:00+00:00")
        got = [x for x in state.fio_drive_rows() if x["bmc_mac"] == MAC + "01"]
        assert len(got) == 1
        g = got[0]
        assert (g["serial"], g["model"], g["size_gb"], g["job"], g["metric"], g["value"], g["run_id"]) == (
            "S9", SMALL[0], 256.0, "bw", "mbps", 2400.0, "run9")
        assert g["captured_at"].startswith("2026-11-11")
    finally:
        a.execute("DELETE FROM post_fio_drive WHERE bmc_mac LIKE %s", (MAC + "%",))
        a.close()
