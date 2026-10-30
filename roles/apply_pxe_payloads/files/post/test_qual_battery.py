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
    assert 'export PATH="$PWD:/opt/flax/bin:$PATH"' in sh    # bundled macinv first; systemd-run PATH omits /opt/flax/bin
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


def test_macinv_forms_both_forms_get_the_full_hwinfo_capture():
    # both forms' block-device line reads hwinfo Disk entries the fast subset
    # lacks, so the already-captured full hwinfo is copied in before the COUNT
    # form (2026-10-30: copied after it, the count form never listed a disk)
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
    assert i_cp < sh.index('macinv -p "$d"\n') < sh.index('macinv -p "$d" -v')


def test_macinv_script_runs_the_bundled_macinv_before_the_isos(tmp_path):
    # The live ISO bakes its own /opt/flax/bin/macinv (Feb 2025, every FRU as
    # "Board Mfg:"); post.tgz bundles the current one into the agent's cwd.
    # The bundled copy must win, else the population profile judges the ISO's
    # format (2026-09-28: no node ever printed "Addon FRU:"). Real bash: a stale
    # macinv earlier on the inherited PATH stands in for the ISO's copy.
    import subprocess
    agent, stale = tmp_path / "agent", tmp_path / "stale"
    for d, tag in ((agent, "BUNDLED"), (stale, "STALE")):
        d.mkdir()
        (d / "macinv").write_text("#!/bin/sh\necho %s\n" % tag)
        (d / "macinv").chmod(0o755)
    env = dict(os.environ, PATH="%s:%s" % (stale, os.environ["PATH"]))
    out = subprocess.run(["bash", "-c", qual_battery._MACINV_SH], cwd=agent, env=env,
                         capture_output=True, text=True, timeout=60).stdout
    assert out.splitlines()[0] == "BUNDLED", out
    assert "STALE" not in out, out


_R37 = os.path.join(os.path.dirname(__file__), "testdata", "macinv-rr101-r37")
# f4-compute-tp (bang-gouda /opt/flax/node_config, 2026-10-27), copied verbatim
_F4_COMPUTE_TP = [
    "BIOS Version: TPO_P26F",
    r"Chassis Serial: C\.M\.B60\.00X1C",
    "Board Mfg: Wistron, Product: Tioga ?Pass",
    "^2 Processor.*Intel.*Gold 6138.*2.00GHz",
    "^4 Memory Size: 32 GB",
    "^1 Model:.*MCX4411A-ACQN",
    "^2 Model:.*(ConnectX-4 Lx|ConnectX-5|25G|XXV710|E810|BCM5741[24])",
    '^1 Model: "[^"]*", SubVendor: pci .*Capacity: 23[2-9] GB',
]
_R37_DISK = ('1 Model: "Sandisk WD Black SN850", SubVendor: pci 0x15b7 "Sandisk Corp", '
             'SubDevice: pci 0x5011 , Capacity: 238 GB')


def _r37_full_hwinfo():
    import gzip
    with gzip.open(os.path.join(_R37, "hwinfo.txt.gz"), "rt") as f:
        return f.read()


def _r37_agent(tmp_path, macinv_body=None):
    """An agent cwd whose stub tools print R37's captures (RRORD-101, run
    bf5e01a3, 2026-10-30) the way _MACINV_SH invokes them. The stub hwinfo is
    the fast subset: the full capture minus its Disk entries (`hwinfo` without
    --disk prints none). macinv is the real one copied in, or a stub."""
    import gzip, re, shutil
    agent, cap = tmp_path / "agent", tmp_path / "cap"
    agent.mkdir(); cap.mkdir()
    full = _r37_full_hwinfo()
    (cap / "hwinfo-subset.txt").write_text(
        "".join(b for b in re.split(r"(?<=\n\n)", full) if not re.match(r"\d+: .* Disk\n", b)))
    with gzip.open(os.path.join(_R37, "dmidecode.txt.gz"), "rt") as f:
        (cap / "dmidecode.txt").write_text(f.read())
    for n in ("ipmitool_fru.txt", "ipmitool_lan_print_1.txt", "ipmitool_lan_print_8.txt",
              "ipmitool_mc_info.txt", "lldpcli-show-neigh.txt"):
        shutil.copy(os.path.join(_R37, n), str(cap / n))
    stubs = {
        "dmidecode": 'cat "%s/dmidecode.txt"',
        "hwinfo": 'cat "%s/hwinfo-subset.txt"',
        "lspci": "true",                                   # count form only checks the file exists
        "ethtool": "true",
        "lldpcli": 'cat "%s/lldpcli-show-neigh.txt"',
        "ipmitool": 'case "$*" in fru) f=ipmitool_fru;; "lan print 1") f=ipmitool_lan_print_1;; '
                    '"lan print 8") f=ipmitool_lan_print_8;; *) f=ipmitool_mc_info;; esac; cat "%s/$f.txt"',
    }
    for name, body in stubs.items():
        (agent / name).write_text("#!/bin/bash\n" + (body % cap if "%s" in body else body) + "\n")
        (agent / name).chmod(0o755)
    if macinv_body is not None:
        (agent / "macinv").write_text("#!/bin/bash\n" + macinv_body + "\n")
        (agent / "macinv").chmod(0o755)
    return agent, full


