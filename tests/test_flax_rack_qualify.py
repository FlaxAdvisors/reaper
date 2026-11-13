import json, threading, time
from unittest.mock import ANY
from pathlib import Path
import pytest
pytest.importorskip("flax_rack.qualify")
from flax_post import population
from flax_rack import qualify
from flax_rack.creds import Creds
from flax_rack.fakes import FakeRungs, with_wedge_ips, write_profiles
from flax_rack.geometry import load_geometry
from flax_rack.orderdb import OrderDB
from flax_rack.qualify import (AGENT_STAGES, device_rungs, iface_of, parse_ip_link,
                               qualify_passed, qualify_why, run_qualify)
from flax_rack.stages import OrderContext, run_discover
from flax_rungs.bmc import parse_mc_info
from flax_rungs.result import RungResult
from tests.test_flax_rack_geometry import FIX
from tests.test_flax_rack_stages import CREDS, FakeKea, fake_runner

HOSTS = ("L37", "C37", "R37", "L05", "R05")


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    d = tmp_path / "profiles"
    monkeypatch.setattr(population, "PROFILE_DIR", str(d))
    write_profiles(str(d), load_geometry(FIX))
    return d


def make_ctx(dir_, g=None, discover=True, keep_boots=False, **rk):
    g = with_wedge_ips(g or load_geometry(FIX))
    db = OrderDB(dir_ / "order.db"); db.init(g)
    c = OrderContext(dir_, g, db, FakeKea(), CREDS, fake_runner,
                     FakeRungs(g, **{"iperf_fail": {}, **rk}))
    if discover:                       # Qualify boots only what Discover proved
        run_discover(c)                # (and the JBOD counts via its Discover ladder)
        if not keep_boots:             # nodes rebooted since: Qualify boots them again
            c.rungs.forget_boots()
    return c


@pytest.fixture
def ctx(tmp_path, profiles):
    return make_ctx(tmp_path)


def v(ctx, pos, rung):
    return (ctx.db.latest_runs().get((pos, rung)) or {}).get("verdict")


def det(ctx, pos, rung):
    return ctx.db.latest_runs()[(pos, rung)]["detail"]


def test_happy_path_passes(ctx):
    run_qualify(ctx)
    assert qualify_passed(ctx.db, ctx.g) == (True, [])
    for pos in HOSTS:
        for rung in device_rungs(ctx.g.device(pos)):
            assert v(ctx, pos, rung) == "pass", (pos, rung, det(ctx, pos, rung))
    assert v(ctx, "_rack", "lldp-A") == v(ctx, "_rack", "lldp-B") == "pass"
    # every battery stage is its own rung with saved artifacts
    lr = ctx.db.latest_runs()
    inv = lr[("L37", "inventory")]
    assert inv["stage"] == "qualify" and set(inv["artifacts"]) >= {"macinv", "dmidecode"}
    p = ctx.dir / inv["artifacts"]["macinv"]
    assert p.read_text().startswith("2 Processor") and \
        inv["artifacts"]["macinv"] == "artifacts/L37/fake371/inventory/macinv.txt"
    assert set(AGENT_STAGES) <= {r for (pos, r) in lr if pos == "L37"}
    # the old Qualify fw-versions read is gone (the Firmware stage covers it)
    assert not any(r == "fw-versions" for (_pos, r) in lr)
    assert not any(c[0] == "mc_info" for c in ctx.rungs.calls)
    assert det(ctx, "L37", "population-check")["profile"] == "f4-compute-tp"
    # fio: the per-device summary from the artifact + the inventory's alldisks
    f = det(ctx, "L37", "fio")
    assert f["headline"] == "nvme0n1 812k IOPS · 3283 MB/s · 29.8 µs · +1 device · no baseline"
    assert [r[0] for r in f["table"]["rows"]] == ["nvme0n1", "nvme1n1"] * 3
    assert f["table"]["rows"][0][1].startswith("SAMSUNG_MZQL21T9HCJR-00A07")


def test_fabric_octet_from_address_plan_and_mesh_server(ctx):
    run_qualify(ctx)
    fab = {c[1]: c[2] for c in ctx.rungs.calls if c[0] == "apply_fabric"}
    assert fab["C37"] == {"ens3f0np0": "10.201.37.51/16", "ens3f1np1": "10.202.37.51/16"}
    assert fab["R05"] == {"ens3f0np0": "10.201.5.101/16", "ens3f1np1": "10.202.5.101/16"}
    (mesh,) = [c for c in ctx.rungs.calls if c[0] == "mesh"]
    assert mesh[1:3] == ("L05", "172.32.5.1") and "L05" not in mesh[3]
    assert sorted(mesh[3]) == ["C37", "L37", "R05", "R37"]


def test_pxe_live_failure_skips_later_rungs_rack_continues(ctx):
    ctx.rungs.fail_pxe = {"R37"}
    run_qualify(ctx)
    assert v(ctx, "R37", "pxe-live") == "fail"
    assert "no ssh" in det(ctx, "R37", "pxe-live")["error"]
    for rung in device_rungs(ctx.g.device("R37"))[1:-1]:
        assert v(ctx, "R37", rung) == "skip", rung
        assert det(ctx, "R37", rung)["reason"]
    # the failed boot's console is the evidence: captured, stopped after the boot phase
    assert v(ctx, "R37", "console") == "pass"
    calls = ctx.rungs.calls
    assert calls.index(("sol_start", "R37")) < calls.index(("boot_live", "R37", ANY)) and \
        calls.index(("sol_stop", "R37")) < next(i for i, c in enumerate(calls) if c[0] == "launch")
    assert ("launch", "R37") not in ctx.rungs.calls
    assert v(ctx, "L37", "iperf-mesh") == "pass"
    assert qualify_passed(ctx.db, ctx.g) == (False, ["R37"])


def test_server_not_live_leaves_every_client_unmeasured_never_meshes(ctx):
    ctx.rungs.fail_pxe = {"L05"}
    run_qualify(ctx)
    assert not [c for c in ctx.rungs.calls if c[0] == "mesh"]
    why = qualify_why(ctx.db.latest_runs(), ctx.g)
    for pos in ("L37", "R37", "R05"):
        # no server to measure against: nothing judged, grey, never a pass
        assert v(ctx, pos, "iperf-mesh") == "unmeasured"
        assert det(ctx, pos, "iperf-mesh")["headline"].startswith("server L05 not ready")
        assert why[pos] == "not measured: iperf-mesh"
    assert v(ctx, "C37", "iperf-mesh") == "unmeasured"
    assert set(qualify_passed(ctx.db, ctx.g)[1]) >= {"L37", "R37", "R05", "L05"}


