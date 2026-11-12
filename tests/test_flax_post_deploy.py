"""Loose Post Deploy (spec 2026-11-06 loose-post-deploy-stage): judges, trigger, the
Deploy phase on the tile, Finish gating, the IPMI guard, and a faked end-to-end run."""
import json

import pytest
from types import SimpleNamespace

from flax_post import blades, deploy, finish
from flax_post.observe import ipmi
from flax_rungs import install
from flax_rungs.result import RungResult

SDR_OK = ("Inlet Temp       | 01h | ok  |  7.1 | 25 degrees C\n"
          "P0 Temp          | 02h | ok  |  3.1 | 50 degrees C\n"
          "HSC Fan          | 03h | ns  | 29.1 | No Reading\n")
DIMM0 = "A0        sn: 1234           ce: 0    ue: 0\nB0        sn: 5678           ce: 2    ue: 0\n"


def test_phase_steps_match_engine():
    assert blades.PHASE_STEPS["Deploy"] == deploy.PHASES
    assert blades.PHASES == ("Discover", "Firmware", "Qualify", "Deploy", "Done")


def test_target_disk_and_node_setting():
    ud = "#cloud-config\nautoinstall:\n  storage:\n    config:\n      - {\n          path: /dev/sda,\n"
    assert deploy.target_disk(ud) == "/dev/sda"
    assert deploy.node_setting("# x\nset node-setting leopard\nset com-default com2s\n") == "leopard"


def test_system_storage():
    alld = "/dev/nvme0n1 nvme 0 rotational 960 SAMSUNG MZ1LB960 EDA5 S1234\n"
    assert deploy.has_system_storage(alld, "/dev/nvme0n1")[0] is True
    ok, why = deploy.has_system_storage(alld, "/dev/sda")
    assert ok is False and "no system storage" in why
    assert deploy.has_system_storage(None, "/dev/sda")[0] is None       # never "no disk"


def test_sdr_bad():
    assert deploy.sdr_bad(SDR_OK, SDR_OK) == []
    hot = SDR_OK.replace("P0 Temp          | 02h | ok ", "P0 Temp          | 02h | cr ")
    assert deploy.sdr_bad(SDR_OK, hot) == ["P0 Temp: ok -> cr"]
    nc = SDR_OK.replace("Inlet Temp       | 01h | ok ", "Inlet Temp       | 01h | nc ")
    assert deploy.sdr_bad(SDR_OK, nc) == ["Inlet Temp: ok -> nc"]
    assert deploy.sdr_bad(nc, nc) == []                       # non-critical already there


def test_dimm_counts_reads_silkscreen_labels_with_spaces():
    # dimmerr prints the board silkscreen ("DIMM A0" on TiogaPass); a one-word-only
    # label match read nothing -> deploy-stress "dimmerr not read" (LVQ et7b1 2026-11-10)
    out = ("DIMM A0   sn: 368A9D23       ce: 0    ue: 0\n"
           "DIMM B4   sn: 126343C1       ce: 1    ue: 0\n"
           "DIMM C1   sn: ?              ce: 0    ue: 2\n")
    assert deploy.dimm_counts(out) == {"DIMM A0": (0, 0), "DIMM B4": (1, 0), "DIMM C1": (0, 2)}
    assert deploy.dimm_counts(DIMM0) == {"A0": (0, 0), "B0": (2, 0)}


def test_dimm_bad():
    assert deploy.dimm_bad(DIMM0, DIMM0) == []
    assert deploy.dimm_bad(DIMM0, DIMM0.replace("ce: 2", "ce: 3")) == ["B0: ce 2->3"]  # any CE fails
    assert deploy.dimm_bad(DIMM0, DIMM0.replace("ue: 0\nB0", "ue: 1\nB0")) == ["A0: ue 0->1"]


