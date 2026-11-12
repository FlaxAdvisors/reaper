"""rackpost Run: Discover -> Firmware -> Qualify -> Deploy -> Done driven by the server
(rackpost Plan 6 Task 2)."""
import pytest

pytest.importorskip("flax_rack.run")
from flax_rack import run as runmod
from flax_rack import server
from flax_rack.deploy import deploy_passed
from flax_rack.fakes import canned_fdb, with_site_ports, with_wedge_ips
from flax_rack.geometry import load_geometry
from flax_rack.orderdb import OrderDB
from tests.test_flax_rack_geometry import FIX
from tests.test_flax_rack_qualify import make_ctx, profiles  # noqa: F401 -- fixture
from tests.test_flax_rack_server import _order_dir, _start, get_json, post

HOSTS = ("L37", "C37", "R37", "L05", "R05")


def rack(tmp_path, **rk):
    """A rack nothing has run on: switches on their site ports (their Discover passes),
    every firmware read at target (FakeRungs puts R05/R37/L37 off target)."""
    c = make_ctx(tmp_path, with_wedge_ips(with_site_ports(load_geometry(FIX))),
                 discover=False, **rk)
    c.kea.dyn.update(c.rungs.switch_leases())
    c.rungs.fw_bmc_ver, c.rungs.fw_bios_ver, c.rungs.fw_nic_uefi_off = {}, {}, set()
    return c


def v(c, pos, rung):
    return (c.db.latest_runs().get((pos, rung)) or {}).get("verdict")


def calls(c, name):
    return [x for x in c.rungs.calls if x[0] == name]


@pytest.fixture
def ctx(tmp_path, profiles):
    return rack(tmp_path)


def test_all_pass_goes_straight_through_to_done(ctx):
    rid = runmod.run(ctx, who="op")
    rp = ctx.db.run_plan()
    assert rp["id"] == rid and rp["state"] == "done" and rp["stage"] == "done"
    assert rp["held"] == {} and rp["out"] == {}
    assert deploy_passed(ctx.db, ctx.g) == (True, [])
    for pos in HOSTS:
        assert v(ctx, pos, "boot-reset") == "pass"
    assert (ctx.dir / "F4-RRORD-101-devices.csv").is_file()
    ev = [e["text"] for e in reversed(ctx.db.events(400)) if e["kind"] == "run"]
    assert ev == ["run 1 started by op (no reinstall: installed nodes deploy from firstboot)",
                  "run 1: discover (1/5)", "run 1: firmware (2/5)", "run 1: qualify (3/5)",
                  "run 1: deploy (4/5)", "run 1: done (5/5)", "run 1 done"]


def test_qualify_failure_holds_before_deploy_then_waiver_continues(tmp_path, profiles):
    c = rack(tmp_path, pop_fail={"R05"})
    rid = runmod.run(c, who="op")
    rp = c.db.run_plan()
    assert rp["state"] == "held" and rp["stage"] == "deploy"
    assert list(rp["held"]) == ["R05"]
    assert rp["held"]["R05"] == ["deploy gate: qualify not passed: population-check fail"]
    # nothing deployed; every other device's Qualify ran and passed (results kept)
    assert not calls(c, "install") and not calls(c, "firstboot")
    for pos in ("L37", "C37", "R37", "L05"):
        assert v(c, pos, "population-check") == "pass"
    # Continue with the operator's waiver, recorded at the click
    runmod.cont(c, rid, who="op", waive={"R05": {"rungs": ["population-check"],
                                                 "reason": "Mellanox on order, approved"}})
    rp = c.db.run_plan()
    assert rp["state"] == "done" and rp["out"] == {}
    assert deploy_passed(c.db, c.g) == (True, [])
    assert c.db.waivers()["R05"]["rungs"] == ["population-check"]
    assert v(c, "R05", "boot-reset") == "pass"


def test_discover_failure_is_carried_held_others_continue(ctx):
    ctx.kea.conflict_macs |= canned_fdb(ctx.g)[1]    # C37's host MAC held by a manual row
    rid = runmod.run(ctx)
    rp = ctx.db.run_plan()
    assert rp["state"] == "held" and list(rp["held"]) == ["C37"]
    assert rp["held"]["C37"][0].startswith("discover: bmc-reserved fail")
    # C37 went no further; the others were read and qualified
    assert v(ctx, "C37", "bmc-checked") is None and v(ctx, "C37", "pxe-live") is None
    assert v(ctx, "L37", "bmc-checked") == "pass" and v(ctx, "L37", "pxe-live") == "pass"
    runmod.cont(ctx, rid, who="op")
    rp = ctx.db.run_plan()
    assert rp["state"] == "done" and list(rp["out"]) == ["C37"]
    assert {x[1] for x in calls(ctx, "install")} == set(HOSTS) - {"C37"}


