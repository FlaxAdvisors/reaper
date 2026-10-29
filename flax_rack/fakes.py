"""Fake order context for the headless UI check (`RACKPOST_FAKE=1 python -m flax_rack serve`).

Never used in production. The real run_discover runs against a canned mgmt-switch
FDB synthesised from the geometry (locally-administered 02:... MACs, BMC = host+2),
an in-memory Kea and an always-up BMC. C37's host MAC is held by a "manual" Kea
row, so C37 fails its reserve rung and everything else passes. One stray MAC
sits on the first mgmt port no slot or uplink claims, so the unexpected-MAC
alert renders too.

Qualify runs against FakeRungs (below): every node live-boots and passes its battery
and POP (profiles written to a temp dir matching the canned macinv), LLDP shows every
pcieN port where the geometry cables it, and C37's wedge-B iperf is slow -- the one
Qualify fault. The fixture's wedges get fake mgmt_ips so their LLDP can be "read".
"""
import subprocess

from .creds import Creds
from .kea_client import KeaConflict
from .stages import OrderContext

FAIL_POS = "C37"
def _mac(ou: int, col, n: int) -> str:
    return f"02:fa:{ou:02x}:{col or 0:02x}:00:{n:02x}"


def canned_fdb(g) -> tuple[str, set[str]]:
    """(SONiC `show mac` text, MACs the fake Kea refuses)."""
    mgmt = {s.id for s in g.switches if s.role == "mgmt"}
    rows, refuse = [], set()
    for d in g.devices:
        for name, l in sorted(d.links.items()):
            if l.switch not in mgmt:
                continue
            if name == "mezz0":
                host, bmc = _mac(d.ou, d.col, 0x10), _mac(d.ou, d.col, 0x12)
                rows += [(host, l.port), (bmc, l.port)]
                if d.pos == FAIL_POS:
                    refuse.add(host.replace(":", ""))
            elif name.startswith("iom"):
                rows.append((_mac(d.ou, d.col, 0x20 + int(name[3:])), l.port))
    claimed = {l.port for d in g.devices for l in d.links.values() if l.switch in mgmt}
    claimed |= {p for s in g.switches if s.id in mgmt for p in s.uplink_ports}
    stray = next((p for p in range(1, 49) if p not in claimed), None)
    if stray is not None:
        rows.append(("02:fa:ff:ff:00:01", stray))
    lines = ["  No.    Vlan  MacAddress         Port        Type",
             "-----  ------  -----------------  ----------  -------"]
    for i, (mac, port) in enumerate(rows, 1):
        lines.append(f"{i:5d}    1000  {mac.upper()}  Ethernet{port - 1:<4d} Dynamic")
    lines.append(f"Total number of entries {len(rows)}")
    return "\n".join(lines) + "\n", refuse


class FakeKea:
    def __init__(self, refuse=()):
        self.rows: dict[str, dict] = {}
        self.refuse = set(refuse)

    def upsert(self, source, mac, ipv4, hostname, vid):
        if mac in self.refuse:
            raise KeaConflict("mac held by a non-order (manual) row")
        self.rows[mac] = {"source": source, "ipv4": ipv4, "hostname": hostname, "vid": vid}

    def delete_source(self, source):
        gone = [m for m, r in self.rows.items() if r["source"] == source]
        for m in gone:
            del self.rows[m]
        return len(gone)

    def list(self, source):
        return [dict(r, mac=m) for m, r in self.rows.items() if r["source"] == source]


_KEAS: dict[str, FakeKea] = {}


_PROFILE_DIR = None


def fake_context_factory(rack_dir, g, db) -> OrderContext:
    global _PROFILE_DIR
    from flax_post import population
    import tempfile
    text, refuse = canned_fdb(g)
    kea = _KEAS.setdefault(g.kea_source, FakeKea(refuse))
    if _PROFILE_DIR is None:
        _PROFILE_DIR = tempfile.mkdtemp(prefix="rackpost-fake-profiles-")
        population.PROFILE_DIR = _PROFILE_DIR
    write_profiles(_PROFILE_DIR, g)
    g = with_wedge_ips(g)

    def runner(argv, **kw):
        out = ""
        if argv[0] == "sshpass":
            out = text
        elif argv[0] == "ipmitool":
            out = "Chassis Power is on\n"
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    return OrderContext(rack_dir, g, db, kea,
                        Creds(("u", "p"), [("u", "p")], ("flax", "p"), [("u", "p")]), runner,
                        FakeRungs(g, delay_s=0.5))