def test_stress_hw_events_threshold():
    from flax_rungs import stress
    mce = "[  253.1] mce: [Hardware Error]: Machine check events logged"
    ev = stress.hw_events([mce], None, None)
    assert ev["corrected"] == 1 and not ev["uncorrected"] and ev["source"] == "dmesg"
    ras = ("MCE events:\n"
           "1 2026-11-06 17:37:16 +0000 error: Corrected_error, mcgstatus=0, bank=7\n"
           "2 2026-11-06 17:38:00 +0000 error: Uncorrected_error, bank=4\n"
           "3 2026-11-05 10:00:00 +0000 error: Corrected_error, bank=7\n")
    import datetime
    t0 = datetime.datetime(2026, 11, 6, 17, 30, tzinfo=datetime.timezone.utc).timestamp()
    ev = stress.hw_events([mce], ras, t0)
    assert ev["source"] == "rasdaemon" and ev["corrected"] == 1      # the 11-05 one is older
    assert len(ev["uncorrected"]) == 1 and "bank=4" in ev["uncorrected"][0]


def test_keep_install_needs_same_serial():
    assert deploy.keep_install({"deployed": {"serial": "S1"}}, "S1")
    assert deploy.keep_install({"deployed": {"serial": "S1"}}, "S2") is None
    assert deploy.keep_install({}, "S1") is None


def _row(dep=None, req=None, verdict="pass", pending=True):
    done = {"verdict": verdict, **({"deploy": "pending"} if pending else {})}
    return {"done": done, "qual": {"run_id": "r1"}, "deploy": dep or {},
            **({"deploy_request": req} if req else {})}


def test_wants_deploy():
    assert deploy.wants_deploy(_row()) == "auto"
    assert deploy.wants_deploy(_row(pending=False)) is None
    assert deploy.wants_deploy(_row(verdict="fail")) is None
    assert deploy.wants_deploy(_row({"run_id": "r1", "state": "running"})) is None
    failed = {"run_id": "r1", "state": "fail", "request_at": 5}
    assert deploy.wants_deploy(_row(failed)) is None
    assert deploy.wants_deploy(_row(failed, {"mode": "reinstall", "at": 9})) == "reinstall"
    assert deploy.wants_deploy(_row(failed, {"mode": "rerun", "at": 4})) is None   # already handled


def test_tile_phase_and_done_wait_for_deploy():
    st = _row({"run_id": "r1", "state": "running", "steps": {"install": {"status": "running"}}})
    assert not blades.deploy_finished(blades.deploy_slice(st))
    assert blades._deploy_steps(st)["install"] == "cur"
    assert set(blades._done_steps(st).values()) == {"pending"}
    st["deploy"] = {"run_id": "r1", "verdict": "pass", "state": "pass",
                    "steps": {p: {"status": "pass"} for p in deploy.PHASES},
                    "tail": {"identify": "done", "power_off": "done"}}
    assert blades.deploy_finished(blades.deploy_slice(st))
    assert blades._done_steps(st) == {"identify": "done", "power-off": "done", "done": "done"}
    # a run without Deploy: steps skip, Done as before
    old = _row(pending=False)
    assert set(blades._deploy_steps(old).values()) == {"skip"}


def test_finish_is_done_waits_for_deploy():
    run = {"verdict": "pass", "result": {"done": {"verdict": "pass", "deploy": "pending"}}}
    assert not finish.is_done(run)
    run["result"]["deploy"] = {"verdict": "skip"}
    assert finish.is_done(run)
    run["result"]["deploy"] = {"verdict": "fail"}
    assert not finish.is_done(run)
    assert finish.is_done({"verdict": "pass", "result": {"done": {"verdict": "pass"}}})


def test_ipmi_no_human_reset_while_deploying():
    row = {"power_on": "off", "done": {"verdict": "pass"}, "qual": {"run_id": "r1"},
           "deploy": {"state": "running"}}
    assert ipmi.clear_fields_for(row, "aa:bb", "on") == {}
    row["deploy"]["state"] = "pass"
    assert "done" in ipmi.clear_fields_for(row, "aa:bb", "on")