def test_no_iperf_server_in_geometry(tmp_path, profiles):
    g = load_geometry(FIX); g.iperf_server = None
    c = make_ctx(tmp_path, g)
    run_qualify(c)
    assert det(c, "L37", "iperf-mesh")["error"] == "no iperf_server in geometry.json"


def test_pop_missing_profile_fails(ctx):
    ctx.db.set_pop("L37", "f4-nosuch")
    run_qualify(ctx, positions=["L37"])
    assert v(ctx, "L37", "population-check") == "fail"
    assert det(ctx, "L37", "population-check")["error"] == "no profile f4-nosuch"


def test_pop_empty_profile_and_rule_miss_fail(ctx, profiles):
    (profiles / "empty").write_text("# nothing\n")
    ctx.db.set_pop("L37", "empty")
    (profiles / "big").write_text("24 Memory Size: 64 GB\n2 Processor Version: .*Gold 6138\n")
    ctx.db.set_pop("C37", "big")
    run_qualify(ctx, positions=["L37", "C37"])
    assert det(ctx, "L37", "population-check")["error"] == "profile empty has no rules"
    d = det(ctx, "C37", "population-check")
    assert v(ctx, "C37", "population-check") == "fail" and [r["ok"] for r in d["results"]] == [False, True]


def test_positions_rerun_only_those_plus_server(ctx):
    run_qualify(ctx)
    ctx.rungs.calls.clear()
    run_qualify(ctx, positions=["C37"])
    assert {c[1] for c in ctx.rungs.calls if c[0] == "boot_live"} == {"C37"}
    assert ctx.db.run_count("C37", "pxe-live") == 2 and ctx.db.run_count("L37", "pxe-live") == 1
    assert ctx.db.run_count("L05", "fabric-addr") == 2      # the server's addresses re-applied
    assert ctx.db.run_count("L37", "fabric-addr") == 1
    (mesh,) = [c for c in ctx.rungs.calls if c[0] == "mesh"]
    assert mesh[3] == ["C37"]
    with pytest.raises(ValueError):
        run_qualify(ctx, positions=["JBOD01"])


def test_agent_stage_list_mismatch_is_a_failed_rung(ctx):
    base = ctx.rungs.poll

    def poll(ip, **k):
        out = base(ip, **k)
        if "fio" in out:                    # the storage phase
            out.pop("fio")
            out["iperf"] = RungResult("pass", {"stage": "iperf"})
        return out
    ctx.rungs.poll = poll
    run_qualify(ctx, positions=["L37"])
    e = det(ctx, "L37", "agent-stages")["error"]
    assert v(ctx, "L37", "agent-stages") == "fail" and "missing fio" in e and "extra iperf" in e
    assert "storage list" in e and det(ctx, "L37", "agent-stages")["phase"] == "storage"
    assert det(ctx, "L37", "fio")["error"] == "agent did not report fio"
    assert ("L37", "iperf") not in ctx.db.latest_runs()
    assert v(ctx, "L37", "inventory") == "pass"
    assert det(ctx, "L37", "cpu-mem-stress")["reason"] == "failed in phase storage"


def test_agent_unhealthy_skips_battery(ctx):
    ctx.rungs.poll = lambda ip, **k: {"agent": RungResult("fail", {"error": "agent unhealthy: unknown stage x"})}
    run_qualify(ctx, positions=["L37"])
    assert "unknown stage" in det(ctx, "L37", "agent-stages")["error"]
    assert v(ctx, "L37", "inventory") == "skip"
    assert det(ctx, "L37", "population-check")["error"] == "no macinv from the inventory stage"


def test_unsafe_artifact_names_dropped(ctx):
    base = ctx.rungs.poll

    def poll(ip, **k):
        out = base(ip, **k)
        if "sdr-pre" in out:
            out["sdr-pre"].artifacts = {"../../evil": "x", "sdr": "ok"}
        return out
    ctx.rungs.poll = poll
    run_qualify(ctx, positions=["L37"])
    r = ctx.db.latest_runs()[("L37", "sdr-pre")]
    assert list(r["artifacts"]) == ["sdr"] and r["detail"]["dropped_artifacts"] == ["../../evil"]
    assert not list(ctx.dir.rglob("evil*"))


def test_wedge_unreadable_fails_ha_fabric_uses_seen_side(ctx):
    base = ctx.rungs.read_lldp

    def rl(ip, nos, cred, runner=None):
        if ip.endswith("-B"):
            return RungResult("fail", {"error": "ssh denied"})
        return base(ip, nos, cred, runner=runner)
    ctx.rungs.read_lldp = rl
    run_qualify(ctx, positions=["L37"])
    assert v(ctx, "_rack", "lldp-B") == "fail"
    assert det(ctx, "L37", "ha-path")["error"].startswith("LLDP unreadable on B")
    fab = [c for c in ctx.rungs.calls if c[0] == "apply_fabric" and c[1] == "L37"][-1]
    assert fab[2] == {"ens3f0np0": "10.201.37.1/16"}


def test_lldp_artifact_saved_not_inline(ctx):
    run_qualify(ctx, positions=["L37"])
    r = ctx.db.latest_runs()[("_rack", "lldp-A")]
    rel = r["artifacts"]["show-lldp-table"]
    assert rel.startswith("artifacts/_rack/q") and (ctx.dir / rel).is_file()


def test_closed_order_refused(ctx):
    ctx.db.set_closed()
    with pytest.raises(RuntimeError):
        run_qualify(ctx)


def test_two_racks_qualify_concurrently_each_own_server(tmp_path, profiles):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    geo = json.loads(Path(FIX).read_text())
    geo.update(rack="b", iperf_server="R05")
    geo["network"].update(vid=33, cidr="172.33.0.0/16", prefix="rr101b-")
    gb = load_geometry_from(tmp_path / "gb.json", geo)
    ca, cb = make_ctx(a, delay_s=0.05), make_ctx(b, gb, delay_s=0.05)
    ts = [threading.Thread(target=run_qualify, args=(c,)) for c in (ca, cb)]
    t0 = time.monotonic()
    for t in ts: t.start()
    for t in ts: t.join(30)
    assert time.monotonic() - t0 < 30
    (ma,) = [c for c in ca.rungs.calls if c[0] == "mesh"]
    (mb,) = [c for c in cb.rungs.calls if c[0] == "mesh"]
    assert ma[1:3] == ("L05", "172.32.5.1") and mb[1:3] == ("R05", "172.33.5.101")
    assert v(ca, "L37", "iperf-mesh") == v(cb, "L37", "iperf-mesh") == "pass"