def test_continue_leaves_still_blocked_device_out(tmp_path, profiles):
    c = rack(tmp_path, pop_fail={"R05"})
    rid = runmod.run(c)
    runmod.cont(c, rid, who="op")                    # no waiver: R05 is left out
    rp = c.db.run_plan()
    assert rp["state"] == "done"
    assert rp["out"] == {"R05": "qualify not passed: population-check fail"}
    assert "R05" not in {x[1] for x in calls(c, "install")}
    assert v(c, "L37", "ha-test") == "pass" and v(c, "R05", "install") is None


def test_reinstall_no_never_wipes_an_installed_node(ctx):
    runmod.run(ctx)
    assert ctx.db.run_plan()["state"] == "done"
    installs = len(calls(ctx, "install"))
    assert installs == len(HOSTS)
    ctx.rungs.calls.clear()
    runmod.run(ctx, reinstall=False)                 # every node is installed now
    rp = ctx.db.run_plan()
    assert rp["state"] == "done", rp
    assert calls(ctx, "install") == []               # no disk wiped
    assert {x[1] for x in calls(ctx, "firstboot")} == set(HOSTS)
    ev = next(e["text"] for e in ctx.db.events(400) if e["text"].startswith("deploy started"))
    assert ev.startswith("deploy started from firstboot")


def test_reinstall_no_mixed_rack_installs_only_the_new_node(ctx):
    runmod.run(ctx)
    # R05's disk is replaced: no install ever passed there (a fresh order.db fact)
    import sqlite3
    con = sqlite3.connect(ctx.db.path)
    con.execute("UPDATE run SET verdict='fail' WHERE pos='R05' AND rung IN ('install','firstboot')")
    con.execute("UPDATE run SET verdict='fail' WHERE pos='R05' AND rung IN ('power-on','host-ssh')")
    con.commit(); con.close()
    ctx.rungs.calls.clear()
    ctx.rungs.installed.discard("R05")
    runmod.run(ctx)
    assert ctx.db.run_plan()["state"] == "done"
    assert [x[1] for x in calls(ctx, "install")] == ["R05"]
    assert {x[1] for x in calls(ctx, "firstboot")} == set(HOSTS)


def test_reinstall_yes_installs_every_node(ctx):
    runmod.run(ctx)
    ctx.rungs.calls.clear()
    runmod.run(ctx, reinstall=True)
    assert ctx.db.run_plan()["state"] == "done"
    assert {x[1] for x in calls(ctx, "install")} == set(HOSTS)


def test_cancel_stops_the_run(ctx):
    ctx.cancel.set()
    runmod.run(ctx)
    rp = ctx.db.run_plan()
    assert rp["state"] == "cancelled" and rp["stage"] == "discover"
    assert not calls(ctx, "install")


# ---------------------------------------------------------------- server
def test_restart_mid_run_is_interrupted_not_rerun(tmp_path, factory):
    d = _order_dir(tmp_path)
    db = OrderDB(d / "order.db"); db.init(load_geometry(d / "geometry.json"))
    rid = db.start_run_plan("op", {"reinstall": False})
    db.set_run_plan(rid, stage="deploy")
    db.start_run("C37", "deploy", "install")         # the daemon died mid-install
    s = _start(tmp_path, factory)
    try:
        st = get_json(s, "/rr101/api/state")
        assert st["run"]["state"] == "interrupted" and st["run"]["stage"] == "deploy"
        assert st["run"]["can_continue"] and not st["stage_running"]
        assert any("run 1 interrupted by a daemon restart in deploy" in e["text"]
                   for e in st["events"])
        # nothing was started again: the only run rows are the ones before the restart
        assert [r["rung"] for r in db.runs_all()] == ["install"]
    finally:
        s.shutdown(); s.server_close()


