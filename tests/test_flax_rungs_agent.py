import json
from types import SimpleNamespace

from flax_post.qualclient import QualUnreachable
from flax_rungs import agent

PW = "s3cretpw"


class Runner:
    def __init__(self, out="", rc=0):
        self.out, self.rc, self.calls = out, rc, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return SimpleNamespace(returncode=self.rc, stdout=self.out, stderr="")


class FakeClient:
    """Walks a scripted sequence of /stages snapshots, one per poll."""
    def __init__(self, snaps, run_id="r1", ok=True, down_first=0):
        self.snaps, self.i, self.run_id, self.ok = snaps, 0, run_id, ok
        self.down, self.restarts, self.acks = down_first, 0, 0

    def health(self):
        if self.down:
            self.down -= 1
            raise QualUnreachable("down")
        return {"ok": self.ok, "error": "unknown stage x", "run_id": self.run_id}

    def stages(self):
        s = self.snaps[min(self.i, len(self.snaps) - 1)]
        self.i += 1
        return [{"name": n, "status": st} for n, st in s]

    def stage(self, n):
        s = self.snaps[-1 if self.i >= len(self.snaps) else self.i - 1]
        st = dict(s)[n]
        return {"name": n, "status": st, "verdict": st if st in agent._TERMINAL else None,
                "summary": {"k": 1}, "artifacts": [{"name": "raw"}] if st != "pending" else []}

    def stage_artifact(self, n, a):
        return f"{n}-{a}-text"

    def restart(self):
        self.restarts += 1
        return {"old_run_id": "o", "new_run_id": "n"}

    def restart_ack(self):
        self.acks += 1


def test_order_stages_exact():
    assert agent.ORDER_STAGES == ("sdr-pre,sel-pre,sel-clear,tooling,inventory,smart-health,fio,mem-pre,"
                                  "cpu-mem-stress,mem-post,sdr-post,sel-post")
    assert "iperf" not in agent.ORDER_STAGES.split(",")


def test_launch_script_and_no_password():
    r = Runner()
    res = agent.launch("172.32.5.1", "flax", PW, runner=r,
                       client_factory=lambda url: FakeClient([]))
    assert res.verdict == "pass" and res.detail["mode"] == "fresh"
    argv, kw = r.calls[0]
    script = kw["input"]
    assert f"FLAX_QUAL_STAGES={agent.ORDER_STAGES}" in script
    assert ">> flax-qual.env" in script and "./post.sh postautomate" in script
    assert "http://bang/post.tgz" in script
    assert PW not in script and PW not in " ".join(argv)
    assert kw["env"]["SSHPASS"] == PW


def test_launch_rejects_bad_stage_text():
    import pytest
    with pytest.raises(ValueError):
        agent.launch_script("a;rm -rf /")


def test_launch_ssh_failure():
    res = agent.launch("h", "flax", PW, runner=Runner(out="boom", rc=2))
    assert res.verdict == "fail" and "rc=2" in res.detail["error"]


class SeqRunner(Runner):
    """First ssh says the unit is active; later ones (stop, relaunch) succeed."""
    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        out = "FLAX_AGENT_ACTIVE\n" if len(self.calls) == 1 else ""
        return SimpleNamespace(returncode=0, stdout=out, stderr="")


def _names(stages):
    return [(n, "pass") for n in stages.split(",")]


def test_already_active_matching_restarts():
    c = FakeClient([_names(agent.ORDER_STAGES)])
    r = SeqRunner()
    res = agent.launch("h", "flax", PW, runner=r, client_factory=lambda url: c)
    assert res.verdict == "pass" and res.detail["mode"] == "restart"
    assert (c.restarts, c.acks) == (1, 1) and len(r.calls) == 1
    assert res.detail["new_run_id"] == "n"


def test_already_active_mismatch_stops_and_relaunches():
    full = agent.ORDER_STAGES.split(",") + ["iperf"]
    c = FakeClient([[(n, "pass") for n in full]])
    r = SeqRunner()
    res = agent.launch("h", "flax", PW, runner=r, client_factory=lambda url: c)
    assert res.verdict == "pass" and res.detail["mode"] == "relaunch-stage-mismatch"
    assert c.restarts == 0 and c.acks == 0
    scripts = [kw["input"] for _, kw in r.calls]
    assert len(scripts) == 3
    assert "systemctl stop flax-qual-agent" in scripts[1]
    assert "FLAX_QUAL_STAGES=" in scripts[2] and "postautomate" in scripts[2]


