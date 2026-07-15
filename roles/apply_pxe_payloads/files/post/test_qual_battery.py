import os, sys
sys.path.insert(0, os.path.dirname(__file__))
import qual_battery
from qual_battery import Battery


def test_macinv_population_builds_layout_and_runs_macinv_p():
    # macinv parses inventory FILES in a post-<mac>/latest/ dir, not `.`; the stage
    # must materialize that exact layout then run `macinv -p <dir>` (validated live).
    seen = {}
    def runner(argv, timeout):
        seen["argv"] = argv
        return 0, "2 Memory Size: 64 GB\n1 Model: Mellanox\n"
    out = qual_battery._macinv_population(runner)
    assert out == "2 Memory Size: 64 GB\n1 Model: Mellanox\n"
    assert seen["argv"][0] == "bash" and seen["argv"][1] == "-c"
    sh = seen["argv"][2]
    assert "macinv -p " in sh and "macinv -p ." not in sh   # a real dir, not cwd
    assert 'export PATH="/opt/flax/bin:$PATH"' in sh         # systemd-run PATH omits it -> else macinv NOT-FOUND
    assert 'ln -sfn inv "$d/latest"' in sh                   # the 'latest' symlink macinv needs
    for f in ("dmidecode.txt", "hwinfo.txt", "lspci-vvv.txt", "ipmitool_fru.txt",
              "ipmitool_lan_print_1.txt", "ipmitool_lan_print_8.txt",
              "ipmitool_mc_info.txt", "lldpcli-show-neigh.txt", "ethtool-i_"):
        assert f in sh, f


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