def test_run_api_confirm_reinstall_and_drain(tmp_path, factory, monkeypatch):
    _order_dir(tmp_path)
    drain = tmp_path / "drain"
    monkeypatch.setattr(server, "DRAIN_FILE", drain)
    s = _start(tmp_path, factory)
    try:
        assert post(s, "/rr101/api/run", {"confirm": "nope"}).status == 400
        r = post(s, "/rr101/api/run", {"confirm": "RRORD-101", "reinstall": True})
        assert r.status == 400 and b"order id" in r.body
        drain.touch()
        r = post(s, "/rr101/api/run", {"confirm": "RRORD-101"})
        assert r.status == 409 and b"being updated" in r.body
        assert get_json(s, "/rr101/api/state")["run"] is None
        r = post(s, "/rr101/api/run/continue", {"confirm": True})
        assert r.status == 409 and b"no held" in r.body
    finally:
        s.shutdown(); s.server_close()


def test_cancel_ends_a_held_run(tmp_path, factory):
    d = _order_dir(tmp_path)
    db = OrderDB(d / "order.db"); db.init(load_geometry(d / "geometry.json"))
    rid = db.start_run_plan("op", {"reinstall": False})
    db.set_run_plan(rid, state="held", stage="deploy", held={"R05": ["x"]})
    s = _start(tmp_path, factory)
    try:
        assert get_json(s, "/rr101/api/state")["run"]["state"] == "held"
        assert post(s, "/rr101/api/stage/cancel", {"confirm": True}).status == 202
        assert get_json(s, "/rr101/api/state")["run"]["state"] == "cancelled"
    finally:
        s.shutdown(); s.server_close()


@pytest.fixture
def factory():
    from tests.test_flax_rack_server import Factory
    return Factory()


def test_cli_run_hold_and_continue(tmp_path, profiles, monkeypatch, capsys):
    from flax_rack import __main__ as cli
    d = tmp_path / "o"; d.mkdir()
    (d / "geometry.json").write_text(open(FIX).read())
    holder = {}

    def factory(rd, g, db):
        if "c" not in holder:
            c = holder["c"] = rack(rd, pop_fail={"R05"})
            c.db = db
        return holder["c"]
    monkeypatch.setattr(server, "default_context_factory", factory)
    monkeypatch.setattr(server, "DRAIN_FILE", tmp_path / "drain")
    monkeypatch.chdir(d)
    with pytest.raises(SystemExit, match="run --confirm"):
        cli.main(["run"])
    assert cli.main(["run", "--confirm", "RRORD-101"]) == 1
    out = capsys.readouterr().out
    assert "run 1: held (stage deploy)" in out and "held R05" in out
    assert cli.main(["run", "--continue", "--waive", "R05:population-check",
                     "--reason", "approved"]) == 0
    assert "run 1: done (stage done)" in capsys.readouterr().out
    assert holder["c"].db.waivers()["R05"]["reason"] == "approved"


def test_continue_after_interrupted_reinstall_deploy_never_wipes_twice(ctx):
    rid = runmod.run(ctx, reinstall=True)            # first Run installs every node
    ctx.db.set_run_plan(rid, state="interrupted", stage="deploy")   # as if cut mid-Deploy
    ctx.rungs.calls.clear()
    runmod.cont(ctx, rid, who="op")
    assert ctx.db.run_plan()["state"] == "done"
    assert calls(ctx, "install") == []               # installed by this Run: firstboot


# ---------------------------------------------------------------- fix wave (final review)
def test_reinstall_no_installs_only_a_disk_proven_blank(ctx):
    from flax_rack.deploy import GateError, NOT_BLANK, run_deploy
    ctx.rungs.disk_used = {"R05"}                    # a foreign OS on R05's nvme0n1
    ctx.rungs.disk_unread = {"L05"}                  # L05's disk: not known
    rid = runmod.run(ctx)
    rp = ctx.db.run_plan()
    assert rp["state"] == "held" and sorted(rp["held"]) == ["L05", "R05"]
    assert rp["held"]["R05"] == [f"deploy: {NOT_BLANK} (nvme0n1 has nvme0n1p1 (lsblk))"]
    assert rp["held"]["L05"][0].startswith(f"deploy: {NOT_BLANK} (no inventory")
    assert not calls(ctx, "install")
    with pytest.raises(GateError, match="not blank"):            # the manual Deploy too
        run_deploy(ctx, positions=["R05"])
    runmod.cont(ctx, rid, who="op")                  # Continue: the blank three install
    assert ctx.db.run_plan()["state"] == "done"
    assert {x[1] for x in calls(ctx, "install")} == set(HOSTS) - {"R05", "L05"}
    ctx.rungs.calls.clear()
    runmod.run(ctx, reinstall=True)                  # the typed reinstall wipes them
    assert {"R05", "L05"} <= {x[1] for x in calls(ctx, "install")}


