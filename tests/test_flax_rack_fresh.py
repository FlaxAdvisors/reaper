"""rackpost Start fresh (passes) + the internal process report.

Start fresh opens a new pass: every latest read (tiles, gates, waivers, CSV, archive)
sees only the new pass; nothing is deleted, and the process report lists every pass.
"""
import json
import sqlite3
import threading

import pytest
pytest.importorskip("flax_rack.process_report")
from flax_rack import archive, done, process_report, report
from flax_rack.deploy import GateError, deploy_why, run_deploy
from flax_rack.qualify import run_qualify
from flax_rack.kea_client import KeaError
from flax_rack.qualify import qualify_why
from flax_rack.stages import close_order, run_discover, start_fresh
from tests.test_flax_rack_deploy import POP_WAIVE, events, qualified
from tests.test_flax_rack_qualify import profiles  # noqa: F401 -- fixture
from tests.test_flax_rack_server import RACK, _wait_idle, get, get_json, post, srv, factory  # noqa: F401


@pytest.fixture
def c(tmp_path, profiles):
    """Pass 1: Discover + Qualify (R05 POP fail), Deploy with R05 waived, Done (CSVs)."""
    c = qualified(tmp_path, pop_fail={"R05"})
    c.rungs.firstboot_fail = {"L37"}
    run_deploy(c, waive=POP_WAIVE, who="dbahi")
    done.run(c)
    return c


def test_fresh_hides_old_runs_and_waivers(c):
    n_runs = len(c.db.runs_all())
    assert c.db.waivers() and c.db.components() and c.db.ha_mode()
    out = start_fresh(c.dir, c.db, "op", "re-cabled wedge B")
    assert out["pass"]["id"] == 2
    assert out["renamed"] == ["F4-RRORD-101-devices.pass1.csv", "F4-RRORD-101-firmware.pass1.csv",
                              "F4-RRORD-101-inventory.pass1.csv"]
    assert report.csv_path(c.dir, c.g, "devices") is None \
        and (c.dir / "F4-RRORD-101-devices.pass1.csv").is_file()
    assert events(c)[0].startswith("start fresh (pass 2) by op: re-cabled wedge B")
    # every latest read starts over ...
    assert c.db.latest_runs() == {} and c.db.latest_stage_runs() == []
    assert c.db.waivers() == {} and c.db.waivers(history=True) == []
    assert c.db.components() == [] and c.db.ha_mode() is None
    assert c.db.latest_run_id("C37", "inventory") is None and c.db.run_count("C37", "inventory") == 0
    assert c.db.last_passed(("serial",)) == {}
    assert qualify_why(c.db.latest_runs(), c.g)["C37"] and deploy_why(c.db.latest_runs(), c.g)["C37"]
    rows = {r["pos"]: r for r in report.device_rows(c.dir, c.g, c.db)}
    assert rows["C37"]["chassis_serial"] in ("", None) and rows["C37"]["deploy"] in ("", None)
    assert archive.payload(c.g, c.db, c.dir)["rungs"] == []
    # ... nothing is deleted
    assert len(c.db.runs_all()) == n_runs and len(c.db.waivers_all()) == 1
    # a new Discover of one device is all the new pass sees
    run_discover(c, positions=["C37"])
    assert {k[0] for k in c.db.latest_runs()} - {"_rack"} == {"C37"}