def test_install_passes_platform_and_autoinstall(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(install, "preflight", lambda root, ai: seen.setdefault("ai", ai) and None)
    monkeypatch.setattr(install.bmc, "power_status", lambda *a, **k: "off")
    monkeypatch.setattr(install.pxeboot, "write",
                        lambda h, **k: seen.update(platform=k["platform"]) or RungResult("fail", {"error": "x"}))
    install.install("b", "h", "rr1-et1b1", SimpleNamespace(bmc=[]), platform="leopard",
                    autoinstall="leopard/user-data")
    assert seen == {"ai": "leopard/user-data", "platform": "leopard"}


# ---- faked end-to-end ----
class Store:
    def __init__(self, node_vars=None, alldisks="/dev/sda ahci 0 rotational 480 ATA X 1 S9\n"):
        self.state, self.node, self.arts, self.alld = {}, dict(node_vars or {}), [], alldisks
        self.node.setdefault("result", {"run_id": "r1", "verdict": "pass"})

    def set_state(self, port, **kw):
        self.state.update(kw)

    def get_node(self, bmc):
        return self.node

    def upsert_node(self, bmc, **kw):
        self.node.update(kw)

    def update_result(self, bmc, res):
        self.node["result"] = res

    def write_artifact(self, *a, **k):
        self.arts.append(a[2])

    def get_artifact(self, bmc, rid, stage, name):
        return self.alld


def _node(tmp_path, monkeypatch, store, mode="auto", fail=None, **target):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "leopard.part").write_text("set node-setting leopard\n")
    (tmp_path / "leopard").mkdir()
    (tmp_path / "leopard" / "user-data").write_text("storage:\n  config:\n    - {path: /dev/sda,\n")
    calls = []

    def rung(name):
        def fn(*a, **k):
            calls.append(name)
            store.kw = {**getattr(store, "kw", {}), name: k}
            return RungResult("fail" if name == fail else "pass", {"headline": name})
        return fn
    monkeypatch.setattr(deploy.install, "install", rung("install"))
    monkeypatch.setattr(deploy.install, "firstboot", rung("firstboot"))
    monkeypatch.setattr(deploy.Node, "do_user", lambda self: (calls.append("flax-user"),
                        self.rung("flax-user", RungResult("pass", {})))[1])
    monkeypatch.setattr(deploy.Node, "do_iperf", lambda self: (calls.append("iperf"),
                        self.rung("deploy-iperf", RungResult("pass", {})))[1])
    monkeypatch.setattr(deploy.Node, "do_stress", lambda self: (calls.append("stress"),
                        self.rung("deploy-stress", RungResult("pass", {})))[1])
    monkeypatch.setattr(deploy.Node, "tail", lambda self: {"identify": "done", "power_off": "done"})
    cfg = deploy.Config(pxe_dir=str(tmp_path), install_root=str(tmp_path))
    t = {"port": "et7b1", "bmc_mac": "aa", "serial": "S1", "bmc_ip": "b", "host_ip": "h",
         "hostname": "rr033-et7b1", "family": "leopard", "run_id": "r1", **target}
    n = deploy.Node(t, mode, store=store, creds=deploy.Creds([("u", "p")], ("flax", "x")), cfg=cfg)
    n.run()
    return calls


def test_run_fresh_installs_and_records(tmp_path, monkeypatch):
    s = Store()
    calls = _node(tmp_path, monkeypatch, s)
    assert calls == ["install", "firstboot", "flax-user", "iperf", "stress"]
    assert s.state["deploy"]["verdict"] == "pass" and s.node["deployed"]["serial"] == "S1"
    assert s.node["result"]["deploy"]["verdict"] == "pass"


def test_do_firstboot_passes_family(tmp_path, monkeypatch):
    s = Store()
    _node(tmp_path, monkeypatch, s)
    assert s.kw["firstboot"]["family"] == "leopard"


def test_run_keeps_our_install(tmp_path, monkeypatch):
    s = Store({"deployed": {"serial": "S1", "installed_at": 1}})
    calls = _node(tmp_path, monkeypatch, s)
    assert calls[0] == "firstboot" and s.state["deploy"]["kept"] is True


