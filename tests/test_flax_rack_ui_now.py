"""rackpost follow-up (2026-10-31, RRORD-101 live Run): a stage re-run shows only its new
attempt, Deploy from firstboot reads complete, a scoped Qualify leaves the iperf server's
row alone when no client measured, the agent launch's evidence rows, the hold cleared
once the Run goes on."""
from types import SimpleNamespace

from flax_rack import run as runmod
from flax_rack import server
from flax_rack.deploy import run_deploy
from flax_rack.qualify import run_qualify
from flax_rungs import agent
from flax_rungs.result import RungResult
from tests.test_flax_rack_qualify import ctx, det, profiles, v  # noqa: F401 -- fixtures
from tests.test_flax_rack_run import rack
from tests.test_flax_rack_ui_state import _dev, _rack
from tests.test_flax_rungs_agent import PW, FakeClient, Runner


def _state_of(tmp_path, c):
    r = _rack(tmp_path)
    r.g, r.db = c.g, c.db
    return server.rack_state(r)


def test_rerun_draws_only_the_new_attempt(ctx, tmp_path):
    ctx.rungs.fail_pxe = {"C37"}
    run_qualify(ctx)                       # C37: every later rung "failed in phase boot"
    assert det(ctx, "C37", "fio")["reason"] == "failed in phase boot"
    ctx.rungs.fail_pxe = set()
    seen = {}
    base = ctx.rungs.boot_live

    def boot(*a, **k):                     # mid-attempt: what the drawer would draw now
        seen["runs"] = _dev(_state_of(tmp_path, ctx), "C37")["runs"]
        seen["l37"] = _dev(_state_of(tmp_path, ctx), "L37")["runs"]
        return base(*a, **k)
    ctx.rungs.boot_live = boot
    run_qualify(ctx, positions=["C37"])
    # the earlier attempt's skips are gone: only the new attempt's running boot step
    assert set(seen["runs"]) & {"fio", "inventory", "agent-reachable", "iperf-mesh"} == set()
    assert seen["runs"]["pxe-live"]["status"] == "running"
    assert seen["runs"]["serial"]["verdict"] == "pass"         # Discover's rows stand
    assert seen["l37"]["fio"]["verdict"] == "pass"             # other devices untouched
    # after: the new attempt's results; history keeps both attempts
    runs = _dev(_state_of(tmp_path, ctx), "C37")["runs"]
    assert runs["fio"]["verdict"] == "pass"
    assert ctx.db.run_count("C37", "fio") == 2


def test_deploy_from_firstboot_install_reads_na(tmp_path, profiles):  # noqa: F811
    c = rack(tmp_path)
    runmod.run(c)
    runmod.run(c, reinstall=False)          # every node installed: from firstboot
    assert c.db.run_plan()["state"] == "done"
    inst = _dev(_state_of(tmp_path, c), "L37")["runs"]["install"]
    assert (inst["verdict"], inst["skip_reason"]) == ("skip", "n/a")


def test_scoped_qualify_never_overwrites_the_servers_mesh_without_a_client(ctx):
    run_qualify(ctx)
    srv = ctx.g.iperf_server
    assert v(ctx, srv, "iperf-mesh") == "pass"
    n, f = ctx.db.run_count(srv, "iperf-mesh"), ctx.db.run_count(srv, "fabric-addr")
    ctx.rungs.fail_pxe = {"L37"}
    run_qualify(ctx, positions=["L37"])     # no client reaches the network phase
    assert ctx.db.run_count(srv, "iperf-mesh") == n and v(ctx, srv, "iperf-mesh") == "pass"
    assert ctx.db.run_count(srv, "fabric-addr") == f          # nothing of it touched
    ctx.rungs.fail_pxe = set()
    run_qualify(ctx, positions=["L37"])     # a client measured against it: written
    assert ctx.db.run_count(srv, "iperf-mesh") == n + 1
    assert det(ctx, srv, "iperf-mesh")["served"] == ["L37"]


_FETCH_FAIL = """FLAXEV ssh up 312.55
FLAXEV fetch http://bang/post.tgz -> unresolved, 40 tries, 151 s; last: curl: (6) Could not resolve host: bang
post.tgz fetch failed for 2 min
FLAXNET_BEGIN
lo UNKNOWN 127.0.0.1/8
eth0 UP

default via 172.32.37.1 dev eth0
FLAXNET_END
"""


def test_launch_failure_names_each_step_and_keeps_the_nodes_network():
    res = agent.launch("172.32.37.11", "flax", PW, runner=Runner(out=_FETCH_FAIL, rc=2))
    assert res.verdict == "fail" and res.detail["error"] == "agent launch rc=2"
    assert res.detail["headline"] == "agent launch rc=2: post.tgz fetch failed for 2 min"
    assert res.detail["rows"] == [
        ["ssh", "flax@172.32.37.11 ok · up 312 s"],
        ["fetch", "http://bang/post.tgz -> unresolved, 40 tries, 151 s; "
                  "last: curl: (6) Could not resolve host: bang"]]
    assert "eth0 UP" in res.artifacts["net"] and "default via" in res.artifacts["net"]


def test_launch_pass_says_when_the_agent_port_answered():
    out = ("FLAXEV ssh up 40.1\nFLAXEV fetch http://bang/post.tgz -> 172.32.0.1, 3 tries, 6 s\n"
           "FLAXEV unpack post.tgz unpacked\n"
           "FLAXEV launch post.sh postautomate started, flax-qual-agent active\n")
    t = [0.0]
    res = agent.launch("h", "flax", PW, runner=Runner(out=out),
                       client_factory=lambda url: FakeClient([], down_first=3),
                       sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    assert res.verdict == "pass"
    assert [r[0] for r in res.detail["rows"]] == ["ssh", "fetch", "unpack", "launch", "agent port"]
    assert res.detail["rows"][-1] == ["agent port", "8087 answered after 6 s"]


def test_launch_net_capture_is_saved_as_an_artifact(ctx):
    base = ctx.rungs.launch

    def launch(ip, u, p, **k):
        if ctx.rungs.pos_of[ip] == "C37":
            return RungResult("fail", {"error": "agent launch rc=2"}, {"net": "eth0 UP\n"})
        return base(ip, u, p, **k)
    ctx.rungs.launch = launch
    run_qualify(ctx)
    row = ctx.db.latest_runs()[("C37", "agent-reachable")]
    path = row["artifacts"]["net"]
    assert path.startswith("artifacts/C37/") and (ctx.dir / path).read_text() == "eth0 UP\n"


def test_hold_cleared_once_the_run_goes_on_and_left_out_deployed_since(tmp_path, profiles):  # noqa: F811
    c = rack(tmp_path, pop_fail={"R05"})
    rid = runmod.run(c)
    assert list(c.db.run_plan()["held"]) == ["R05"]
    runmod.cont(c, rid, who="op")           # R05 left out
    rp = c.db.run_plan()
    assert rp["state"] == "done" and rp["held"] == {} and list(rp["out"]) == ["R05"]
    r = SimpleNamespace(db=c.db, g=c.g, lock=SimpleNamespace(busy=lambda: False))
    assert server._run_view(r)["out_resolved"] == []
    run_deploy(c, positions=["R05"], who="op",
               waive={"R05": {"rungs": ["population-check"], "reason": "approved"}})
    assert server._run_view(r)["out_resolved"] == ["R05"]
