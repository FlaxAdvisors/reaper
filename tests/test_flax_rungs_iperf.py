import json, re, subprocess, pytest
pytest.importorskip("flax_rungs")
from flax_rungs.iperf import mesh

SRV = {"pos": "L05", "host_ip": "172.32.5.1",
       "ifaces": {"mgmt": ("eno1", "172.32.5.1"), "A": ("e0", "10.201.5.1"), "B": ("e1", "10.202.5.1")}}


def cl(pos, ip, nets=("mgmt", "A", "B")):
    n = int(ip.rsplit(".", 1)[1])
    full = {"mgmt": ("eno1", ip), "A": ("e0", f"10.201.9.{n}"), "B": ("e1", f"10.202.9.{n}")}
    return {"pos": pos, "host_ip": ip, "ifaces": {k: full[k] for k in nets}}


def js(bps):
    return json.dumps({"end": {"sum_received": {"bits_per_second": bps}}})


class Fake:
    def __init__(self, bps=None, speeds=None, server_up=True, stderr=""):
        self.log, self.bps, self.stderr = [], bps or {}, stderr
        self.speeds = speeds or {"eno1": 1000, "e0": 25000, "e1": 25000}
        self.server_up = server_up

    def __call__(self, argv, input=None, **kw):
        host = [a for a in argv if "@" in a][0].split("@")[1]
        self.log.append((host, input))
        out, rc = "", 0
        m = re.search(r"IP=(\S+) PORT=(\d+)", input)
        if m:                                              # start / stop server
            if "iperf3 -s -D" in input and not self.server_up:
                rc = 1
            elif "kill" not in input:
                out = "PID 4242\n"
        elif input.startswith("ethtool"):
            ifn = input.split()[1]
            out = f"Settings for {ifn}:\n\tSpeed: {self.speeds[ifn]}Mb/s\n"
        elif input.startswith("iperf3 -c"):
            port = int(re.search(r"-p (\d+)", input).group(1))
            net = {11001: "mgmt", 11002: "A", 11003: "B"}[port]
            out = js(self.bps[(net, " -R" in input)])
        return subprocess.CompletedProcess(argv, rc, out, self.stderr)


G = 1e9
OK = {("mgmt", False): 941e6, ("mgmt", True): 940e6, ("A", False): 23.4 * G, ("A", True): 23.4 * G,
      ("B", False): 23.4 * G, ("B", True): 23.4 * G}


def test_pass():
    f = Fake(OK)
    r = mesh(SRV, [cl("L37", "172.32.37.1")], user="flax", pw="p", runner=f)
    assert r["L37"].verdict == "pass" and r["L05"].verdict == "pass"
    assert r["L37"].detail["A"]["speed_bps"] == 25e9 and r["L37"].detail["mgmt"]["ok"]
    assert r["L05"].detail["server_pids"] == {"mgmt": ["4242"], "A": ["4242"], "B": ["4242"]}


def test_low_throughput_fails():
    bad = dict(OK); bad[("A", True)] = 15 * G
    r = mesh(SRV, [cl("L37", "172.32.37.1")], user="flax", pw="p", runner=Fake(bad))
    assert r["L37"].verdict == "fail" and not r["L37"].detail["A"]["ok"]
    assert "A:" in r["L37"].detail["error"]


def test_server_down_leaves_clients_unmeasured():
    f = Fake(OK, server_up=False)
    r = mesh(SRV, [cl("L37", "172.32.37.1"), cl("C37", "172.32.37.51")], user="flax", pw="p", runner=f)
    # no client was judged (grey); the server's own start failed (fail)
    assert {k: v.verdict for k, v in r.items()} == {"L37": "unmeasured", "C37": "unmeasured",
                                                    "L05": "fail"}
    assert r["L37"].detail["error"].startswith("server L05 not ready")
    assert not any(i and i.startswith("iperf3 -c") for _, i in f.log)