def test_reinstall_wipes_even_ours(tmp_path, monkeypatch):
    s = Store({"deployed": {"serial": "S1", "installed_at": 1}})
    assert _node(tmp_path, monkeypatch, s, mode="reinstall")[0] == "install"


def test_fail_stops_and_fails_the_run(tmp_path, monkeypatch):
    s = Store()
    calls = _node(tmp_path, monkeypatch, s, fail="firstboot")
    assert calls == ["install", "firstboot"]
    d = s.state["deploy"]
    assert d["verdict"] == "fail" and d["steps"]["flax-user"]["status"] == "skip" and not d["tail"]
    assert s.node["result"]["verdict"] == "fail"


def test_no_system_storage_skips(tmp_path, monkeypatch):
    s = Store(alldisks="/dev/nvme0n1 nvme 0 rotational 960 X 1 S9\n")
    assert _node(tmp_path, monkeypatch, s) == []
    assert s.state["deploy"]["verdict"] == "skip" and s.state["deploy"]["tail"]


def test_bootdev_next_boot_only_for_deploy(monkeypatch):
    from flax_rungs import bmc
    sent = []
    monkeypatch.setattr(bmc, "_write", lambda ip, creds, args, **k: sent.append(args) or True)
    bmc.bootdev_pxe("b", [], persistent=False)
    bmc.bootdev_disk("b", [])
    assert sent == [["chassis", "bootdev", "pxe", "options=efiboot"],
                    ["chassis", "bootdev", "disk", "options=efiboot,persistent"]]


# ---- a diskless node (rabbit-edam et10b4, 2026-11-09): `alldisks -v` captured, but empty
NO_MEDIA = {"status": "skip", "summary": {"reason": "no physical storage media"}}


def test_an_empty_alldisks_is_no_storage_only_when_qualify_found_no_media():
    ok, why = deploy.has_system_storage("", "/dev/nvme0n1", no_media=True)
    assert ok is False and "no system storage" in why
    assert deploy.has_system_storage("", "/dev/nvme0n1")[0] is None          # uncorroborated
    assert deploy.has_system_storage(None, "/dev/nvme0n1", no_media=True)[0] is None   # absent


def test_target_carries_qualifys_no_media_finding():
    from flax_post import deployd
    row = {"bmc_mac": "aa", "qual": {"run_id": "r1", "steps": {"fio": NO_MEDIA, "smart-health": NO_MEDIA}}}
    assert deployd.target_for("et10b4", row, [])["no_media"] is True
    row["qual"]["steps"]["fio"] = {"status": "pass"}
    row["qual"]["steps"]["smart-health"] = {"status": "pass"}
    assert deployd.target_for("et10b4", row, [])["no_media"] is False


def test_a_diskless_node_skips_deploy_and_reaches_the_done_tail(tmp_path, monkeypatch):
    s = Store(alldisks="")
    assert _node(tmp_path, monkeypatch, s, no_media=True) == []
    d = s.state["deploy"]
    assert d["verdict"] == "skip" and "no system storage" in d["reason"] and d["tail"]
    assert s.node["result"]["verdict"] == "pass"


# ---- `plan` is a rung (operator 2026-11-09): Deploy can never fail before its first step
def _tile(dep):
    return _row(dict(dep, run_id="r1"))


def test_plan_is_the_first_deploy_rung():
    assert deploy.PHASES[0] == "plan" and blades.PHASE_STEPS["Deploy"][0] == "plan"


def test_a_plan_fail_is_a_red_plan_step(tmp_path, monkeypatch):
    s = Store()
    assert _node(tmp_path, monkeypatch, s, family="nope") == []
    d = s.state["deploy"]
    assert d["verdict"] == "fail" and d["steps"]["plan"]["status"] == "fail"
    assert "no Deploy platform" in d["steps"]["plan"]["reason"]
    assert d["steps"]["install"]["status"] == "skip"
    st = _tile(d)
    assert blades._deploy_steps(st)["plan"] == "fault"
    assert "no Deploy platform" in blades._deploy_notes(st)[1]["plan"]