# ---------------------------------------------------------------- canned Qualify
IPERF_FAIL = {"C37": "B"}            # C37's wedge-B iperf is slow: the one Qualify fault
_IFN = {"pcie0": "ens3f0np0", "pcie1": "ens3f1np1"}
_MACINV = ("2 Processor Version: Intel(R) Xeon(R) Gold 6138 CPU @ 2.00GHz\n"
           "12 Memory Size: 32 GB, Type: DDR4\n"
           "2 Ethernet controller: Mellanox Technologies MT27710 Family [ConnectX-4 Lx]\n")
FAKE_PROFILE_RULES = ("2 Processor Version: .*Gold 6138", "12 Memory Size: 32 GB",
                      "Ethernet controller: Mellanox.*ConnectX-4 Lx")


def write_profiles(profile_dir, g) -> None:
    """Every POP profile this rack names, matching the canned macinv (fake mode/tests)."""
    from .qualify import pop_default
    import os
    os.makedirs(profile_dir, exist_ok=True)
    for d in g.devices:
        with open(os.path.join(profile_dir, pop_default(g, d)), "w") as f:
            f.write("\n".join(FAKE_PROFILE_RULES) + "\n")


def with_wedge_ips(g):
    """The fixture's wedges carry no mgmt_ip yet; give fake ones so LLDP can be 'read'."""
    import dataclasses
    sw = [s if s.mgmt_ip or s.role != "tor" else dataclasses.replace(s, mgmt_ip=f"fake-wedge-{s.id}")
          for s in g.switches]
    return dataclasses.replace(g, switches=sw)