def load_geometry_from(path, data):
    path.write_text(json.dumps(data))
    return load_geometry(path)


def test_device_threads_bounded(tmp_path, profiles, monkeypatch):
    monkeypatch.setattr(qualify, "MAX_PARALLEL", 2)
    c = make_ctx(tmp_path)
    live, peak, mu = [0], [0], threading.Lock()
    base = c.rungs.boot_live

    def bl(*a, **k):
        with mu:
            live[0] += 1; peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with mu:
            live[0] -= 1
        return base(*a, **k)
    c.rungs.boot_live = bl
    run_qualify(c)
    assert peak[0] == 2


# ---------------------------------------------------------------- identity gate
def test_no_discover_never_boots(tmp_path, profiles):
    c = make_ctx(tmp_path, discover=False)
    run_qualify(c)
    assert not [x for x in c.rungs.calls if x[0] in ("boot_live", "launch", "mesh")]
    for pos in HOSTS:
        assert v(c, pos, "pxe-live") == "fail"
        assert det(c, pos, "pxe-live")["error"] == ("discover first: bmc-reserved not run, "
            "bmc-leased not run, bmc-pinged not run, host-reserved not run")
        assert v(c, pos, "agent-reachable") == "skip"


def test_failed_reserve_or_reach_gates_that_device_only(ctx):
    for pos, rung in (("L37", "bmc-reserved"), ("C37", "bmc-leased")):
        ctx.db.finish_run(ctx.db.start_run(pos, "discover", rung),
                          RungResult("fail", {"error": "x"}))
    run_qualify(ctx)
    booted = {x[1] for x in ctx.rungs.calls if x[0] == "boot_live"}
    assert booted == {"R37", "L05", "R05"}
    assert det(ctx, "L37", "pxe-live")["error"] == "discover first: bmc-reserved fail"
    assert det(ctx, "C37", "pxe-live")["error"] == "discover first: bmc-leased fail"
    assert v(ctx, "L37", "iperf-mesh") == "skip" and v(ctx, "R37", "iperf-mesh") == "pass"


def test_no_host_mac_gates(ctx):
    ctx.db.set_macs("R37", host_mac=None, bmc_mac=None, iom_macs=[])
    run_qualify(ctx, positions=["R37"])
    assert det(ctx, "R37", "pxe-live")["error"] == "discover first: no host MAC matched"
    assert not [x for x in ctx.rungs.calls if x[0] == "boot_live"]


def test_boot_live_gets_the_discovered_host_mac(ctx):
    run_qualify(ctx, positions=["L37"])
    (call,) = [x for x in ctx.rungs.calls if x[0] == "boot_live"]
    row = next(x for x in ctx.db.devices() if x["pos"] == "L37")
    assert call[2] == row["host_mac"] and call[2]


def test_wrong_host_runs_nothing_more_on_it(ctx):
    ctx.rungs.wrong_host = {"R37"}
    run_qualify(ctx)
    assert det(ctx, "R37", "pxe-live")["error"].startswith("wrong host at 172.32.37.101: BOOTIF")
    assert not [x for x in ctx.rungs.calls if x[0] in ("launch", "poll", "apply_fabric")
                and x[1] == "R37"]
    for rung in ("agent-reachable", "population-check", "ha-path", "fabric-addr", "iperf-mesh"):
        assert v(ctx, "R37", rung) == "skip", rung
    (mesh,) = [x for x in ctx.rungs.calls if x[0] == "mesh"]
    assert "R37" not in mesh[3]


# ---------------------------------------------------------------- phase deadlines
def test_phase_deadlines(ctx, monkeypatch):
    monkeypatch.delenv("FLAX_STRESS_SECONDS", raising=False)
    run_qualify(ctx, positions=["L37"])
    dl = {x[3]: x[2] for x in ctx.rungs.calls if x[0] == "poll"}
    inv, sto, burn = (",".join(qualify.PHASE_STAGES[p]) for p in ("inventory", "storage", "burn"))
    assert dl == {inv: 1200, sto: 120 + 120 * 2, burn: 600 + 900}   # canned lsblk: 2 NVMe
    lr = ctx.db.latest_runs()
    assert lr[("L37", "agent-stages")]["detail"]["phase"] == "burn"
    assert lr[("L37", "agent-stages")]["detail"]["deadline_s"] == 1500


def test_storage_deadline_unknown_disks_and_stress_env(ctx, monkeypatch):
    monkeypatch.setenv("FLAX_STRESS_SECONDS", "60")
    base = ctx.rungs.poll

    def poll(ip, **k):
        out = base(ip, **k)
        if "inventory" in out:
            out["inventory"].artifacts.pop("lsblk")
        return out
    ctx.rungs.poll = poll
    seen = []
    ctx.db.finish_run = (lambda f: lambda rid, r: (seen.append(r), f(rid, r))[1])(ctx.db.finish_run)
    run_qualify(ctx, positions=["L37"])
    dl = [x[2] for x in ctx.rungs.calls if x[0] == "poll"]
    assert dl == [1200, 120 + 120 * 8, 60 + 900]
    sto = next(r.detail for r in seen if r.detail.get("phase") == "storage"
               and "deadline_s" in r.detail)
    assert sto["disks"] == 8 and sto["disks_from"].startswith("unknown")


def test_fio_disks_mirrors_the_battery_rule():
    from flax_rack.qualify import fio_disks
    t = ("NAME        MAJ:MIN RM   SIZE RO TYPE MOUNTPOINTS\n"
         "loop0         7:0    0 850.2M  1 loop /run/overlay\n"
         "sda           8:0    1  14.6G  0 disk /run/initramfs/live\n"   # the boot stick: mounted
         "sdb           8:16   0   7.3T  0 disk \n"
         "├─sdb1        8:17   0   512M  0 part /boot\n"                 # only a partition mounted
         "nvme0n1     259:0    0   1.7T  0 disk \n"
         "sr0          11:0    1  1024M  0 rom  \n")
    assert fio_disks(t) == 2
    assert fio_disks("NAME MAJ:MIN RM SIZE RO TYPE MOUNTPOINTS\nloop0 7:0 0 1G 1 loop /x\n") == 0
    assert fio_disks(None) is None and fio_disks("lsblk: command not found\n") is None


# ---------------------------------------------------------------- lockstep phases
def test_order_stages_is_the_union_of_the_phase_lists():
    from flax_rungs.agent import ORDER_STAGES
    union = [s for p in ("inventory", "storage", "burn") for s in qualify.PHASE_STAGES[p]]
    assert union == ORDER_STAGES.split(",") == list(AGENT_STAGES)
    assert qualify.PHASES == ("boot", "inventory", "storage", "burn", "network")