def test_plan_passes_with_the_platform_and_disk(tmp_path, monkeypatch):
    s = Store()
    _node(tmp_path, monkeypatch, s)
    p = s.state["deploy"]["steps"]["plan"]
    assert p["status"] == "pass" and "leopard" in p["reason"] and "/dev/sda" in p["reason"]


def test_a_diskless_plan_passes_and_the_rest_skip(tmp_path, monkeypatch):
    s = Store(alldisks="")
    _node(tmp_path, monkeypatch, s, no_media=True)
    d = s.state["deploy"]
    assert d["steps"]["plan"]["status"] == "pass" and "no system storage" in d["steps"]["plan"]["reason"]
    steps = blades._deploy_steps(_tile(d))
    assert steps["plan"] == "done" and {steps[n] for n in deploy.PHASES[1:]} == {"skip"}


def test_the_plan_drawer_shows_what_plan_chose():
    from flax_post import stepinfo
    row = {"deploy": {"mode": "auto", "platform": "wiwynn-tp-hmc", "autoinstall": "wiwynn-tp/user-data",
                      "disk": "/dev/nvme0n1", "steps": {"plan": {"status": "fail", "reason": "x"}}}}
    rows, _notes = stepinfo._deploy({}, row, "plan", "fault")
    got = dict(rows)
    assert got["platform"] == "wiwynn-tp-hmc" and got["autoinstall"] == "wiwynn-tp/user-data"
    assert got["disk"] == "/dev/nvme0n1"


# ---- spec 2026-11-11 §5: the BMC boot override is cleared when Deploy ends ----
def _clear_runner(code):
    sent = []

    def run(argv, **kw):
        if argv[0] == "curl":
            sent.append(argv[argv.index("-d") + 1])
            return SimpleNamespace(returncode=0, stdout=code, stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")
    return run, sent


def _finish(tmp_path, monkeypatch, verdict, code):
    run, sent = _clear_runner(code)
    monkeypatch.setattr(deploy.Node, "tail", lambda self: {"identify": "done", "power_off": "done"})
    cfg = deploy.Config(pxe_dir=str(tmp_path), install_root=str(tmp_path))
    t = {"port": "et7b1", "bmc_mac": "aa", "serial": "S1", "bmc_ip": "b", "host_ip": "h",
         "hostname": "rr033-et7b1", "family": "tiogapass", "run_id": "r1"}
    s = Store()
    n = deploy.Node(t, "auto", store=s, cfg=cfg, runner=run,
                    creds=deploy.Creds([("u", "p")], ("flax", "x"), [("rf", "p")]))
    n.finish(verdict, "why")
    return s.state["deploy"], sent


@pytest.mark.parametrize("verdict", ["pass", "fail"])
def test_finish_clears_boot_override(tmp_path, monkeypatch, verdict):
    d, sent = _finish(tmp_path, monkeypatch, verdict, "204")
    assert len(sent) == 1
    assert json.loads(sent[0])["Boot"]["BootSourceOverrideEnabled"] == "Disabled"
    assert d["override_cleared"] == "yes" and d["verdict"] == verdict


@pytest.mark.parametrize("verdict", ["pass", "fail"])
def test_finish_clear_failure_keeps_verdict(tmp_path, monkeypatch, verdict):
    d, sent = _finish(tmp_path, monkeypatch, verdict, "500")
    assert d["verdict"] == verdict and d["override_cleared"].startswith("no (")


def test_stepinfo_firstboot_shows_override_cleared():
    from flax_post import stepinfo
    row = {"deploy": {"override_cleared": "yes", "steps": {"firstboot": {"rows": []}}}}
    rows, _ = stepinfo._deploy({}, row, "firstboot", "done")
    assert ["boot override cleared", "yes"] in rows