class FakeRungs:
    """Stand-ins for flax_rack.qualify.real_rungs(): every node boots, every battery
    stage passes, LLDP shows each pcieN port where the geometry cables it."""

    def __init__(self, g, *, fail_pxe=(), iperf_fail=None, delay_s=0.0, wrong_host=(),
                 poll_timeout=(), stop_fail=()):
        from .addressing import plan_addresses
        self.g, self.fail_pxe, self.delay_s = g, set(fail_pxe), delay_s
        self.wrong_host, self.poll_timeout = set(wrong_host), set(poll_timeout)
        self.stop_fail = set(stop_fail)
        self.iperf_fail = IPERF_FAIL if iperf_fail is None else iperf_fail
        self.pos_of = {a.ip: a.pos for a in plan_addresses(g)}
        self.calls = []

    def _pause(self):
        if self.delay_s:
            import time
            time.sleep(self.delay_s)

    def _macs(self, pos):
        d = self.g.device(pos)
        out = {"eno1": _mac(d.ou, d.col, 0x10)}
        for k, (link, ifn) in enumerate(sorted(_IFN.items()), 1):
            if link in d.links:
                out[ifn] = f"02:fb:{d.ou:02x}:{d.col or 0:02x}:00:{k:02x}"
        return out

    def boot_live(self, bmc_ip, host_ip, creds, *, expect_mac=None, runner=None):
        from flax_rungs.result import RungResult
        pos = self.pos_of.get(host_ip)
        self.calls.append(("boot_live", pos, expect_mac))
        self._pause()
        if not expect_mac:
            return RungResult("fail", {"error": "no discovered host MAC (discover first)"})
        if pos in self.wrong_host:
            return RungResult("fail", {"error": f"wrong host at {host_ip}: BOOTIF "
                                                f"02:fa:ff:ff:00:99 != discovered {expect_mac}"})
        if pos in self.fail_pxe:
            return RungResult("fail", {"error": "no ssh within 900s",
                                       "last": {"power": "on", "ping": False, "ssh": "rc=255"}})
        return RungResult("pass", {"override": "redfish", "power_action": "cycle",
                                   "ssh_after_s": 212, "cmdline_ok": True})

    def launch(self, host_ip, user, pw, *, runner=None):
        from flax_rungs.result import RungResult
        self.calls.append(("launch", self.pos_of.get(host_ip)))
        return RungResult("pass", {"mode": "fresh"})

    def stop_agent(self, host_ip, user, pw, *, runner=None):
        from flax_rungs.result import RungResult
        pos = self.pos_of.get(host_ip)
        self.calls.append(("stop_agent", pos))
        if pos in self.stop_fail:
            return RungResult("fail", {"error": "agent stop rc=255"})
        return RungResult("pass", {"stopped": True})

    def poll(self, host_ip, *, deadline_s):
        from flax_rungs.agent import ORDER_STAGES
        from flax_rungs.result import RungResult
        pos = self.pos_of[host_ip]
        self.calls.append(("poll", pos, deadline_s))
        self._pause()
        if pos in self.poll_timeout:            # inventory done, stress still running
            d = self.g.device(pos)
            out = {}
            for n in ORDER_STAGES.split(","):
                done = n in ("sdr-pre", "sel-pre", "sel-clear", "tooling")
                out[n] = RungResult("pass" if done else "fail",
                                    {"stage": n, "run_id": f"fake{d.ou:02d}{d.col or 0}",
                                     **({} if done else {"error": "timeout"})})
            return out
        d = self.g.device(pos)
        run_id = f"fake{d.ou:02d}{d.col or 0}"
        head = d.kind.startswith("bc-head")
        macs = self._macs(pos)
        link = "".join(f"{i}: {n}: <BROADCAST,MULTICAST,UP> mtu 1500\n    link/ether {m} brd ff:ff:ff:ff:ff:ff\n"
                       for i, (n, m) in enumerate(macs.items(), 2))
        addr = (f"2: eno1: <BROADCAST,MULTICAST,UP> mtu 1500\n    link/ether {macs['eno1']}\n"
                f"    inet {host_ip}/16 brd 172.{self.g.vid}.255.255 scope global eno1\n")
        dmi = ("Handle 0x0000, DMI type 0, 26 bytes\nBIOS Information\n\tVendor: American Megatrends Inc.\n"
               f"\tVersion: {'F09_3A20' if head else 'F09_3A23'}\n\tRelease Date: "
               f"{'03/11/2020' if head else '07/29/2021'}\n\n")
        out = {}
        for n in ORDER_STAGES.split(","):
            arts = {"macinv": _MACINV, "dmidecode": dmi, "ip-d-link": link,
                    "ip-d-address": addr} if n == "inventory" else {f"{n}": f"{n}: ok\n"}
            out[n] = RungResult("pass", {"stage": n, "run_id": run_id, "summary": {}}, arts)
        return out

    def read_lldp(self, ip, user, pw, *, runner=None):
        from dataclasses import asdict
        from flax_rungs.result import RungResult
        from flax_rungs.sonic import LldpEntry, wedge_iface
        sw = next(s.id for s in self.g.switches if s.mgmt_ip == ip)
        self.calls.append(("read_lldp", sw))
        rows = []
        for d in self.g.devices:
            for name, l in sorted(d.links.items()):
                if name in _IFN and l.switch == sw:
                    rows.append(LldpEntry(wedge_iface(l.port, l.lane),
                                          f"{self.g.prefix}{d.pos.lower()}",
                                          self._macs(d.pos)[_IFN[name]], _IFN[name]))
        text = "".join(f"{e.local_iface:12s} {e.remote_device:15s} {e.remote_port}  R  "
                       f"{e.remote_descr}\n" for e in rows)
        return RungResult("pass", {"lldp": [asdict(e) for e in rows], "count": len(rows)},
                          {"show-lldp-table.txt": text})

    def apply_fabric(self, host_ip, user, pw, assigns, *, runner=None):
        from flax_rungs.result import RungResult
        self.calls.append(("apply_fabric", self.pos_of.get(host_ip), dict(assigns)))
        if not assigns:
            return RungResult("fail", {"error": "no fabric ports to address"})
        return RungResult("pass", dict(assigns))

    def mesh(self, server, clients, *, user, pw, runner=None):
        from flax_rungs.result import RungResult
        self.calls.append(("mesh", server["pos"], server["host_ip"], [c["pos"] for c in clients]))
        self._pause()
        out = {}
        for c in clients:
            nets, errs = {}, []
            for net in c["ifaces"]:
                speed = 1e9 if net == "mgmt" else 25e9
                bps = 0.94 * speed
                if self.iperf_fail.get(c["pos"]) == net:
                    bps = 0.36 * speed
                    errs.append(f"{net}: tx {round(bps / 1e6)} / rx {round(bps / 1e6)} Mbit/s < "
                                f"{round(0.8 * speed / 1e6)} (0.8 x {round(speed / 1e6)})")
                nets[net] = {"tx_bps": bps, "rx_bps": bps, "speed_bps": speed,
                             "ok": self.iperf_fail.get(c["pos"]) != net}
            out[c["pos"]] = RungResult("fail" if errs else "pass",
                                       {**nets, **({"error": "; ".join(errs)} if errs else {})})
        out[server["pos"]] = RungResult("pass", {"served": [c["pos"] for c in clients]})
        return out

    def mc_info(self, bmc_ip, creds, *, runner=None):
        pos = self.pos_of.get(bmc_ip)
        head = pos and self.g.device(pos).kind.startswith("bc-head")
        return {"firmware": "2.61" if head else "2.66", "aux": "0x00 0x00 0x00 0x00"}

    def host_run(self, host_ip, user, pw, script, *, runner=None):
        return 0, "", ""
