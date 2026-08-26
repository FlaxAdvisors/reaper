import json, os, sys, threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen, Request
sys.path.insert(0, os.path.dirname(__file__))
from qual_battery import Battery, DEFAULT_STAGES
from qual_server import make_handler

# canned tool output so the real DEFAULT_STAGES run deterministically, no hardware
def _fake_runner(argv, timeout):
    j = " ".join(argv)
    if "lsblk -ndo" in j: return 0, "nvme0n1\n"                  # one UNMOUNTED dev -> fio runs
    if "sdr" in j: return 0, "Fan1 | ok\nTemp | ok\n"
    if "sel elist" in j: return 0, "1 | ts | Fan #1 | Lower Critical\n"
    if "dmidecode -s system-serial" in j: return 0, "SN1\n"
    if "dmidecode" in j: return 0, "Handle 0x1\n"
    if "hwinfo" in j: return 0, "hwinfo dump\n"
    if "lscpu" in j: return 0, "Socket(s): 2\nCore(s) per socket: 8\nThread(s) per core: 2\n"
    if "meminfo" in j: return 0, "MemTotal: 100000 kB\nMemAvailable: 90000 kB\n"
    if "smartctl --scan" in j: return 0, "/dev/nvme0 -d nvme\n"
    if "iperf3" in j: return 0, "iperf done 25 Gbits/sec\n"       # server free -> pass, no retry
    if "macinv" in j: return 0, "12 Memory Size: 64 GB\n"
    if "dimmerr" in j: return 0, "DIMM_A0 sn: 1 ce: 0 ue: 0\n"
    return 0, ""


def _serve():
    b = Battery(runner=_fake_runner, stages=DEFAULT_STAGES, mac="aa:bb", serial="SN1")
    b.run()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(b))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return b, srv, "http://127.0.0.1:%d" % srv.server_address[1]


def _g(u): return json.loads(urlopen(u, timeout=5).read().decode())


def test_full_battery_contract_shapes():
    b, srv, base = _serve()
    try:
        h = _g(base + "/health")
        assert set(("ok", "mac", "serial", "run_id", "state", "agent_ver")) <= set(h)
        s = _g(base + "/status")
        assert set(("status", "current", "done_n", "total_n", "verdict", "pct")) <= set(s)
        assert s["total_n"] == len(DEFAULT_STAGES)
        for stg in _g(base + "/stages"):
            assert set(("name", "status", "verdict", "started", "ended")) <= set(stg)
        inv = _g(base + "/stage/inventory")
        assert set(("name", "status", "verdict", "started", "ended", "summary", "artifacts")) <= set(inv)
        names = {a["name"] for a in inv["artifacts"]}
        assert {"dmidecode", "hwinfo", "macinv"} <= names
        for a in inv["artifacts"]:
            assert set(("name", "kind", "bytes")) <= set(a) and a["kind"] in ("raw", "digest")
    finally:
        srv.shutdown()


def test_restart_changes_run_id_over_http():
    b, srv, base = _serve()
    try:
        old = _g(base + "/health")["run_id"]
        urlopen(Request(base + "/restart", method="POST"), timeout=5).read()
        urlopen(Request(base + "/restart/ack", method="POST"), timeout=5).read()
        assert _g(base + "/health")["run_id"] != old
    finally:
        srv.shutdown()