def test_fresh_refused_while_running(srv, tmp_path, monkeypatch):
    from flax_rack import server
    body = {"confirm": "RRORD-101", "reason": "start over"}
    assert post(srv, "/rr101/api/fresh", {"confirm": "RRORD-101"}).status == 400   # no reason
    assert post(srv, "/rr101/api/fresh", {**body, "confirm": "rrord-101"}).status == 400
    srv.factory.gate = threading.Event()
    assert post(srv, "/rr101/api/stage/discover", RACK).status == 202
    assert srv.factory.entered.wait(5)
    r = post(srv, "/rr101/api/fresh", body)
    assert r.status == 409 and b"running" in r.body
    srv.factory.gate.set()
    _wait_idle(srv)
    drain = tmp_path / "drain"
    monkeypatch.setattr(server, "DRAIN_FILE", drain)
    drain.touch()
    assert post(srv, "/rr101/api/fresh", body).status == 409
    drain.unlink()
    s = get_json(srv, "/rr101/api/state")
    assert s["pass"]["id"] == 1 and any(d["runs"] for d in s["devices"])
    assert any(d["bmc_mac"] for d in s["devices"])
    r = post(srv, "/rr101/api/fresh", body)
    assert r.status == 200 and json.loads(r.body)["pass"]["id"] == 2
    s = get_json(srv, "/rr101/api/state")
    assert s["pass"] == {**s["pass"], "id": 2, "reason": "start over"}
    # tiles back to topology: no runs; the MACs (facts, not verdicts) and presence stay
    assert all(not d["runs"] and "presence" in d for d in s["devices"])
    assert any(d["bmc_mac"] for d in s["devices"])
    assert all(not sw["runs"] for sw in s["switches"]) and not s["rack_runs"]


def test_fresh_keeps_the_installed_os(tmp_path, profiles):
    """Start fresh resets verdicts, not the disks: after it, Deploy from firstboot works
    on the installed nodes and a plain Deploy still needs the typed reinstall."""
    c = qualified(tmp_path)
    run_deploy(c, who="op")
    assert deploy_why(c.db.latest_runs(), c.g) == {}
    start_fresh(c.dir, c.db, "op", "again")
    run_discover(c)
    run_qualify(c)
    with pytest.raises(GateError, match="already installed"):
        run_deploy(c)
    c.rungs.calls.clear()
    run_deploy(c, from_phase="firstboot")
    assert "install" not in [x[0] for x in c.rungs.calls]
    assert deploy_why(c.db.latest_runs(), c.g) == {}


def test_reinventory_keeps_earlier_pass_components(c):
    n = len(_components_all(c))
    start_fresh(c.dir, c.db, "op", "again")
    c.db.set_components("C37", [{"kind": "cpu", "model": "x"}], c.db.start_run("C37", "done", "components"))
    assert len(_components_all(c)) == n + 1
    assert [x["model"] for x in c.db.components("C37")] == ["x"]


def _components_all(c):
    with sqlite3.connect(c.db.path) as x:
        return x.execute("SELECT * FROM component").fetchall()


def test_process_report_spans_passes(c):
    sol = next(p for p in c.dir.glob("artifacts/C37/d*/install-console/sol.txt"))
    c.db.add_link_event("C37", "pcie0", "ok", "rr101-c37")
    c.db.add_link_event("C37", "pcie0", "down", "Ethernet31 down")
    c.db.add_link_event("C37", "pcie0", "ok", "rr101-c37")
    rid = c.db.start_run("L05", "qualify", "stress")
    assert c.db.mark_interrupted() == 1
    c.db.add_event("restart", "1 rung(s) interrupted by a daemon restart")
    start_fresh(c.dir, c.db, "op", "wedge B swapped")
    with pytest.raises(GateError):                         # pass 2's gate: C37 not qualified
        run_deploy(c, positions=["C37"])
    run_discover(c, positions=["C37"])
    path = process_report.write(c.dir, c.g, c.db)
    data = json.loads((c.dir / process_report.JSON).read_text())
    p1, p2 = data["passes"]
    assert (p1["id"], p2["id"], p2["reason"], p2["who"]) == (1, 2, "wedge B swapped", "op")
    assert p1["ended_at"] == p2["started_at"]
    runs = {(a["run"]) for atts in p1["devices"].values() for a in atts}
    assert rid in runs and len(runs) + sum(len(x) for x in p2["devices"].values()) \
        == len(c.db.runs_all())
    assert [w["pos"] for w in p1["waivers"]] == ["R05"] and p1["waivers"][0]["who"] == "dbahi"
    assert p2["waivers"] == []
    cats = {f["category"]: f for f in p1["failures"]}
    assert cats["deploy · firstboot · fail"]["devices"] == ["L37"]
    assert cats["qualify · stress · interrupted"]["count"] == 1
    assert [i["rung"] for i in p1["interruptions"]] == ["stress"] and p1["restarts"]
    assert [e["kind"] for e in p1["links"]] == ["change", "pull", "re-plug"] and p1["pulls"] == 1
    c37 = p1["devices"]["C37"]
    assert all(a["seconds"] is not None for a in c37)
    assert any(a["artifacts"].get("sol") == str(sol.relative_to(c.dir)) for a in c37)
    html = path.read_text()
    assert 'id="pass1"' in html and 'id="pass2"' in html and "wedge B swapped" in html
    assert f'href="{sol.relative_to(c.dir)}"' in html          # linked, not inlined
    assert sol.read_text()[:40] not in html


