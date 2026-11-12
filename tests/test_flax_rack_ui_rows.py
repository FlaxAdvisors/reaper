"""rackpost Plan 6 Task 3: what /api/state shows -- no agent-stages / console /
install-console step rows (the console panel carries the SOL), switch facts across
Start fresh, Firmware findings reported on the Run but never holding it."""
from flax_rack import run as runmod
from flax_rack.server import rack_state
from tests.test_flax_rack_run import rack, v  # noqa: F401
from tests.test_flax_rack_qualify import profiles  # noqa: F401 -- fixture
from tests.test_flax_rack_ui_state import _dev, _rack, _run


def test_hidden_rows_and_the_console_panel(tmp_path):
    r = _rack(tmp_path)
    _run(r.db, "L37", "qualify", "console", "pass", {"headline": "SOL captured: 9 bytes"})
    s = rack_state(r)
    l37 = _dev(s, "L37")
    assert "agent-stages" not in l37["qualify_rungs"] and "console" not in l37["qualify_rungs"]
    assert "install-console" not in l37["deploy_rungs"] and l37["deploy_rungs"][0] == "install"
    assert s["hidden_rungs"] == {"qualify": ["agent-stages", "console"], "deploy": ["install-console"]}
    assert l37["console"]["word"] == "captured"         # a Qualify capture is not scanned
    # Deploy's capture, while it runs: live; after: its scan's findings (the device fails)
    rid = r.db.start_run("L37", "deploy", "install-console")
    assert _dev(rack_state(r), "L37")["console"]["word"] == "live"
    from flax_rungs.result import RungResult
    r.db.finish_run(rid, RungResult("fail", {
        "findings": 2, "headline": "2 error line(s) in SOL",
        "rows": [["capture", "sol.txt, 9 bytes"], ["line 4", "I/O error: x"], ["line 9", "I/O error: y"]]},
        {"sol": "artifacts/L37/d1/install-console/sol.txt"}))
    c = _dev(rack_state(r), "L37")["console"]
    assert (c["rung"], c["word"], c["bad"]) == ("install-console", "2 findings", True)
    assert [x[0] for x in c["findings"]] == ["line 4", "line 9"] and c["artifacts"]["sol"]
    # a clean scan
    _run(r.db, "L37", "deploy", "install-console", "pass", {"findings": 0, "rows": []})
    assert _dev(rack_state(r), "L37")["console"]["word"] == "clean"


def test_switch_sn_and_mgmt_mac_survive_start_fresh(tmp_path):
    r = _rack(tmp_path)
    _run(r.db, "_sw-A", "discover", "serial", "pass", {"serial": "WEDGE-SN-A"})
    _run(r.db, "_sw-A", "discover", "host-mac-seen", "pass", {"mac": "02fc27000001"})
    _run(r.db, "_sw-A", "discover", "host-leased", "pass", {"ip": "172.26.0.61"})
    _run(r.db, "_sw-A", "discover", "bmc-mac-seen", "pass", {"mac": "02fc27000002"})
    r.db.start_pass("op", "again")
    sw = next(x for x in rack_state(r)["switches"] if x["id"] == "A")
    assert sw["runs"] == {}                           # verdicts: this pass only
    assert sw["serial"] == "WEDGE-SN-A"               # facts: last seen
    assert sw["sonic"] == {"mac": "02:fc:27:00:00:01", "ip": "172.26.0.61"}
    assert sw["bmc"]["mac"] == "02:fc:27:00:00:02"


def test_firmware_findings_never_hold_the_run(tmp_path, profiles):  # noqa: F811
    c = rack(tmp_path)
    c.rungs.fw_bmc_ver = {"R05": "flax-onetree-1.1.3"}  # R05's BMC off target
    runmod.run(c, who="op")
    rp = c.db.run_plan()
    assert rp["state"] == "done" and rp["held"] == {}  # detect only: straight through
    assert v(c, "R05", "ha-test") == "pass"
    ev = next(e["text"] for e in c.db.events(400) if "firmware findings" in e["text"])
    assert "R05 (firmware: bmc needs update)" in ev
    assert runmod.firmware_findings(c.db.latest_runs(), ["R05", "L37"]) == {
        "R05": ["firmware: bmc needs update"]}
