import subprocess, pytest
pytest.importorskip("flax_rungs")
from flax_rungs.bmc import mc_info, power_action, power_status, reach

def _cp(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err)

def test_reach_uses_env_password_and_retries():
    calls = []
    def fake(argv, **kw):
        calls.append((argv, kw.get("env", {})))
        if argv[0] == "ping":
            return _cp(argv)
        if sum(1 for a, _ in calls if a[0] == "ipmitool") < 3:
            return _cp(argv, 1, "", "RAKP 2 HMAC is invalid")
        return _cp(argv, 0, "Chassis Power is off\n")
    r = reach("172.32.37.101", [("u", "p")], runner=fake)
    assert r.verdict == "pass" and r.detail == {"ping": True, "power": "off", "user": "u", "cipher": 17}
    ipmi = [(a, e) for a, e in calls if a[0] == "ipmitool"]
    assert len(ipmi) == 3
    for a, e in ipmi:
        assert "-P" not in a and "p" not in a and "-E" in a and e["IPMI_PASSWORD"] == "p"

def test_reach_no_ping_fails_without_ipmi():
    seen = []
    def fake(argv, **kw):
        seen.append(argv[0]); return _cp(argv, 1)
    r = reach("1.2.3.4", [("u", "p")], runner=fake)
    assert r.verdict == "fail" and r.detail["ping"] is False and seen == ["ping"]

def test_reach_second_cred_wins():
    def fake(argv, **kw):
        if argv[0] == "ping": return _cp(argv)
        return _cp(argv, 0, "Chassis Power is on\n") if "u2" in argv else _cp(argv, 1, "", "denied")
    r = reach("h", [("u1", "p1"), ("u2", "p2")], tries=1, runner=fake)
    assert r.verdict == "pass" and r.detail["user"] == "u2" and r.detail["power"] == "on"


def _leopard(log):
    """A BMC that only speaks lanplus cipher 3 (Leopard); -C 17 looks dead."""
    def fake(argv, **kw):
        log.append(argv)
        if argv[0] == "ping":
            return _cp(argv)
        c = argv[argv.index("-C") + 1]
        if c != "3":
            return _cp(argv, 1, "", "Error in open session response message : no matching cipher suite")
        if argv[-1] == "status":
            return _cp(argv, 0, "Chassis Power is on\n")
        if argv[-2:] == ["mc", "info"]:
            return _cp(argv, 0, "Firmware Revision         : 3.1\n")
        return _cp(argv, 0, "ok")
    return fake


def test_reach_falls_back_to_cipher_3_and_records_it():
    log = []
    r = reach("h", [("u", "p")], tries=1, runner=_leopard(log))
    assert r.verdict == "pass" and r.detail["cipher"] == 3 and r.detail["power"] == "on"
    assert [a[a.index("-C") + 1] for a in log if a[0] == "ipmitool"] == ["17", "3"]


def test_reach_both_ciphers_fail_names_them():
    def fake(argv, **kw):
        return _cp(argv) if argv[0] == "ping" else _cp(argv, 1, "", "no session")
    r = reach("h", [("u", "p")], tries=1, runner=fake)
    assert r.verdict == "fail" and "17/3" in r.detail["error"]


def test_status_and_mc_info_fall_back_to_cipher_3():
    info = {}
    assert power_status("h", [("u", "p")], info=info, runner=_leopard([])) == "on"
    assert info == {"user": "u", "cipher": 3}
    assert mc_info("h", [("u", "p")], runner=_leopard([]))["cipher"] == 3


def test_power_action_sent_once_on_the_cipher_that_answered():
    log, info = [], {}
    assert power_action("h", [("u", "p")], "cycle", info=info, runner=_leopard(log))
    cyc = [a for a in log if "cycle" in a]
    assert len(cyc) == 1 and cyc[0][cyc[0].index("-C") + 1] == "3" and info["cipher"] == 3


def test_power_action_not_sent_when_no_cipher_answers():
    log = []
    def fake(argv, **kw):
        log.append(argv); return _cp(argv, 1, "", "no session")
    assert power_action("h", [("u", "p")], "cycle", runner=fake) is False
    assert not [a for a in log if "cycle" in a]