def _logged(ctx, slow=None, delay=0.05):
    """Wrap the fake rungs to log (what, phase-stages, pos) at start AND end of each call."""
    log, mu = [], threading.Lock()
    R = ctx.rungs

    def wrap(name, fn, key):
        def w(*a, **k):
            with mu:
                log.append(("start", name, key(a, k)))
            out = fn(*a, **k)
            if slow and slow[0] == name and slow[1] == key(a, k)[1]:
                time.sleep(delay)
            with mu:
                log.append(("end", name, key(a, k)))
            return out
        return w
    pos = lambda ip: R.pos_of.get(ip)
    R.launch = wrap("launch", R.launch, lambda a, k: (k.get("stages"), pos(a[0])))
    R.poll = wrap("poll", R.poll, lambda a, k: (",".join(R.stages_of[pos(a[0])]), pos(a[0])))
    R.read_lldp = wrap("lldp", R.read_lldp, lambda a, k: (None, a[0]))
    return log


def test_phases_strictly_ordered_slow_device_holds_the_barrier(ctx):
    # R05's inventory poll is slow: no storage step may start before it ended
    log = _logged(ctx, slow=("poll", "R05"), delay=0.3)
    run_qualify(ctx)
    lists = [",".join(qualify.PHASE_STAGES[p]) for p in ("inventory", "storage", "burn")]
    idx = lambda what, name, stages: [i for i, e in enumerate(log)
                                      if e[0] == what and e[1] == name and e[2][0] == stages]
    for a, b in zip(lists, lists[1:]):
        assert max(idx("end", "poll", a)) < min(idx("start", "launch", b)), (a, b)
    first_lldp = min(i for i, e in enumerate(log) if e[1] == "lldp")
    assert max(idx("end", "poll", lists[2])) < first_lldp
    assert len(idx("start", "launch", lists[1])) == 5
    assert qualify_passed(ctx.db, ctx.g) == (True, [])


def test_burn_launched_everywhere_before_any_burn_poll(ctx):
    run_qualify(ctx)
    burn = ",".join(qualify.PHASE_STAGES["burn"])
    calls = ctx.rungs.calls
    launches = [i for i, c in enumerate(calls) if c[0] == "launch" and c[2] == burn]
    polls = [i for i, c in enumerate(calls) if c[0] == "poll" and c[3] == burn]
    assert len(launches) == 5 and max(launches) < min(polls)
    ev = [e["text"] for e in ctx.db.events(100) if e["text"].startswith("burn:")]
    assert len(ev) == 1 and ev[0].startswith("burn: stress launched on 5 node(s) within ")


def test_slow_device_does_not_hold_back_the_others_results(ctx):
    gate, entered = threading.Event(), threading.Event()
    base = ctx.rungs.poll

    def poll(ip, **k):
        if ctx.rungs.pos_of[ip] == "R05" and "inventory" in ctx.rungs.stages_of["R05"]:
            entered.set()
            assert gate.wait(10)
        return base(ip, **k)
    ctx.rungs.poll = poll
    t = threading.Thread(target=run_qualify, args=(ctx,))
    t.start()
    try:
        assert entered.wait(10)
        end = time.monotonic() + 10
        while time.monotonic() < end and not all(v(ctx, p, "population-check") for p in ("L37", "C37", "R37", "L05")):
            time.sleep(0.02)
        for p in ("L37", "C37", "R37", "L05"):          # recorded while R05 still runs
            assert v(ctx, p, "inventory") == "pass" and v(ctx, p, "population-check") == "pass", p
        assert v(ctx, "R05", "inventory") is None
        assert not [c for c in ctx.rungs.calls if c[0] == "launch" and c[2] == "fio"]
        pg = ctx.db.qualify_progress()
        assert pg["phase"] == "inventory" and pg["n"] == 2 and pg["of"] == 5
        assert pg["done"] == 4 and pg["waiting_on"] == ["R05"] and pg["devices"] == 5
    finally:
        gate.set()
        t.join(30)
    assert qualify_passed(ctx.db, ctx.g) == (True, [])
    assert ctx.db.qualify_progress() is None            # cleared when the stage ends


def test_failed_device_skipped_in_later_phases(ctx):
    base = ctx.rungs.poll

    def poll(ip, **k):
        out = base(ip, **k)
        if ctx.rungs.pos_of[ip] == "R37" and "fio" in out:
            out["fio"] = RungResult("fail", {"stage": "fio", "error": "rc=1"})
        return out
    ctx.rungs.poll = poll
    run_qualify(ctx)
    assert v(ctx, "R37", "fio") == "fail"
    for rung in (*qualify.PHASE_STAGES["burn"], "ha-path", "fabric-addr", "iperf-mesh"):
        assert v(ctx, "R37", rung) == "skip", rung
        assert det(ctx, "R37", rung)["reason"] == "failed in phase storage"
    burn = ",".join(qualify.PHASE_STAGES["burn"])
    assert {c[1] for c in ctx.rungs.calls if c[0] == "launch" and c[2] == burn} == \
        {"L37", "C37", "L05", "R05"}
    (mesh,) = [c for c in ctx.rungs.calls if c[0] == "mesh"]
    assert "R37" not in mesh[3] and v(ctx, "L37", "iperf-mesh") == "pass"
    assert any("phase storage: 4 passed, failed: R37" == e["text"] for e in ctx.db.events(100))
    assert qualify_passed(ctx.db, ctx.g) == (False, ["R37"])


def test_pop_fail_never_gates_later_phases(ctx, profiles):
    # RRORD-101: the iperf server L05 has no f4-bc-head-25g profile yet
    (profiles / "f4-bc-head-25g").unlink()
    run_qualify(ctx)
    assert det(ctx, "L05", "population-check")["error"] == "no profile f4-bc-head-25g"
    for rung in ("fio", *qualify.PHASE_STAGES["burn"], "ha-path", "fabric-addr", "iperf-mesh"):
        assert v(ctx, "L05", rung) == "pass", rung
    (mesh,) = [c for c in ctx.rungs.calls if c[0] == "mesh"]
    assert mesh[1] == "L05" and v(ctx, "L37", "iperf-mesh") == "pass"
    assert qualify_passed(ctx.db, ctx.g) == (False, ["L05", "R05"])   # both BC heads
    # a later --pos run still takes L05 as the server (pop does not make it not-ready)
    ctx.rungs.calls.clear()
    run_qualify(ctx, positions=["L37"])
    (mesh,) = [c for c in ctx.rungs.calls if c[0] == "mesh"]
    assert mesh[1:4] == ("L05", "172.32.5.1", ["L37"])


