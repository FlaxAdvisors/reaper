import os, sys
sys.path.insert(0, os.path.dirname(__file__))
import qual_battery
from qual_battery import Battery


_COUNT = "latest inventory in post-aabb is from X\n==========\n2 Memory Size: 64 GB\n1 Model: Mellanox\n\n"
_VERBOSE = ("latest inventory in post-aabb is from X\n==========\n"
            "Memory: 64 GB, DIMM A0, _Node0_Channel0_Dimm0, 2666 MT/s, Samsung, 31433C08, M386A8K40CM2-CTD\n\n")


def _macinv_script_runner(seen):
    """Stands in for bash running _MACINV_SH: prints what the script prints --
    the count form, the marker line, the detail form."""
    def runner(argv, timeout):
        seen.append(argv)
        return 0, _COUNT + qual_battery.MACINV_V_MARKER + "\n" + _VERBOSE
    return runner


def test_macinv_forms_builds_layout_and_runs_macinv_p():
    # macinv parses inventory FILES in a post-<mac>/latest/ dir, not `.`; the stage
    # must materialize that exact layout then run `macinv -p <dir>` (validated live).
    seen = []
    count, verbose = qual_battery._macinv_forms(_macinv_script_runner(seen))
    assert (count, verbose) == (_COUNT, _VERBOSE)
    assert len(seen) == 1                                    # ONE hardware collection
    assert seen[0][0] == "bash" and seen[0][1] == "-c"
    sh = seen[0][2]
    assert "macinv -p " in sh and "macinv -p ." not in sh   # a real dir, not cwd
    assert 'export PATH="/opt/flax/bin:$PATH"' in sh         # systemd-run PATH omits it -> else macinv NOT-FOUND
    assert 'ln -sfn inv "$d/latest"' in sh                   # the 'latest' symlink macinv needs
    for f in ("dmidecode.txt", "hwinfo.txt", "lspci-vvv.txt", "ipmitool_fru.txt",
              "ipmitool_lan_print_1.txt", "ipmitool_lan_print_8.txt",
              "ipmitool_mc_info.txt", "lldpcli-show-neigh.txt", "ethtool-i_"):
        assert f in sh, f
    # both forms off the same materialized dir: count, marker, then -v
    i_count = sh.index('macinv -p "$d"\n')
    i_mark = sh.index(qual_battery.MACINV_V_MARKER)
    i_verbose = sh.index('macinv -p "$d" -v')
    assert i_count < i_mark < i_verbose
    assert seen[0][3:] == []                                 # no full hwinfo handed in


def test_macinv_forms_detail_form_gets_the_full_hwinfo_capture():
    # the count form keeps the fast hwinfo subset (population-check unchanged);
    # the detail form's storage/GPU/controller tables read hwinfo Disk entries
    # the subset lacks, so the already-captured full hwinfo is copied in first
    seen, handed = [], {}

    def runner(argv, timeout):
        seen.append(argv)
        with open(argv[4]) as f:
            handed["text"] = f.read()
        return 0, _COUNT + qual_battery.MACINV_V_MARKER + "\n" + _VERBOSE
    qual_battery._macinv_forms(runner, hwinfo_full="31: None 00.0: 10600 Disk\n")
    assert handed["text"] == "31: None 00.0: 10600 Disk\n"
    assert not os.path.exists(seen[0][4])                    # temp file removed
    sh = seen[0][2]
    i_cp = sh.index('cp "$1" "$d/inv/hwinfo.txt"')
    assert sh.index('macinv -p "$d"\n') < i_cp < sh.index('macinv -p "$d" -v')


def test_macinv_forms_without_the_marker_keeps_the_count_and_has_no_detail():
    # the script died before the detail form (set -e): the count form is exactly
    # what it printed, and there is no detail form to upload
    count, verbose = qual_battery._macinv_forms(lambda a, t: (1, _COUNT))
    assert (count, verbose) == (_COUNT, None)


def test_inventory_uploads_count_and_detail_forms_from_one_collection(monkeypatch):
    seen = []
    monkeypatch.setattr(qual_battery, "_INV_CMDS", [("hwinfo", ["hwinfo", "--disk"], "raw")])
    monkeypatch.setattr(qual_battery, "_smartctl_all", lambda runner: "")
    base = _macinv_script_runner(seen)
    out = qual_battery._inventory(lambda argv, t: base(argv, t) if argv[:2] == ["bash", "-c"]
                                  else (0, "FULL HWINFO\n") if argv[0] == "hwinfo" else (0, ""))
    arts = out["artifacts"]
    assert arts["macinv"] == ("digest", _COUNT)          # unchanged: what population-check judges
    assert arts["macinv-v"] == ("digest", _VERBOSE)      # new: what the INV modal parses
    assert len(seen) == 1 and len(seen[0]) == 5          # one collection, full hwinfo handed in