@pytest.mark.parametrize("arts,want", [
    ({"lsblk": "NAME MAJ:MIN RM SIZE RO TYPE MOUNTPOINTS\nnvme0n1 259:0 0 1T 0 disk \n",
      "blkid": '/dev/loop0: TYPE="squashfs"\n'}, True),
    ({"lsblk": "NAME MAJ:MIN RM SIZE RO TYPE MOUNTPOINTS\nnvme0n1 259:0 0 1T 0 disk \n",
      "blkid": '/dev/loop0: TYPE="squashfs"\n/dev/nvme0n1: TYPE="LVM2_member"\n'}, False),
    ({"lsblk": "NAME MAJ:MIN RM SIZE RO TYPE MOUNTPOINTS\nnvme0n1 259:0 0 1T 0 disk \n"}, None),
    ({"lsblk": "NAME MAJ:MIN RM SIZE RO TYPE MOUNTPOINTS\nsda 8:0 0 1T 0 disk \n",
      "blkid": '/dev/loop0: TYPE="squashfs"\n'}, None),
    ({}, None)])
def test_disk_blank_needs_both_captures_and_the_install_disk(tmp_path, arts, want):
    from flax_rack.deploy import disk_blank
    for n, t in arts.items():
        (tmp_path / f"{n}.txt").write_text(t)
    lr = {("R05", "inventory"): {"artifacts": {n: f"{n}.txt" for n in arts}}}
    assert disk_blank(tmp_path, lr, "R05")[0] is want


def test_interrupted_run_continues_in_its_stage_never_on_older_qualify(ctx):
    from flax_rack.qualify import run_qualify
    from flax_rack.stages import run_discover
    run_discover(ctx); run_qualify(ctx)              # this morning's passes, before any Run
    rid = runmod.start(ctx.db, "op", reinstall=False)
    run_discover(ctx)                                # the Run's Discover finished ...
    ctx.db.set_run_plan(rid, state="interrupted", stage="qualify")   # ... cut in Qualify
    ctx.rungs.calls.clear()
    runmod.cont(ctx, rid, who="op")
    assert ctx.db.run_plan()["state"] == "done"
    ev = [e["text"] for e in reversed(ctx.db.events(400)) if e["kind"] == "run"]
    assert ev[ev.index("run 1: continue by op (was interrupted in qualify)") + 1:] == [
        "run 1: qualify (3/5)", "run 1: deploy (4/5)", "run 1: done (5/5)", "run 1 done"]
    launched = [x[1] for x in calls(ctx, "launch")]
    assert set(launched) == set(HOSTS)               # Qualify ran again in this Run
    first_install = next(i for i, x in enumerate(ctx.rungs.calls) if x[0] == "install")
    assert max(i for i, x in enumerate(ctx.rungs.calls) if x[0] == "launch") < first_install


def test_held_run_qualifies_a_rediscovered_device_before_deploying_it(ctx):
    from flax_rack.qualify import run_qualify
    from flax_rack.stages import run_discover
    run_discover(ctx); run_qualify(ctx)              # C37 passed Qualify before the Run
    held = canned_fdb(ctx.g)[1]
    ctx.kea.conflict_macs |= held                    # this Run's Discover fails C37
    rid = runmod.run(ctx)
    assert list(ctx.db.run_plan()["held"]) == ["C37"]
    ctx.kea.conflict_macs -= held
    run_discover(ctx, positions=["C37"])             # fixed and re-discovered while held
    ch = runmod.deploy_choice(ctx.db, ctx.g, ctx.db.run_plan())
    assert ch["requalify"] == ["C37"] and "C37" not in ch["targets"]
    ctx.rungs.calls.clear()
    runmod.cont(ctx, rid, who="op")
    assert ctx.db.run_plan()["state"] == "done"
    assert [x[1] for x in calls(ctx, "launch")][:1] == ["C37"]
    assert "C37" in {x[1] for x in calls(ctx, "install")}


def test_firstboot_target_with_an_earlier_failed_install_counts_deployed(ctx):
    import sqlite3
    runmod.run(ctx)
    con = sqlite3.connect(ctx.db.path)               # an install that failed earlier in the pass
    con.execute("UPDATE run SET verdict='fail' WHERE pos='L37' AND rung='install'")
    con.commit(); con.close()
    runmod.run(ctx)                                  # installed: L37 deploys from firstboot
    assert v(ctx, "L37", "install") == "skip"
    assert deploy_passed(ctx.db, ctx.g) == (True, [])