def test_old_fw_versions_rows_are_harmless(ctx):
    """A pre-removal order.db fw-versions row stays: never re-recorded, never gates."""
    rid = ctx.db.start_run("L37", "firmware", "fw-versions")
    ctx.db.finish_run(rid, RungResult("fail", {"error": "unreadable: BIOS version"}))
    run_qualify(ctx, positions=["L37"])
    assert v(ctx, "L37", "fio") == "pass"
    assert "fw-versions" not in qualify_why(ctx.db.latest_runs(), ctx.g).get("L37", "")


def test_launch_failure_fails_that_phase_only_for_that_device(ctx):
    base = ctx.rungs.launch

    def launch(ip, u, p, **k):
        if ctx.rungs.pos_of[ip] == "C37" and k["stages"] == "smart-health,fio":
            return RungResult("fail", {"error": "agent launch rc=255"})
        return base(ip, u, p, **k)
    ctx.rungs.launch = launch
    run_qualify(ctx)
    assert v(ctx, "C37", "agent-reachable") == "fail"
    assert det(ctx, "C37", "agent-reachable")["phase"] == "storage"
    assert det(ctx, "C37", "fio")["reason"] == "agent-reachable failed"
    assert det(ctx, "C37", "cpu-mem-stress")["reason"] == "failed in phase storage"
    assert v(ctx, "R37", "cpu-mem-stress") == "pass"


def test_positions_rerun_repeats_every_phase(ctx):
    run_qualify(ctx)
    ctx.rungs.calls.clear()
    run_qualify(ctx, positions=["C37"])
    assert [c[2] for c in ctx.rungs.calls if c[0] == "launch"] == \
        [",".join(qualify.PHASE_STAGES[p]) for p in ("inventory", "storage", "burn")]
    assert {c[1] for c in ctx.rungs.calls if c[0] in ("launch", "poll", "boot_live")} == {"C37"}


def test_poll_timeout_stops_agent_before_mesh(ctx):
    ctx.rungs.poll_timeout = {"R37"}
    run_qualify(ctx)
    calls = [x[:2] for x in ctx.rungs.calls]
    assert ("stop_agent", "R37") in calls
    assert calls.index(("stop_agent", "R37")) < calls.index(next(x for x in calls if x[0] == "mesh"))
    assert [x for x in calls if x[0] == "stop_agent"] == [("stop_agent", "R37")]
    d = det(ctx, "R37", "agent-stages")
    assert v(ctx, "R37", "agent-stages") == "fail" and d["agent_stop"] == "stopped"
    assert d["error"] == "burn phase timed out after 1500s; agent stopped"
    assert v(ctx, "R37", "cpu-mem-stress") == "fail" and v(ctx, "R37", "mem-pre") == "pass"
    # the watchdog is shown on the stage the agent was in, with its log as an artifact
    cms = ctx.db.latest_runs()[("R37", "cpu-mem-stress")]
    assert cms["detail"]["error"] == d["error"] and "agent-watchdog" in cms["artifacts"]
    assert "agent-watchdog" not in ctx.db.latest_runs()[("R37", "mem-post")]["artifacts"]
    assert v(ctx, "R37", "sdr-pre") == v(ctx, "R37", "fio") == "pass"
    (mesh,) = [x for x in ctx.rungs.calls if x[0] == "mesh"]
    assert "R37" not in mesh[3]             # failed the burn phase: out of the network one
    assert det(ctx, "R37", "iperf-mesh")["reason"] == "failed in phase burn"


def test_poll_timeout_stop_failed_sits_out_the_mesh(ctx):
    ctx.rungs.poll_timeout = ctx.rungs.stop_fail = {"R37"}
    run_qualify(ctx)
    assert "NOT stopped" in det(ctx, "R37", "agent-stages")["error"]
    (mesh,) = [x for x in ctx.rungs.calls if x[0] == "mesh"]
    assert "R37" not in mesh[3]
    assert det(ctx, "R37", "iperf-mesh")["reason"] == "qual agent still running (stop failed)"
    assert not [x for x in ctx.rungs.calls if x[0] == "apply_fabric" and x[1] == "R37"]


def test_server_stop_failed_fails_every_client(ctx):
    ctx.rungs.poll_timeout = ctx.rungs.stop_fail = {"L05"}
    run_qualify(ctx)
    assert not [x for x in ctx.rungs.calls if x[0] == "mesh"]
    assert det(ctx, "L37", "iperf-mesh")["error"] == \
        "server L05 not ready: its qual agent is still running (stop failed)"


# ---------------------------------------------------------------- parsers
def test_parse_ip_link_both_forms():
    d = ("1: lo: <LOOPBACK,UP> mtu 65536\n    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00\n"
         "2: eno1: <UP> mtu 1500\n    link/ether 0C:42:A1:E1:6B:50 brd ff:ff:ff:ff:ff:ff promiscuity 0\n"
         "3: ens3f0np0@if9: <UP> mtu 9000\n    link/ether 88:e9:a4:00:00:01 brd ff:ff:ff:ff:ff:ff\n")
    assert parse_ip_link(d) == {"eno1": "0c:42:a1:e1:6b:50", "ens3f0np0": "88:e9:a4:00:00:01"}
    o = "2: eno1: <UP> mtu 1500 qdisc mq\\    link/ether 0c:42:a1:e1:6b:50 brd ff:ff:ff:ff:ff:ff\n"
    assert parse_ip_link(o) == {"eno1": "0c:42:a1:e1:6b:50"}


def test_iface_of():
    d = ("2: eno1: <UP>\n    link/ether aa\n    inet 172.32.37.1/16 brd x scope global eno1\n"
         "3: eno2: <UP>\n    inet 172.32.37.10/16 scope global eno2\n")
    assert iface_of(d, "172.32.37.1") == "eno1" and iface_of(d, "172.32.37.10") == "eno2"
    assert iface_of("2: eth0    inet 172.32.5.1/16 brd x scope global eth0\n", "172.32.5.1") == "eth0"
    assert iface_of(d, "172.32.37.2") is None


def test_parse_mc_info():
    mc = ("Device ID                 : 32\nFirmware Revision         : 2.61\n"
          "IPMI Version              : 2.0\nAux Firmware Rev Info     : \n    0x00\n    0x11\n"
          "Device Available          : yes\n")
    assert parse_mc_info(mc) == {"firmware": "2.61", "aux": "0x00 0x11"}