def test_inventory_without_a_detail_form_uploads_no_macinv_v(monkeypatch):
    monkeypatch.setattr(qual_battery, "_INV_CMDS", [])
    monkeypatch.setattr(qual_battery, "_smartctl_all", lambda runner: "")
    arts = qual_battery._inventory(lambda argv, t: (0, _COUNT) if argv[:2] == ["bash", "-c"] else (0, ""))["artifacts"]
    assert arts["macinv"] == ("digest", _COUNT) and "macinv-v" not in arts


def _stage(name, verdict="pass", arts=None):
    def fn(runner):
        return {"verdict": verdict, "summary": {"s": name},
                "artifacts": arts or {name: ("raw", name + "-out")}}
    return {"name": name, "fn": fn}


def test_run_all_pass_sets_done_and_pass_verdict():
    b = Battery(runner=lambda a, t: (0, ""), stages=[_stage("sdr-pre"), _stage("sel-pre")],
                mac="aa:bb", serial="SN1")
    b.run()
    assert b.status()["status"] == "done"
    assert b.status()["verdict"] == "pass"
    assert b.status()["done_n"] == 2 and b.status()["total_n"] == 2 and b.status()["pct"] == 100
    assert [s["status"] for s in b.stages_list()] == ["pass", "pass"]


def test_one_fail_sets_fault_and_fail_verdict():
    b = Battery(runner=lambda a, t: (0, ""),
                stages=[_stage("sdr-pre"), _stage("cpu-mem-stress", verdict="fail")])
    b.run()
    assert b.status()["verdict"] == "fail"
    assert b.status()["status"] == "fault"
    assert b.stage("cpu-mem-stress")["status"] == "fail"


def test_stage_artifacts_and_content():
    b = Battery(runner=lambda a, t: (0, ""),
                stages=[_stage("inventory", arts={"dmidecode": ("raw", "Handle 0x1"),
                                                  "macinv": ("digest", "12 Memory Size: 64 GB")})])
    b.run()
    st = b.stage("inventory")
    names = {a["name"]: a for a in st["artifacts"]}
    assert names["dmidecode"]["kind"] == "raw" and names["dmidecode"]["bytes"] == len("Handle 0x1")
    assert b.artifact("inventory", "dmidecode") == "Handle 0x1"
    assert b.artifact("inventory", "nope") is None


def test_health_carries_identity_and_run_id():
    b = Battery(runner=lambda a, t: (0, ""), stages=[_stage("sdr-pre")], mac="aa:bb", serial="SN1")
    h = b.health()
    assert h["ok"] is True and h["mac"] == "aa:bb" and h["serial"] == "SN1"
    assert h["run_id"] and h["state"] in ("running", "done", "fault")


def test_restart_is_idempotent_and_holds_until_ack():
    b = Battery(runner=lambda a, t: (0, ""), stages=[_stage("sdr-pre")])
    b.run()
    old = b.health()["run_id"]
    r1 = b.request_restart(); r2 = b.request_restart()
    assert r1["old_run_id"] == old and r1["new_run_id"] == r2["new_run_id"]   # idempotent
    assert b.health()["state"] == "reset_pending"
    ack = b.ack_restart()
    assert ack["ok"] is True and b.health()["run_id"] == r1["new_run_id"]


def test_fio_skips_diskless_node():
    from qual_battery import _fio
    # lsblk shows only loop/ram/sr (no nvme*/sd*) -> a diskless node -> skip, no artifacts
    def runner(argv, t):
        return (0, "loop0\nram0\nsr0\n") if "lsblk" in " ".join(argv) else (0, "")
    out = _fio(runner)
    assert out["verdict"] == "skip"
    assert out["summary"]["reason"] == "no physical storage media"
    assert out["artifacts"] == {}


def test_run_aborts_when_run_id_rotates_midrun():
    ran = []
    def s_a(runner):
        ran.append("a")
        bat.run_id = "rotated"      # simulate a restart-ack replacing this run mid-stage
        return {"verdict": "pass", "summary": {}, "artifacts": {}}
    def s_b(runner):
        ran.append("b")
        return {"verdict": "pass", "summary": {}, "artifacts": {}}
    bat = Battery(runner=lambda a, t: (0, ""),
                  stages=[{"name": "a", "fn": s_a}, {"name": "b", "fn": s_b}])
    bat.run()
    assert ran == ["a"]                                # stage b never ran
    assert bat.stage("a")["status"] == "running"       # a's write was aborted (left as 'running')
    assert bat.stage("b")["status"] == "pending"       # never started


def test_battery_skip_stage_still_passes_overall():
    # a skip verdict must NOT fail the battery (diskless fio, unsupported platform, etc.)
    b = Battery(runner=lambda a, t: (0, ""),
                stages=[_stage("sdr-pre"),
                        {"name": "fio", "fn": lambda r: {"verdict": "skip", "summary": {}, "artifacts": {}}}])
    b.run()
    assert b.status()["verdict"] == "pass" and b.status()["status"] == "done"
    assert b.stage("fio")["status"] == "skip"
