"""flax_rungs.stress (Deploy's deploy-stress) against the fake ssh + ipmitool runner."""
import pytest

pytest.importorskip("flax_rungs.stress")
from flax_rack.fakes import StressHosts
from flax_rungs import stress

BMC_PW = "fake-bmc-pw"
NODES = [{"pos": "L37", "host_ip": "172.32.37.1", "bmc_ip": "172.32.37.11"},
         {"pos": "R37", "host_ip": "172.32.37.3", "bmc_ip": "172.32.37.13"}]


class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s

    def __call__(self):
        return self.t


def go(**knobs):
    hosts = StressHosts({ip: n["pos"] for n in NODES for ip in (n["host_ip"], n["bmc_ip"])})
    for k, v in knobs.items():
        setattr(hosts, k, v)
    clk = Clock()
    out = stress.run(NODES, user="flax", pw="fake-host-pw", bmc_creds=[("admin", BMC_PW)],
                     runner=hosts, sleep=clk.sleep, clock=clk)
    return out, hosts


def test_pass_launches_every_node_and_keeps_the_evidence():
    out, hosts = go()
    for pos in ("L37", "R37"):
        r = out[pos]
        assert r.verdict == "pass", r.detail
        # the before SEL's correctable ECC and the boot-time EDAC line are not this run's
        assert r.detail["sel_new"] == 0 and r.detail["dmesg_bad"] == []
        assert set(r.artifacts) == {"stress-ng.txt", "dmesg-delta.txt", "sdr-elist-before.txt",
                                    "sel-elist-before.txt", "sdr-elist-after.txt",
                                    "sel-elist-after.txt"}
        assert "EDAC skx: ECC is enabled" not in r.artifacts["dmesg-delta.txt"]
        assert "perf: interrupt took too long" in r.artifacts["dmesg-delta.txt"]
        assert ["stress-ng cpu", "9461234 bogo ops in 600.02 s (15768.20/s)"] in r.detail["rows"]
    launches = [s for _p, s in hosts.scripts if "setsid sh -c" in s]
    assert len(launches) == 2 and "stress-ng --cpu 0 --vm 4 --vm-bytes 80% --timeout 600s " \
        "--metrics-brief" in launches[0]


def test_new_non_benign_sel_fails_only_that_node():
    out, _ = go(sel_bad={"R37"})
    assert out["L37"].verdict == "pass"
    r = out["R37"]
    assert r.verdict == "fail" and r.detail["headline"].startswith("SEL: 1 new non-benign")
    assert "DIMM_B1" in r.detail["sel_nonbenign"][0]


def test_threshold_transient_pair_is_benign():
    out, _ = go(transient={"L37"})
    r = out["L37"]
    assert r.verdict == "pass", r.detail
    assert r.detail["sel_new"] == 2 and r.detail["sel_transients"][0]["seconds"] == 6


def test_one_corrected_machine_check_fails():
    # operator 2026-11-06 (final): any correctable hardware error fails (CE_FAIL_AT = 1)
    out, _ = go(mce={"L37"})
    r = out["L37"]
    assert r.verdict == "fail" and r.detail["hw_corrected"] == 1 and not r.detail["hw_uncorrected"]
    assert "1 correctable hardware errors during stress" in r.detail["error"]
    assert len(r.detail["dmesg_bad"]) == 2
    assert out["R37"].verdict == "pass" and out["R37"].detail["hw_corrected"] == 0


def test_node_lost_after_launch_fails():
    out, hosts = go(lost={"R37"})
    r = out["R37"]
    assert r.verdict == "fail"
    assert "did not answer ssh after stress" in r.detail["error"]
    assert "no exit code within the 1200 s deadline" in r.detail["error"]
    assert out["L37"].verdict == "pass"


def test_stress_ng_rc_fails():
    out, _ = go(rc={"L37": 2})
    assert out["L37"].verdict == "fail" and out["L37"].detail["rc"] == 2
    assert out["L37"].detail["headline"].startswith("stress-ng rc=2")


def _rig(**kw):
    hosts = StressHosts({ip: n["pos"] for n in NODES for ip in (n["host_ip"], n["bmc_ip"])})
    clk = Clock()
    run = lambda: stress.run(NODES, user="flax", pw="fake-host-pw",      # noqa: E731
                             bmc_creds=[("admin", BMC_PW)], runner=hosts, sleep=clk.sleep,
                             clock=clk, **kw)
    return hosts, clk, run


def test_deadline_counts_from_the_launch_not_the_prep():
    hosts, clk, run = _rig()
    real = hosts._ssh

    def ssh(pos, script):
        if "/proc/uptime" in script and pos == "L37":
            clk.t += 400                      # a slow apt-get install of stress-ng
        if script == stress._DONE and clk.t < 400 + 1000:
            return 0, "RUNNING\n"             # stress-ng ends 1000 s after its launch
        return real(pos, script)
    hosts._ssh = ssh
    out = run()
    assert all(r.verdict == "pass" for r in out.values()), out["L37"].detail
    assert not [s for _p, s in hosts.scripts if s == stress._KILL]


def test_cancel_stops_stress_ng_within_a_poll():
    hosts, clk, run = _rig(cancelled=lambda: clk.t >= 100)
    out = run()
    assert clk.t < 100 + stress.POLL_S
    assert sorted(p for p, s in hosts.scripts if s == stress._KILL) == ["L37", "R37"]
    assert not [s for _p, s in hosts.scripts if s == stress._COLLECT]
    for r in out.values():
        assert r.verdict == "unmeasured" and r.detail["headline"].startswith("cancelled")