def test_qualify_cli(tmp_path, profiles, monkeypatch, capsys):
    from flax_rack import __main__ as cli, server
    d = tmp_path / "o"; d.mkdir()
    (d / "geometry.json").write_text(Path(FIX).read_text())
    monkeypatch.setattr(server, "default_context_factory",
                        lambda rd, g, db: make_ctx(rd))
    monkeypatch.chdir(d)
    rc = cli.main(["qualify", "--pos", "L37", "C37"])
    out = capsys.readouterr().out
    assert "L37      pxe-live       pass" in out and "R37" not in out.split("qualify:")[0]
    assert rc == 1 and "not passed" in out           # the rest of the rack never ran


def test_qualify_cli_rack_wide_needs_the_order_id(tmp_path, profiles, monkeypatch, capsys):
    from flax_rack import __main__ as cli, server
    d = tmp_path / "o"; d.mkdir()
    (d / "geometry.json").write_text(Path(FIX).read_text())
    ran = []
    monkeypatch.setattr(server, "default_context_factory",
                        lambda rd, g, db: ran.append(1) or make_ctx(rd))
    monkeypatch.chdir(d)
    with pytest.raises(SystemExit, match="--confirm <order id>"):
        cli.main(["qualify"])
    with pytest.raises(SystemExit, match="must be the order id RRORD-101"):
        cli.main(["qualify", "--confirm", "rrord-101"])
    assert ran == []                                 # nothing was built, nothing booted
    cli.main(["qualify", "--confirm", "RRORD-101"])
    out = capsys.readouterr().out
    assert "R37      pxe-live       pass" in out and "L37      pxe-live       pass" in out


def test_qualify_cli_has_no_agent_deadline(tmp_path, monkeypatch):
    from flax_rack import __main__ as cli
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):         # per-phase deadlines replaced the override
        cli.main(["qualify", "--agent-deadline", "7200"])


# ---------------------------------------------------------------- fix wave: cancel
def test_cancel_during_boot_stops_at_the_barrier(ctx, monkeypatch):
    real = ctx.rungs.boot_live

    def boot(*a, **k):                   # Cancel pressed while L37 boots
        if ctx.rungs.pos_of.get(a[1]) == "L37":
            ctx.cancel.set()
        return real(*a, **k)
    ctx.rungs.boot_live = boot
    monkeypatch.setattr(qualify, "MAX_PARALLEL", 1)          # one boot at a time
    run_qualify(ctx)
    booted = [c[1] for c in ctx.rungs.calls if c[0] == "boot_live"]
    assert booted[-1] == "L37"                   # the running boot finished, none after
    for pos in HOSTS:
        if pos not in booted:
            assert det(ctx, pos, "pxe-live")["reason"] == "cancelled"
        for rung in device_rungs(ctx.g.device(pos))[1:-1]:      # console: its SOL capture
            assert v(ctx, pos, rung) == "skip" and det(ctx, pos, rung)["reason"] == "cancelled", \
                (pos, rung)
    assert not [c for c in ctx.rungs.calls if c[0] in ("launch", "mesh", "read_lldp")]
    assert any(e["text"].startswith("qualify cancelled") for e in ctx.db.events(30))


# ---------------------------------------------------------------- server
from tests.test_flax_rack_server import RACK, Factory, _order_dir, _start, _wait_idle, get, get_json, post


class QFactory(Factory):
    """Discover as in the server tests (gate-able); Qualify on FakeRungs."""
    def __call__(self, rack_dir, g, db):
        c = super().__call__(rack_dir, g, db)
        c.g = with_wedge_ips(g)
        c.creds = CREDS
        c.rungs = FakeRungs(c.g, iperf_fail={"C37": "B"}, fdb_runner=c.rungs.fdb_runner)
        return c


@pytest.fixture
def qsrv(tmp_path, profiles):
    _order_dir(tmp_path)
    f = QFactory()
    s = _start(tmp_path, f)
    s.factory = f
    yield s
    if f.gate is not None:
        f.gate.set()
    s.shutdown(); s.server_close()


def test_qualify_refused_while_discover_runs(qsrv):
    qsrv.factory.gate = threading.Event()
    assert post(qsrv, "/rr101/api/stage/discover", RACK).status == 202
    assert qsrv.factory.entered.wait(5)
    r = post(qsrv, "/rr101/api/stage/qualify", {"confirm": "RRORD-101"})
    assert r.status == 409 and b"discover" in r.body
    assert get_json(qsrv, "/rr101/api/state")["stage"] == "discover"
    qsrv.factory.gate.set()
    _wait_idle(qsrv)


def _discover(qsrv):
    assert post(qsrv, "/rr101/api/stage/discover", RACK).status == 202
    _wait_idle(qsrv)


def test_qualify_route_needs_confirm(qsrv):
    for body in ({}, {"confirm": "rrord-101"}, {"confirm": True}, {"confirm": ""}):
        r = post(qsrv, "/rr101/api/stage/qualify", body)
        assert r.status == 400 and b"order id" in r.body, body
    for body in ({"positions": ["R37"]}, {"positions": ["R37"], "confirm": "RRORD-101"},
                 {"positions": ["R37"], "confirm": 1}):
        r = post(qsrv, "/rr101/api/stage/qualify", body)
        assert r.status == 400 and b"confirm" in r.body, body
    s = get_json(qsrv, "/rr101/api/state")
    assert not any("pxe-live" in d["runs"] for d in s["devices"])   # nothing started