def test_stop_failure_fails():
    class R(SeqRunner):
        def __call__(self, argv, **kw):
            res = super().__call__(argv, **kw)
            if len(self.calls) == 2:
                res.returncode = 3
            return res
    c = FakeClient([[("iperf", "pass")]])
    res = agent.launch("h", "flax", PW, runner=R(), client_factory=lambda url: c)
    assert res.verdict == "fail" and "stop" in res.detail["error"]


def test_active_restart_unreachable_fails():
    class Dead(FakeClient):
        def restart(self):
            raise QualUnreachable("nope")
    res = agent.launch("h", "flax", PW, runner=Runner(out="FLAX_AGENT_ACTIVE"),
                       client_factory=lambda url: Dead([_names(agent.ORDER_STAGES)]))
    assert res.verdict == "fail"


def _poll(c, **kw):
    t = [0]
    def clock():
        return t[0]
    def sleep(s):
        t[0] += s
    kw.setdefault("deadline_s", 3600)
    return agent.poll("h", client_factory=lambda url: c, sleep=sleep, clock=clock, **kw)


def test_poll_walks_to_terminal_and_collects():
    c = FakeClient([[("a", "running"), ("b", "pending")],
                    [("a", "pass"), ("b", "running")],
                    [("a", "pass"), ("b", "fail")]], down_first=1)
    out = _poll(c)
    assert out["a"].verdict == "pass" and out["b"].verdict == "fail"
    assert out["a"].artifacts == {"raw": "a-raw-text"}
    assert out["a"].detail["run_id"] == "r1" and out["a"].detail["summary"] == {"k": 1}


def test_poll_skip_is_terminal():
    out = _poll(FakeClient([[("a", "skip")]]))
    assert out["a"].verdict == "skip"


def test_poll_deadline_fails_unfinished():
    c = FakeClient([[("a", "pass"), ("b", "running")]])
    out = _poll(c, deadline_s=100, poll_s=20)
    assert out["a"].verdict == "pass"
    assert out["b"].verdict == "fail" and out["b"].detail["error"] == "timeout"


def test_poll_never_reachable_is_unmeasured():
    out = _poll(FakeClient([[]], down_first=10**6), deadline_s=60)
    a = out["agent"]
    assert a.verdict == "unmeasured" and a.detail["error"] == "timeout"   # qualify stops it
    assert a.detail["headline"].startswith("agent never answered in 60s")


def test_poll_unhealthy_agent():
    out = _poll(FakeClient([[("a", "pending")]], ok=False))
    assert out["agent"].verdict == "fail" and "unknown stage" in out["agent"].detail["error"]


def test_save_artifacts_separate_runs(tmp_path):
    p1 = agent.save_artifacts(tmp_path, "L05", "r1", "sdr-pre", {"sdr": "one"})
    p2 = agent.save_artifacts(tmp_path, "L05", "r2", "sdr-pre", {"sdr": "two"})
    assert p1 == ["artifacts/L05/r1/sdr-pre/sdr.txt"]
    assert p2 == ["artifacts/L05/r2/sdr-pre/sdr.txt"]
    assert (tmp_path / p1[0]).read_text() == "one" and (tmp_path / p2[0]).read_text() == "two"


def test_poll_deadline_is_required():
    import inspect, pytest
    assert inspect.signature(agent.poll).parameters["deadline_s"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        agent.poll("h", client_factory=lambda url: FakeClient([[]]))


def test_stop_runs_systemctl_stop_over_ssh():
    r = Runner()
    res = agent.stop("h", "flax", PW, runner=r)
    assert res.verdict == "pass"
    assert "systemctl stop flax-qual-agent" in r.calls[0][1]["input"]
    assert PW not in " ".join(r.calls[0][0])
    res = agent.stop("h", "flax", PW, runner=Runner(out="still", rc=3))
    assert res.verdict == "fail" and "rc=3" in res.detail["error"]


def test_unfetched_artifact_named_in_detail():
    class C(FakeClient):
        def stage_artifact(self, n, a):
            raise QualUnreachable("gone")
    out = _poll(C([[("a", "pass")]]))
    assert out["a"].artifacts == {} and out["a"].detail["unfetched_artifacts"] == ["raw"]