def _run_in(agent):
    import subprocess
    def runner(argv, timeout):
        p = subprocess.run(argv, cwd=str(agent), capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    return runner


def _real_macinv():
    for p in (os.environ.get("FLAX_MACINV"), "/opt/flax/bin/macinv"):
        if p and os.path.isfile(p):
            return p
    return None


def test_macinv_count_form_reads_the_full_hwinfo(tmp_path):
    # Real bash over _MACINV_SH; the stub macinv reports how many hwinfo Disk
    # entries its post-<mac>/latest dir holds. The count form must see the full
    # capture's disk, not the subset's none.
    agent, full = _r37_agent(tmp_path, 'echo "disks=$(grep -c " Disk$" "$2/latest/hwinfo.txt")"')
    count, detail = qual_battery._macinv_forms(_run_in(agent), full)
    assert count == "disks=1\n" and detail == "disks=1\n"
    count, detail = qual_battery._macinv_forms(_run_in(agent))      # no full capture handed in
    assert count == "disks=0\n"                                     # presence proof: subset has none


def test_macinv_count_form_lists_the_r37_disk_with_the_real_macinv(tmp_path):
    # End to end with ghost's macinv (bundled into post.tgz from the bang's
    # /opt/flax/bin; not in this repo -- FLAX_MACINV points at a copy).
    import shutil
    import pytest
    src = _real_macinv()
    if not src:
        pytest.skip("no macinv: set FLAX_MACINV to a copy of the bang's /opt/flax/bin/macinv")
    agent, full = _r37_agent(tmp_path)
    shutil.copy(src, str(agent / "macinv"))
    count, detail = qual_battery._macinv_forms(_run_in(agent), full)
    with open(os.path.join(_R37, "macinv-live.txt")) as f:
        live = f.read()
    # the live count form ended in a bare "1 " where the disk belongs; every
    # other line (header timestamp aside) is unchanged
    assert live.splitlines()[2:] == [ln if ln != _R37_DISK else "1 "
                                     for ln in count.splitlines()[2:]]
    assert _R37_DISK in count.splitlines()
    assert ", Capacity: 238 GB" in detail


def test_r37_fixed_count_form_passes_f4_compute_tp():
    import sys
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *[".."] * 4)))
    from flax_post import population
    with open(os.path.join(_R37, "macinv-live.txt")) as f:
        live = f.read()
    fixed = live.replace("\n1 \n", "\n" + _R37_DISK + "\n")
    assert fixed != live
    assert population.evaluate(_F4_COMPUTE_TP, fixed)["ok"]
    r = population.evaluate(_F4_COMPUTE_TP, live)                   # what the agent shipped
    assert [x["rule"] for x in r["results"] if not x["ok"]] == [_F4_COMPUTE_TP[-1]]


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


def test_inventory_uploads_the_bmc_usbnet_report_last(monkeypatch):
    # collect_bmc_usbnet.sh brings up a new ethN, so it must run after every
    # NIC-reading capture (hwinfo, lsnet, macinv) or they would list it.
    seen = []
    monkeypatch.setattr(qual_battery, "_INV_CMDS", [("lsnet", ["./lsnet"], "raw")])
    monkeypatch.setattr(qual_battery, "_smartctl_all", lambda runner: "")

    def runner(argv, t):
        seen.append(argv[0])
        if argv == ["./collect_bmc_usbnet.sh"]:
            return 0, "present: yes\nverdict: ok\n"
        return (0, _COUNT) if argv[:2] == ["bash", "-c"] else (0, "")
    arts = qual_battery._inventory(runner)["artifacts"]
    assert arts["bmc-usbnet"] == ("raw", "present: yes\nverdict: ok\n")
    assert seen[-1] == "./collect_bmc_usbnet.sh"


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


def test_inv_cmds_capture_tpmstate_read_only():
    # flax_post's tpm-state rung judges this capture; post only CHECKS the TPM
    hits = [c for c in qual_battery._INV_CMDS if c[0] == "tpmstate"]
    assert len(hits) == 1
    name, argv, kind = hits[0]
    assert argv == ["sh", "-c", qual_battery.TPMSTATE_SH] and kind == "raw"
    for want in ("/sys/class/tpm/tpm0", "device=present", "device=absent", "python3 ./tpm_state.py"):
        assert want in qual_battery.TPMSTATE_SH
    for never in ("tpm_clear", "tpm_sce", "ppi/request", "efivars"):
        assert never not in qual_battery.TPMSTATE_SH


def test_tpmstate_script_reports_absent_device(tmp_path):
    import subprocess
    sh = qual_battery.TPMSTATE_SH.replace("/sys/class/tpm/tpm0", str(tmp_path / "nope"))
    r = subprocess.run(["sh", "-c", sh], capture_output=True, text=True)
    assert (r.returncode, r.stdout) == (0, "device=absent\n")


_ORDER = "sdr-pre,sel-pre,sel-clear,tooling,inventory,fio,mem-pre,cpu-mem-stress,mem-post,sdr-post,sel-post"


def test_select_stages_default_unchanged():
    assert qual_battery.select_stages(None) is qual_battery.DEFAULT_STAGES
    assert qual_battery.select_stages("") is qual_battery.DEFAULT_STAGES
    assert len(qual_battery.select_stages(None)) == 12


def test_select_stages_order_mode_drops_iperf_keeps_default_order():
    got = [s["name"] for s in qual_battery.select_stages(_ORDER)]
    assert len(got) == 11 and "iperf" not in got
    shuffled = ",".join(reversed(_ORDER.split(",")))
    assert [s["name"] for s in qual_battery.select_stages(shuffled)] == got


def test_select_stages_unknown_name_raises():
    import pytest
    with pytest.raises(ValueError) as e:
        qual_battery.select_stages("fio,bogus")
    assert str(e.value) == "bogus"


def test_battery_with_error_reports_health_and_runs_nothing():
    b = Battery(runner=lambda a, t: (0, ""), stages=[], error="unknown stage bogus")
    assert b.health()["ok"] is False and b.health()["error"] == "unknown stage bogus"
    b.run()
    assert b.state == "fault" and b.verdict is None
    b.request_restart(); b.ack_restart()
    assert b.state == "fault"