def test_qualify_route_state_and_artifacts(qsrv, tmp_path):
    ok = {"confirm": True}
    assert post(qsrv, "/rr101/api/stage/qualify", {"positions": ["JBOD01"], **ok}).status == 400
    assert post(qsrv, "/rr101/api/stage/qualify", {"positions": "L37", **ok}).status == 400
    _discover(qsrv)
    r = post(qsrv, "/rr101/api/stage/qualify", {"confirm": "RRORD-101"})
    assert r.status == 202, r.body
    _wait_idle(qsrv)
    s = get_json(qsrv, "/rr101/api/state")
    assert s["qualify"]["passed"] is False and "C37" in s["qualify"]["not_passed"]
    assert s["agent_stages"] == list(AGENT_STAGES) and s["firmware_old_rungs"] == []
    assert s["firmware_rungs"] == ["bmc-checked", "bmc-updated", "bmc-backup-checked",
                                   "bios-checked", "bios-updated", "mlx-checked", "mlx-updated"]
    assert s["qualify_phases"] == list(qualify.PHASES) and s["qualify"]["progress"] is None
    c37 = next(d for d in s["devices"] if d["pos"] == "C37")
    assert c37["runs"]["iperf-mesh"]["verdict"] == "fail"
    assert "B:" in c37["runs"]["iperf-mesh"]["detail"]["error"]
    assert c37["qualify_rungs"][0] == "pxe-live" and "bmc_live" in c37
    assert "lldp" not in s["rack_runs"]["lldp-A"]["detail"]
    rel = c37["runs"]["inventory"]["artifacts"]["macinv"]
    a = get(qsrv, f"/rr101/{rel}")
    assert a.status == 200 and a.body.startswith(b"2 Processor") and \
        a.headers["Content-Type"].startswith("text/plain") and \
        a.headers["X-Content-Type-Options"] == "nosniff"
    # read-only, path-checked
    (tmp_path / "RRORD-101" / "secret.txt").write_text("no")
    for bad in ("artifacts/C37/../../secret.txt", "artifacts/C37/x/inventory/../../../secret.txt",
                "artifacts/%2e%2e/x/y/z.txt", "artifacts/C37/x/y", "artifacts/order.db",
                "artifacts/C37/fake372/inventory/macinv.json"):
        assert get(qsrv, f"/rr101/{bad}").status == 404, bad
    link = tmp_path / "RRORD-101" / "artifacts" / "C37" / "lnk" / "inventory"
    link.mkdir(parents=True)
    (link / "x.txt").symlink_to(tmp_path / "RRORD-101" / "secret.txt")
    assert get(qsrv, "/rr101/artifacts/C37/lnk/inventory/x.txt").status == 404
    assert post(qsrv, f"/rr101/{rel}", {}).status == 404


def test_qualify_positions_route(qsrv):
    _discover(qsrv)
    r = post(qsrv, "/rr101/api/stage/qualify", {"positions": ["R37"], "confirm": True})
    assert r.status == 202 and json.loads(r.body)["positions"] == ["R37"]
    _wait_idle(qsrv)
    s = get_json(qsrv, "/rr101/api/state")
    l37 = next(d for d in s["devices"] if d["pos"] == "L37")
    r37 = next(d for d in s["devices"] if d["pos"] == "R37")
    assert "pxe-live" not in l37["runs"] and r37["runs"]["population-check"]["verdict"] == "pass"


def test_progress_in_state_mid_run_and_stale_one_cleared(qsrv, tmp_path):
    from flax_rack.orderdb import OrderDB
    assert get_json(qsrv, "/rr101/api/state")["qualify"]["progress"] is None
    db = OrderDB(tmp_path / "RRORD-101" / "order.db")
    db.set_qualify_progress({"phase": "storage", "n": 3, "of": 5, "devices": 5, "done": 3,
                             "waiting_on": ["R05", "L05"]})
    s = get_json(qsrv, "/rr101/api/state")
    assert s["qualify"]["progress"]["phase"] == "storage"
    from flax_rack.server import Rack
    from flax_rack.geometry import load_geometry
    Rack(tmp_path / "RRORD-101", load_geometry(tmp_path / "RRORD-101" / "geometry.json"))
    assert db.qualify_progress() is None    # a new daemon: no Qualify runs, the phase is stale


def test_qualify_positions_skip_non_host_devices(qsrv):
    """A positions run naming a JBOD skips it (reported) and qualifies the rest."""
    ok = {"confirm": True}
    r = post(qsrv, "/rr101/api/stage/qualify", {"positions": ["JBOD01"], **ok})
    assert r.status == 400 and b"not a host-bearing" in r.body     # nothing left to run
    r = post(qsrv, "/rr101/api/stage/qualify", {"positions": ["NOPE", "R37"], **ok})
    assert r.status == 400 and b"unknown position: NOPE" in r.body  # a typo is still refused
    _discover(qsrv)
    r = post(qsrv, "/rr101/api/stage/qualify", {"positions": ["JBOD01", "R37"], **ok})
    assert r.status == 202, r.body
    assert json.loads(r.body) == {"started": "qualify", "positions": ["R37"], "skipped": ["JBOD01"]}
    _wait_idle(qsrv)
    s = get_json(qsrv, "/rr101/api/state")
    r37 = next(d for d in s["devices"] if d["pos"] == "R37")
    j = next(d for d in s["devices"] if d["pos"] == "JBOD01")
    assert "pxe-live" in r37["runs"] and "pxe-live" not in j["runs"]
    r = post(qsrv, "/rr101/api/stage/qualify", {"positions": ["R37"], **ok})
    assert r.status == 202 and "skipped" not in json.loads(r.body)
    _wait_idle(qsrv)


def test_smart_health_failed_drive_fails_storage_names_it_and_fio_lists_it(ctx):
    base = ctx.rungs.poll
    bad = {"dev": "nvme1n1", "serial": "SN-BAD", "model": "M", "health": "UNKNOWN!",
           "power_on_hours": None, "verdict": "failed",
           "reasons": ["SMART self-assessment UNKNOWN!"]}

    def poll(ip, **k):
        out = base(ip, **k)
        if ctx.rungs.pos_of[ip] == "L37" and "smart-health" in out:
            out["smart-health"] = RungResult("fail", {"stage": "smart-health", "summary": {
                "devices": [bad], "failed": ["nvme1n1"], "warn": []}}, {})
            f = out["fio"]
            out["fio"] = RungResult(f.verdict, {**f.detail, "summary": {
                "devices": ["nvme0n1"], "smart_excluded": {"nvme1n1": bad["reasons"]}}},
                f.artifacts)
        return out
    ctx.rungs.poll = poll
    run_qualify(ctx, positions=["L37"])
    s = det(ctx, "L37", "smart-health")
    assert v(ctx, "L37", "smart-health") == "fail"
    assert s["headline"] == "1 drive · 1 failed: SN-BAD (NVMe nvme1)"
    assert s["table"]["rows"][0][4] == "failed"
    assert v(ctx, "L37", "fio") == "pass"
    left = dict(det(ctx, "L37", "fio")["rows"])["left out (SMART failed)"]
    assert left.startswith("nvme1n1 (") and left.endswith("): SMART self-assessment UNKNOWN!")
    assert "smart-health" in qualify_why(ctx.db.latest_runs(), ctx.g).get("L37", "")


# ---------------------------------------------------------------- fio judge (amber flag)
from flax_rack.fakes import _ALLDISKS as _FAKE_ALLDISKS, _fio_job

_HDD = "ST6000NM019B-2TG103"
_HDD_ALLDISKS = "".join(
    f"/dev/{d} [0:0:{i}:0] host0 Front_ArrayDevice0{i} mpt3sas rotational 6001.2 "
    f"SEAGATE SN04 {_HDD} WX01DSV{i}\n" for i, d in enumerate(("sda", "sdb", "sdc"), 2))