def test_close_writes_process_report_first(c):
    c.kea.fail_archive = True
    with pytest.raises(KeaError):
        close_order(c)
    assert (c.dir / process_report.HTML).is_file() and c.db.closed_at() is None
    (c.dir / process_report.HTML).unlink()
    (c.dir / process_report.JSON).unlink()
    (c.dir / process_report.JSON).mkdir()               # cannot be written: Close fails
    c.kea.fail_archive = False
    with pytest.raises(OSError):
        close_order(c)
    assert c.db.closed_at() is None and not c.kea.archives
    assert events(c)[0].startswith("close: process report not written")
    (c.dir / process_report.JSON).rmdir()
    close_order(c)
    assert c.db.closed_at() and (c.dir / process_report.HTML).is_file()
    # 040: the report THIS Close wrote rides the archive as a document, byte-exact
    import base64, gzip
    arts = {a["path"]: a for a in c.kea.archives[-1]["artifacts"]}
    sent = gzip.decompress(base64.b64decode(arts[process_report.HTML]["gzip_b64"]))
    assert sent == (c.dir / process_report.HTML).read_bytes()


def test_old_order_db_is_pass_one(c):
    first = c.db.runs_all()[0]["started_at"]
    with sqlite3.connect(c.db.path) as x:
        x.execute("DROP TABLE pass")
    c.db.init(c.g)
    (p,) = c.db.passes()
    assert p["id"] == 1 and p["started_at"] == first and p["run_after"] == 0
    assert c.db.latest_runs() and c.db.waivers()


def test_process_report_served(srv):
    r = get(srv, "/rr101/api/report/process.html")
    assert r.status == 200 and r.headers["Content-Type"].startswith("text/html")
    assert b'<base href="../../">' in r.body and b"Pass 1" in r.body


def test_closed_order_serves_the_report_close_wrote(srv, tmp_path):
    from flax_rack.orderdb import OrderDB
    d = tmp_path / "RRORD-101"
    assert get(srv, "/rr101/api/report/process.html").status == 200       # open: written
    stored = d / process_report.HTML
    stored.write_text("<html><head></head><body>as closed</body></html>")
    OrderDB(d / "order.db").set_closed()
    r = get(srv, "/rr101/api/report/process.html")
    assert r.status == 200 and b"as closed" in r.body
    assert stored.read_text() == "<html><head></head><body>as closed</body></html>"


def test_report_writers_never_share_a_temp_file(tmp_path):
    out = tmp_path / process_report.HTML
    errs = []

    def w(i):
        try:
            for _ in range(50):
                process_report._atomic(out, f"writer {i}\n" * 2000)
        except Exception as e:      # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=w, args=(i,)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs and out.read_text().startswith("writer ")
    assert [p.name for p in tmp_path.iterdir()] == [out.name]