def test_targets_only_server_ips_and_sequential_and_cleanup():
    f = Fake(OK)
    mesh(SRV, [cl("L37", "172.32.37.1"), cl("C37", "172.32.37.51")], user="flax", pw="p", runner=f)
    allowed = {"172.32.5.1", "10.201.5.1", "10.202.5.1"}
    clients = [(h, i) for h, i in f.log if i and i.startswith("iperf3 -c")]
    assert clients and all(re.search(r"-c (\S+)", i).group(1) in allowed for _, i in clients)
    order = [h for h, _ in clients]
    assert order == ["172.32.37.1"] * 6 + ["172.32.37.51"] * 6      # one client at a time
    assert f.log[-1][0] == "172.32.5.1" and "kill" in f.log[-1][1]  # servers stopped last
    assert sum("kill" in (i or "") for _, i in f.log) == 3


def test_cleanup_on_error():
    class Boom(Fake):
        def __call__(self, argv, input=None, **kw):
            if input.startswith("ethtool"):
                raise RuntimeError("boom")
            return super().__call__(argv, input=input, **kw)
    f = Boom(OK)
    with pytest.raises(RuntimeError):
        mesh(SRV, [cl("L37", "172.32.37.1")], user="flax", pw="p", runner=f)
    assert "kill" in f.log[-1][1]


def test_missing_speed_fails():
    f = Fake(OK)
    f.speeds["eno1"] = 0
    r = mesh(SRV, [cl("L37", "172.32.37.1", ("mgmt",))], user="flax", pw="p", runner=f)
    assert r["L37"].verdict == "fail"


BANNER = ("Warning: Permanently added '172.32.37.1' (ED25519) to the list of known hosts.\r\n"
          "sudo: unable to resolve host localhost: Name or service not known\n")


def test_ssh_stderr_banner_does_not_break_json():
    # live ssh with UserKnownHostsFile=/dev/null prints this on stderr every time
    f = Fake(OK, stderr=BANNER)
    r = mesh(SRV, [cl("L37", "172.32.37.1")], user="flax", pw="p", runner=f)
    assert r["L37"].verdict == "pass", r["L37"].detail
    assert r["L37"].detail["B"]["rx_bps"] == 23.4 * G and r["L05"].verdict == "pass"


def test_no_result_error_carries_stderr_not_parsed():
    bad = dict(OK)
    f = Fake(bad, stderr="ssh: connect to host 172.32.37.1 port 22: No route to host\n")
    f.bps = {}                                    # client gets no JSON at all
    orig = f.__call__
    def call(argv, input=None, **kw):
        if input.startswith("iperf3 -c"):
            return subprocess.CompletedProcess(argv, 255, "", f.stderr)
        return orig(argv, input=input, **kw)
    r = mesh(SRV, [cl("L37", "172.32.37.1")], user="flax", pw="p", runner=call)
    assert r["L37"].verdict == "fail" and "No route to host" in r["L37"].detail["error"]


def test_bps_returns_raw_json():
    from flax_rungs.iperf import _bps
    doc = js(23.4 * G)
    f = Fake(OK, stderr=BANNER)
    f.bps = {("A", False): 23.4 * G}
    bps, err, raw = _bps(cl("L37", "172.32.37.1"), "10.201.9.1", "10.201.5.1", 11002, False,
                         10, 4, "flax", "p", f)
    assert (bps, err) == (23.4 * G, "") and json.loads(raw) == json.loads(doc)
    # mesh keeps each client's -J JSON per network and direction as its artifacts
    r = mesh(SRV, [cl("L37", "172.32.37.1")], user="flax", pw="p", runner=Fake(OK))
    arts = r["L37"].artifacts
    assert sorted(arts) == sorted(f"iperf-{n}-{d}" for n in ("mgmt", "A", "B") for d in ("tx", "rx"))
    assert json.loads(arts["iperf-A-rx"])["end"]["sum_received"]["bits_per_second"] == 23.4 * G
    assert r["L05"].artifacts == {}                     # the server rung links nothing new