def _hdd_fio(lat_by_dev):
    return "\n".join(_fio_job(d, *j) for d, lat in lat_by_dev.items() for j in (
        ("bw", "read", "64.0KiB", 64, "3700", 231, 242, "69000", "usec", 70000, "usec"),
        ("lat", "randread", "4096B", 1, "190", 0.7, 0.8, lat, "usec", 9000, "usec"),
        ("iops", "read", "4096B", 256, "60.1k", 230, 241, "4200", "usec", 5000, "usec")))


_HDD_BASE = {"suite": "v1", "groups": [
    {"model": _HDD[:16], "size_gb": 6001.2, "controller": "mpt3sas", "job": "lat", "metric": "lat_us",
     "n": 40, "mean": 5000.0, "std": 100.0},
    {"model": _HDD[:16], "size_gb": 6001.2, "controller": "mpt3sas", "job": "bw", "metric": "mbps",
     "n": 40, "mean": 240.0, "std": 5.0}]}


def test_fio_detail_flags_outside_drive():
    text = _hdd_fio({"sda": "5010", "sdb": "4990", "sdc": "9000"})
    det = qualify.fio_detail({"summary": {"devices": ["sda", "sdb", "sdc"]}}, "pass", text,
                             _HDD_ALLDISKS, None, baseline=_HDD_BASE)
    assert det["flagged"] == ["sdc"]
    t = det["table"]
    assert t["cols"][-2:] == ["expected", "check"]
    assert len(t["row_cls"]) == len(t["rows"])
    for r, cls in zip(t["rows"], t["row_cls"]):
        assert cls == ("flag" if r[0] == "sdc" else "")
        assert r[-1] == ("outside" if r[0] == "sdc" and r[2].startswith("lat") else "ok")
    lat_sdc = next(r for r in t["rows"] if r[0] == "sdc" and r[2].startswith("lat"))
    assert lat_sdc[-2] == "≤ 5100 µs"
    assert det["judge"]["sdc"]["verdict"] == "outside"
    assert det["headline"].startswith("fio pass · 1 drive outside range")
    assert det["headline"] == "fio pass · 1 drive outside range: WX01DSV4 (Front_ArrayDevice04) lat"


def test_fio_detail_no_baseline_is_label_only():
    from flax_rack.fakes import _FIO
    plain = qualify.fio_detail({"summary": {"devices": ["nvme0n1", "nvme1n1"]}}, "pass", _FIO,
                               _FAKE_ALLDISKS, None, baseline={})
    assert plain["flagged"] == [] and set(plain["table"]["row_cls"]) == {""}
    assert {r[-1] for r in plain["table"]["rows"]} == {"no baseline"}
    # spec decision 9: a headline fragment (label only), so an unjudged pass is visible
    assert plain["headline"] == "nvme0n1 812k IOPS · 3283 MB/s · 29.8 µs · +1 device · no baseline"
    assert plain["flagged"] == []


def test_fio_detail_names_how_many_drives_had_no_baseline():
    other = _HDD_ALLDISKS.replace(f"{_HDD} WX01DSV3", "OTHERMODEL-XYZ WX01DSV3")  # sdb
    text = _hdd_fio({"sda": "5010", "sdb": "4990"})
    det = qualify.fio_detail({"summary": {"devices": ["sda", "sdb"]}}, "pass", text,
                             other, None, baseline=_HDD_BASE)
    assert det["judge"]["sda"]["verdict"] == "ok" and det["judge"]["sdb"]["verdict"] == "no baseline"
    assert det["flagged"] == [] and det["headline"].endswith(" · 1 drive no baseline")
    flagged = qualify.fio_detail({"summary": {"devices": ["sda", "sdb"]}}, "pass",
                                 _hdd_fio({"sda": "9000", "sdb": "4990"}), other, None,
                                 baseline=_HDD_BASE)
    assert flagged["headline"].startswith("fio pass · 1 drive outside range: WX01DSV2")
    assert flagged["headline"].endswith(" · 1 drive no baseline")


def test_fio_detail_with_missing_device_output_and_baseline():
    text = _hdd_fio({"sda": "5010", "sdc": "9000"})       # sdb: no fio output at all
    det = qualify.fio_detail({"summary": {"devices": ["sda", "sdb", "sdc"]}}, "pass", text,
                             _HDD_ALLDISKS, None, baseline=_HDD_BASE)
    assert det["flagged"] == ["sdc"]
    gone = next(r for r in det["table"]["rows"] if r[0] == "sdb")
    i = det["table"]["rows"].index(gone)
    assert gone[3] == "no fio output for this device" and det["table"]["row_cls"][i] == ""
    assert len(gone) == len(det["table"]["cols"])


def test_fio_detail_flag_never_changes_a_failed_verdicts_headline():
    text = _hdd_fio({"sda": "5010", "sdc": "9000"})
    det = qualify.fio_detail({"summary": {"devices": ["sda", "sdc"]}, "headline": "fio err"},
                             "fail", text, _HDD_ALLDISKS, None, baseline=_HDD_BASE)
    assert det["headline"] == "fio err" and det["flagged"] == ["sdc"]


def test_fio_view_rejudges_a_row_summarized_before_the_judge(tmp_path, monkeypatch):
    from flax_rack import server
    text = _hdd_fio({"sda": "5010", "sdb": "4990", "sdc": "9000"})
    (tmp_path / "fio.txt").write_text(text)
    (tmp_path / "alldisks.txt").write_text(_HDD_ALLDISKS)
    agent = {"summary": {"devices": ["sda", "sdb", "sdc"]}, "rows": [["stage", "fio"]],
             "notes": ["agent note"]}
    old = qualify.fio_detail(agent, "pass", text, _HDD_ALLDISKS, None, baseline={})
    for k in ("judge", "flagged", "row_cls"):            # as written before 2026-11-04
        old.pop(k, None)
    old["table"] = {"cols": old["table"]["cols"][:-2],
                    "rows": [r[:-2] for r in old["table"]["rows"]]}
    old["notes"] = old["notes"][:-1]
    runs = {"fio": {"id": 7, "verdict": "pass", "detail": old, "artifacts": {"fio": "fio.txt"}},
            "inventory": {"id": 3, "artifacts": {"alldisks": "alldisks.txt"}}}
    monkeypatch.setattr(qualify, "_BASELINE", _HDD_BASE)
    server._fio_view(tmp_path, runs)
    det = runs["fio"]["detail"]
    assert det["flagged"] == ["sdc"] and det["headline"].startswith("fio pass · 1 drive")
    assert [r[0] for r in det["rows"]].count("devices") == 1
    assert det["rows"][-1] == ["stage", "fio"] and det["notes"].count("agent note") == 1
