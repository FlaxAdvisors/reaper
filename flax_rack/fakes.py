"""Fake order context for the headless UI check (`RACKPOST_FAKE=1 python -m flax_rack serve`).

Never used in production. The real run_discover (discover_ladder) runs against
FakeRungs' Discover stand-ins: a canned mgmt-switch FDB synthesised from the geometry
(locally-administered 02:... MACs, BMC = host+2; a powered-off node shows only its
BMC), an in-memory Kea whose leases follow its reservations, BMCs that power their
node on, and a virtual clock on which a power-on is followed by tftp/iPXE/ISO log
marks and a fresh live boot. LIVE_POS is already in the live image (no power action;
its boot proven at ssh). C37's host MAC is held by a "manual" Kea row, so C37 fails
bmc-reserved and the rest of its ladder skips. The switches get site_ports on a fake
turtle (wedges: SONiC + BMC MAC, the Dell: SONiC) with dynamic leases; each wedge BMC
answers weutil + /etc/issue + cpld_rev (artifacts too), each SONiC side a syseeprom
with part/product/base MAC, and each JBOD IOM fruid-util + fw-util (one part NA).
One stray MAC sits on the first mgmt port no slot or uplink claims, so the
unexpected-MAC alert renders too.

Qualify runs against FakeRungs (below): every node live-boots and passes each phase's
battery, population-check (profiles written to a temp dir matching the canned macinv)
and the ship checks (canned FRU 0 serial = SMBIOS serial, a cleared TPM 2.0); its SOL
"capture" is a few canned console lines written to the order dir. LLDP shows
every pcieN port where the geometry cables it, and C37's wedge-B iperf is slow. (C37's
failed reserve also stops Qualify booting it -- pxe-live "discover first" -- so in the UI
the Qualify fault shown is that gate.) Each agent phase takes a few seconds and R05 is
slower than the rest, so the header's phase/barrier indicator can be watched walking
boot -> inventory -> storage -> burn -> network. The fixture's wedges get fake mgmt_ips
so their LLDP can be "read".

Firmware (detect only) runs the real fwcheck readers against FakeRungs' manifests, a
fake Redfish BMC and canned dmidecode/mstflint output: R05's BMC and R37's BIOS are
off target, L37's Mellanox card has its UEFI ROM off; the rest are at target. In the
served fake (fake_context_factory) R05's host stops answering ssh for the firmware
reads (FW_HOST_DOWN), so its bios/mlx pairs render grey "not measured". One
FakeRungs lives per rack for the server's life, so Discover's boots carry over.

Deploy runs against FakeRungs' install/firstboot/flax_user/bond_config/ha_test stand-ins
(the real solscan reads the canned console the install appends to the SOL file). In the
served fake R05's inventory lacks the Mellanox line (POP_FAIL), so its population-check
is red and Deploy needs a recorded waiver for it; R37's install console carries a
"Buffer I/O error" line (SOL_FINDINGS), so its install-console fails (it never gates).
Knobs for tests: install_fail, firstboot_fail, sol_findings, bond_fail, hatest_fail.

Done runs against FakeRungs' end_summary (canned SDR + SEL judged by the real
BENIGN_SEL rule; the in-band captures only when Done passes a host IP), pxe_remove (a
real pxeboot.remove under pxe_dir when set, else recorded only) and clear_override. In
the served fake R37's SEL holds a correctable-ECC entry (SEL_NONBENIGN): its end-summary
is red. Knobs: sel_nonbenign, sel_unread (SEL not read: unmeasured), override_fail,
pxe_first (the installed BootOrder starts with a PXE entry), pxe_dir.
"""
import subprocess
from pathlib import Path

from .creds import Creds
from .kea_client import KeaConflict
from .stages import OrderContext

FAIL_POS = "C37"
FW_HOST_DOWN = {"R05"}               # served fake only: ssh gone for the firmware reads
LIVE_POS = "L05"                     # already in the live image when Discover starts
POP_FAIL = {"R05"}                   # served fake: POP red (Deploy needs a recorded waiver)
SOL_FINDINGS = {"R37"}               # served fake: an I/O error line in its install console
SEL_NONBENIGN = {"R37"}              # served fake: a correctable-ECC entry in its Done SEL
SITE_PORTS = {"A": "Gi1/0/18", "B": "Gi1/0/17", "C": "Gi1/0/19"}


def _mac(ou: int, col, n: int) -> str:
    return f"02:fa:{ou:02x}:{col or 0:02x}:00:{n:02x}"


def canned_fdb(g, hosts_up=None) -> tuple[str, set[str]]:
    """(SONiC `show mac` text, MACs the fake Kea refuses). hosts_up: the positions whose
    host MAC is on the wire (None = every one); the rest show only their BMC."""
    mgmt = {s.id for s in g.switches if s.role == "mgmt"}
    rows, refuse = [], set()
    for d in g.devices:
        for name, l in sorted(d.links.items()):
            if l.switch not in mgmt:
                continue
            if name == "mezz0":
                host, bmc = _mac(d.ou, d.col, 0x10), _mac(d.ou, d.col, 0x12)
                rows += [(host, l.port)] if hosts_up is None or d.pos in hosts_up else []
                rows += [(bmc, l.port)]
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
    """Reservations in memory; a reserved MAC's lease is its reservation (the device
    renewed), `dyn` holds the dynamic leases (the switches on the site pool)."""

    def __init__(self, refuse=(), dyn=None):
        self.rows: dict[str, dict] = {}
        self.refuse = set(refuse)
        self.dyn = dict(dyn or {})
        self.archives: dict[tuple, dict] = {}      # (order_id, rack) -> the last payload

    def archive(self, payload):
        self.archives[(payload["order_id"], payload["rack"])] = payload
        return {k: len(payload[k]) for k in ("devices", "components", "macs", "rungs")}

    def leases(self, macs):
        out = {}
        for m in macs:
            if m in self.rows:
                out[m] = {"ip": self.rows[m]["ipv4"], "expire": None, "state": 0,
                          "subnet_id": self.rows[m]["vid"], "hostname": self.rows[m]["hostname"]}
            elif m in self.dyn:
                out[m] = {"ip": self.dyn[m], "expire": None, "state": 0, "subnet_id": 26,
                          "hostname": ""}
        return out

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
_RUNGS: dict[str, "FakeRungs"] = {}


_PROFILE_DIR = None


def fake_context_factory(rack_dir, g, db) -> OrderContext:
    global _PROFILE_DIR
    from flax_post import population
    import tempfile
    g = with_site_ports(g)
    _text, refuse = canned_fdb(g)
    # one FakeRungs per rack for the server's life: a node Discover booted is still in
    # the live image when Firmware (or Qualify) runs next
    rungs = _RUNGS.get(g.kea_source)
    if rungs is None:
        # the wedges' fake mgmt IPs too: read_lldp finds its switch by them
        rungs = _RUNGS[g.kea_source] = FakeRungs(with_wedge_ips(g), delay_s=0.5, poll_delay_s=2.5,
                                                 slow={"R05": 2.5}, live={LIVE_POS},
                                                 pop_fail=POP_FAIL)
        rungs.fw_host_down = set(FW_HOST_DOWN)
        rungs.sol_findings = set(SOL_FINDINGS)
        rungs.sel_nonbenign = set(SEL_NONBENIGN)
        rungs.pull_file = Path(rack_dir) / ".fake-pull"
    kea = _KEAS.setdefault(g.kea_source, FakeKea(refuse))
    kea.dyn.update(rungs.switch_leases())
    if _PROFILE_DIR is None:
        _PROFILE_DIR = tempfile.mkdtemp(prefix="rackpost-fake-profiles-")
        population.PROFILE_DIR = _PROFILE_DIR
    write_profiles(_PROFILE_DIR, g)
    g = with_wedge_ips(g)

    def runner(argv, **kw):               # every rung is a FakeRungs stand-in
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    return OrderContext(rack_dir, g, db, kea,
                        Creds(("u", "p"), [("u", "p")], ("flax", "p"), [("u", "p")],
                              ("u", "p"), ("u", "p")), runner, rungs)


# ---------------------------------------------------------------- canned Qualify
IPERF_FAIL = {"C37": "B"}            # C37's wedge-B iperf is slow: the one Qualify fault
_IFN = {"pcie0": "ens3f0np0", "pcie1": "ens3f1np1"}
_MACINV = ("2 Processor Version: Intel(R) Xeon(R) Gold 6138 CPU @ 2.00GHz\n"
           "12 Memory Size: 32 GB, Type: DDR4\n"
           "2 Ethernet controller: Mellanox Technologies MT27710 Family [ConnectX-4 Lx]\n")


def _macinv_v(sn: str, n: int, nic: bool = True) -> str:
    """`macinv -v` detail text (the components Done lists): 2 CPUs, 2 DIMMs, the
    Mellanox (dropped with POP_FAIL), 2 NVMe disks, the BMC VGA."""
    return (f"Board Mfg: Wiwynn, Product: Tioga Pass Single Side, Serial: B{sn}, "
            f"Product Serial: {sn}, Chassis Serial: C{sn}\n"
            + "".join("Processor Version: Intel(R) Xeon(R) Gold 6138 CPU @ 2.00GHz, "
                      "Serial: To be filled by O.E.M.\n" for _ in range(2))
            + "".join(f"Memory: 32 GB, DIMM {s}, _Node{k}_Channel0_Dimm0, 2666 MT/s, Samsung, "
                      f"{0xF00D0000 + n * 16 + k:08X}, M393A4K40CB2-CTD\n"
                      for k, s in enumerate(("A0", "D0")))
            + (f'BusID: 5e:00.0, Eth MAC: 02:00:00:00:{n:02x}:01, Model: "Mellanox MCX4411A-ACQN, '
               'ConnectX-4 Lx EN OCP, 1x25Gb", SubVendor: pci 0x15b3 "Mellanox Technologies", '
               "Interface: pxedev\n"
               f"BusID: 5e:00.0, [SN] Serial number: MT{n:04d}X0{n:04d}\n" if nic else "")
            + "".join(f'Handler: {190 + k}, BusID: nvme{k}, Model: "Sandisk WD Black SN850", '
                      'SubVendor: pci 0x15b7 "Sandisk Corp", SubDevice: pci 0x5011 , '
                      f'Serial ID: "2228158{n:03d}{k:02d}", Capacity: 1863 GB\n' for k in range(2))
            + 'BusID: 03:00.0, Model: "ASPEED AST1000/2000", Device: pci 0x2000 "AST1000/2000", '
              "SubDevice: pci 0x2000 , Revision: 0x41\n")


# plain `lsblk` as the inventory captures it: two unmounted NVMe disks for fio
_LSBLK = ("NAME        MAJ:MIN RM   SIZE RO TYPE MOUNTPOINTS\n"
          "loop0         7:0    0 850.2M  1 loop /run/overlay/squashfs_container\n"
          "nvme0n1     259:0    0   1.7T  0 disk \n"
          "nvme1n1     259:1    0   1.7T  0 disk \n"
          "└─nvme1n1p1 259:2    0   1.7T  0 part \n")
_DIMMSUM = "  32 GB  DIMM A0  _Node0_Channel0_Dimm0  2666   Samsung  0000F00D  M393A4K40CB2-CTD\n"
_TPMSTATE = "device=present\nowned=0 phEnable=1 shEnable=1 ehEnable=1 phEnableNV=1 disableClear=0\n"
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


def with_site_ports(g, ports=None):
    """Give each switch its turtle port (fake mode/tests; Task 7 sets the real ones)."""
    import dataclasses
    ports = SITE_PORTS if ports is None else ports
    sw = [dataclasses.replace(s, site_port={"switch": "turtle-gouda", "port": ports[s.id]})
          if s.site_port is None and s.id in ports else s for s in g.switches]
    return dataclasses.replace(g, switches=sw)


def with_wedge_ips(g):
    """The fixture's wedges carry no mgmt_ip yet; give fake ones so LLDP can be 'read'."""
    import dataclasses
    sw = [s if s.mgmt_ip or s.role != "tor" else dataclasses.replace(s, mgmt_ip=f"fake-wedge-{s.id}")
          for s in g.switches]
    return dataclasses.replace(g, switches=sw)


class FakeRungs:
    """Stand-ins for flax_rack.qualify.real_rungs(): every node boots, every battery
    stage passes, LLDP shows each pcieN port where the geometry cables it. poll returns
    the stages the last launch on that host asked for (FLAX_QUAL_STAGES); a host in
    poll_timeout times out once cpu-mem-stress is among them (the burn phase)."""

    def __init__(self, g, *, fail_pxe=(), iperf_fail=None, delay_s=0.0, wrong_host=(),
                 poll_timeout=(), stop_fail=(), poll_delay_s=0.0, slow=None, sol_fail=(),
                 live=(), fdb_runner=False, pop_fail=()):
        from .addressing import plan_addresses
        self.g, self.fail_pxe, self.delay_s = g, set(fail_pxe), delay_s
        self.wrong_host, self.poll_timeout = set(wrong_host), set(poll_timeout)
        self.stop_fail, self.sol_fail = set(stop_fail), set(sol_fail)
        self.poll_delay_s, self.slow = poll_delay_s, dict(slow or {})
        self.stages_of: dict[str, list[str]] = {}
        self.iperf_fail = IPERF_FAIL if iperf_fail is None else iperf_fail
        self.pos_of = {a.ip: a.pos for a in plan_addresses(g)}
        self.calls = []
        self.fdb_runner = fdb_runner           # True: the FDB is read through ctx.runner
        self.host_macs: dict[str, str] = {}    # pos -> host MAC12 (BOOTIF), else canned
        # Firmware: R05's BMC and R37's BIOS are off target, L37's NIC has its UEFI ROM off
        self.fw_bmc_ver = {"R05": "flax-onetree-1.1.3"}
        self.fw_bios_ver = {"R37": "TPC_P26C"}
        self.fw_nic_uefi_off = {"L37"}
        self.fw_host_down: set[str] = set()   # hosts whose firmware-read ssh times out
        self.pop_fail = set(pop_fail)          # inventory lacks the Mellanox line: POP red
        # Deploy: installs that never power off, first boots refused, SOL error lines,
        # bonds that do not come up, nodes whose ha-test fails
        self.install_fail: set[str] = set()
        self.firstboot_fail: set[str] = set()
        self.sol_findings: set[str] = set()
        self.bond_fail: set[str] = set()
        self.hatest_fail: set[str] = set()
        # Done: a non-benign SEL entry, a BMC whose SEL cannot be read, a Redfish that
        # refuses the override clear; pxe_dir = where pxe_remove really unlinks (None:
        # recorded only)
        self.sel_nonbenign: set[str] = set()
        self.sel_unread: set[str] = set()
        self.override_fail: set[str] = set()
        self.pxe_first: set[str] = set()        # efibootmgr BootOrder starts with PXE
        self.pxe_dir = None
        self._discover_init(live)

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

    # ------------------------------------------------------------ Discover stand-ins
    def _discover_init(self, live):
        import threading
        import time
        from .addressing import plan_addresses
        self.mu = threading.Lock()
        self.t = time.time()                   # the virtual clock (sleep advances it)
        self.ip_role = {a.ip: (a.pos, a.role) for a in plan_addresses(self.g)}
        self.power = {d.pos: "off" for d in self.g.devices if d.endpoints.get("bmc")}
        self.boots: dict[str, dict] = {}       # pos -> {boot_id, kernel, tftp, ipxe, iso}
        self.down: set[str] = set()            # IPs that never answer ping
        self.no_marks: set[str] = set()        # positions whose boots leave no log marks
        self.nboot = 0
        self.sw_macs: dict[str, dict] = {}     # switch id -> {role: mac12}
        for i, s in enumerate(self.g.switches):
            ep = s.endpoints
            self.sw_macs[s.id] = {r: f"02fc{s.ou:02x}0000{k:02x}"
                                  for k, r in enumerate(("sonic", "bmc"), 1) if ep.get(r)}
        for pos in live:
            self.power[pos] = "on"
            self._new_boot(pos, self.t - 3600)

    def switch_leases(self) -> dict[str, str]:
        out, n = {}, 50
        for s in self.g.switches:
            for _r, m in sorted(self.sw_macs.get(s.id, {}).items()):
                out[m] = f"172.26.0.{n}"
                n += 1
        return out

    def _new_boot(self, pos, at):
        with self.mu:
            self.nboot += 1
            n = self.nboot
        self.boots[pos] = {"boot_id": f"00000000-0000-4000-8000-{n:012x}", "kernel": at + 150,
                           "tftp": at + 20, "ipxe": at + 90, "iso": at + 160}

    def forget_boots(self):
        """Every live node rebooted since Discover (new boot_ids, nothing else changes)."""
        for pos, b in list(self.boots.items()):
            self._new_boot(pos, b["kernel"] - 150)

    def wall(self):
        return self.t

    clock = wall

    def sleep(self, s):
        with self.mu:
            self.t += s

    def site_switches(self):
        return {"turtle-gouda": "fake-turtle"}

    def read_fdb(self, host, user, pw, *, runner=None):
        from dataclasses import asdict
        from flax_rungs.result import RungResult
        from flax_rungs.sonic import FdbEntry, parse_show_mac, read_fdb
        self.calls.append(("read_fdb", host))
        if self.fdb_runner:                    # e.g. the RRORD-101 Dell fixture
            from .discover import match
            r = read_fdb(host, user, pw, runner=runner)
            if r.verdict == "pass":
                sw = next(s.id for s in self.g.switches if s.mgmt_ip == host)
                m = match(self.g, [FdbEntry(**e) for e in r.detail["fdb"]], sw)
                self.host_macs.update({p: x.host_mac for p, x in m.devices.items() if x.host_mac})
            return r
        text, _ = canned_fdb(self.g, hosts_up={p for p, b in self.boots.items()
                                               if b["kernel"] <= self.t})
        e = parse_show_mac(text)
        return RungResult("pass", {"fdb": [asdict(x) for x in e], "count": len(e)},
                          {"show-mac.txt": text})

    def read_ifstatus(self, host, user, pw, *, runner=None):
        from flax_rungs.result import RungResult
        st = {f"Ethernet{p}": {"oper": "up", "admin": "up", "speed": "1G"} for p in range(52)}
        return RungResult("pass", {"status": st, "count": len(st)})

    def mac_table(self, host, user, pw, ports, *, ping_ips=(), runner=None):
        from flax_rungs.result import RungResult
        self.calls.append(("mac_table", host, tuple(ports)))
        got = {}
        for p in ports:
            s = next((s for s in self.g.switches if (s.site_port or {}).get("port") == p), None)
            got[p] = sorted(self.sw_macs.get(s.id, {}).values()) if s else []
        rows = [[p, ", ".join(v) or "no MAC"] for p, v in got.items()]
        empty = [p for p, v in got.items() if not v]
        det = {"ports": got, "pinged": {}, "reread": False, "rows": rows, "notes": [],
               "headline": f"{sum(map(len, got.values()))} MAC(s)"}
        if empty:
            det["error"] = det["headline"] = f"no MAC on {', '.join(empty)}"
            return RungResult("fail", det)
        return RungResult("pass", det, {"show-mac.txt": "fake turtle\n"})

    def ping(self, ip, *, runner=None):
        if ip in self.down:
            return False
        pos, role = self.ip_role.get(ip, (None, None))
        if role == "host":
            b = self.boots.get(pos)
            return bool(b and b["kernel"] <= self.t)
        return True

    def power_status(self, ip, creds, *, info=None, runner=None):
        pos, _ = self.ip_role.get(ip, (None, None))
        if info is not None:
            info.update(user="u", cipher=17)
        return self.power.get(pos)

    def power_action(self, ip, creds, action, *, info=None, runner=None):
        pos, _ = self.ip_role.get(ip, (None, None))
        self.calls.append(("power_action", pos, action))
        self.power[pos] = "on"
        self._new_boot(pos, self.t)
        self.sleep(240)                        # the boot happens
        return True

    def set_boot_pxe(self, ip, creds, *, enable=True, ipmi_creds=None, runner=None):
        from flax_rungs.result import RungResult
        self.calls.append(("set_boot_pxe", self.ip_role.get(ip, (None,))[0]))
        return RungResult("pass", {"method": "redfish", "enable": enable})

    def boot_marks(self, host_ip, since):
        pos, _ = self.ip_role.get(host_ip, (None, None))
        b = self.boots.get(pos) if pos not in self.no_marks else None
        out = {k: ([b[k]] if b and b[k] >= since and b[k] <= self.t else [])
               for k in ("tftp", "ipxe", "iso")}
        out.update(sources={"tftp": ["/var/log/dnsmasq/dnsmasq.log"],
                            "ipxe": ["/var/log/nginx/access.log"],
                            "iso": ["/var/log/nginx/access.log"]},
                   rotated_tftp=[], readable={})
        return out

    def _fru0(self, pos):
        d = self.g.device(pos)
        return ("FRU Device Description : Builtin FRU Device (ID 0)\n Product Name          : "
                f"Tioga Pass Single Side\n Product Serial        : FAKE{d.ou:02d}{d.col or 0}\n\n")

    def tp_fru(self, ip, creds, *, runner=None):
        from flax_rungs.identity import _tp_result
        pos, _ = self.ip_role.get(ip, (None, None))
        self.calls.append(("tp_fru", pos))
        return _tp_result(self._fru0(pos), "u", 17)

    def iom(self, ip, user, pw, *, runner=None):
        from flax_rungs.result import RungResult
        pos, role = self.ip_role.get(ip, (None, None))
        sn = f"J49FAKE{role[3:]}"
        text = (f"Board Mfg            : Mitac\nBoard Product        : BCIOM001\n"
                f"Board Serial         : {sn}\nBoard Part Number    : 33S5BCI0000\n"
                "Product Serial       : N/A\n")
        vers = {"BMC Version": "fbttn-v2019.52.0", "ROM Version": "fbttn-v2.0",
                "TPM Version": "133.33", "Fan Speed Controller Version": "0x0c",
                "Mellanox NIC firmware version": "14.20.1202", "Expander Version": "0.3.6"}
        absent = {"SCC Version": "Server Power-off"}
        fwu = "".join(f"{k}: {v}\n" for k, v in vers.items()) + "SCC Version: NA (Server Power-off)\n"
        return RungResult("pass", {
            "serial": sn, "serial_field": "Board Serial", "phase": "unsupported",
            "board_product": "BCIOM001", "part_number": "33S5BCI0000",
            "versions": vers, "not_present": absent, "errors": {},
            "headline": f"SN {sn} · BMC fbttn-v2019.52.0",
            "rows": [["Board Serial", sn], ["Board Product", "BCIOM001"],
                     ["Board Part Number", "33S5BCI0000"], *([k, v] for k, v in vers.items()),
                     *([k, f"not present ({w})"] for k, w in absent.items())],
            "notes": ["identity is the IOM's Board Serial (its Product Serial is N/A)"]},
            {"fruid-util-iom.txt": text, "fw-util-bmc.txt": fwu, "fw-util-all.txt": fwu})

    def _sw_of(self, ip):
        leases = self.switch_leases()
        for s in self.g.switches:
            for r, m in self.sw_macs.get(s.id, {}).items():
                if leases.get(m) == ip:
                    return s, r, m
        return None, None, None

    def wedge_bmc(self, ip, user, pw, *, syseeprom=None, runner=None):
        from flax_rungs.result import RungResult
        s, role, m = self._sw_of(ip)
        if role != "bmc":
            return RungResult("fail", {"error": "weutil: rc=127 command not found",
                                       "headline": f"wedge BMC {ip} not read", "rows": [],
                                       "notes": []})
        sn = f"AIFAKE{s.id}"
        mac = ":".join(m[i:i + 2] for i in range(0, 12, 2))
        sonic = self.sw_macs[s.id].get("sonic") or ""
        ext = ":".join(sonic[i:i + 2] for i in range(0, 12, 2))
        vers = {"OpenBMC": "wedge100-v29", "CPLD": "6.101"}
        rows = [["Product Serial Number", sn], ["Product Name", "WEDGE100S12V"],
                ["Local MAC (BMC)", mac], ["Extended MAC Base", ext],
                ["Product Asset Tag", f"FAKEASSET{s.id}"], *([k, v] for k, v in vers.items())]
        if syseeprom is not None:
            rows.append(["SONiC syseeprom serial", f"{syseeprom.get('serial')} (matches)"])
        return RungResult("pass", {
            "serial": sn, "serial_field": "Product Serial Number", "product_name": "WEDGE100S12V",
            "local_mac": mac, "ext_mac_base": ext, "asset_tag": f"FAKEASSET{s.id}",
            "versions": vers, "errors": {}, "phase": "unsupported",
            "syseeprom_serial": (syseeprom or {}).get("serial"),
            "headline": f"SN {sn} · wedge100-v29", "rows": rows,
            "notes": ["no firmware manifest exists for Facebook OpenBMC wedges: versions are "
                      "report only"]},
            {"weutil.txt": f"Product Name: WEDGE100S12V\nProduct Serial Number: {sn}\n"
                           f"Local MAC: {mac}\nExtended MAC Base: {ext}\n",
             "issue.txt": "OpenBMC Release wedge100-v29\n", "cpld.txt": "6.101\n"})

    def read_syseeprom(self, ip, user, pw, *, runner=None):
        from flax_rungs.result import RungResult
        s, role, _m = self._sw_of(ip)
        if role != "sonic":
            return RungResult("fail", {"error": "rc=255 permission denied", "headline": "x",
                                       "rows": [], "notes": []})
        sn = f"AIFAKE{s.id}" if s.model == "wedge100s" else "CNFAKE0C"
        name, part = (("WEDGE100S12V", "20-001784") if s.model == "wedge100s"
                      else ("N3248TE-ON", "0F2XYZ"))
        base = ":".join(_m[i:i + 2] for i in range(0, 12, 2))
        return RungResult("pass", {
            "serial": sn, "service_tag": "FAKETAG", "base_mac": base, "product_name": name,
            "part_number": part, "headline": f"SN {sn}",
            "rows": [["Serial Number", sn], ["Service Tag", "FAKETAG"], ["Product Name", name],
                     ["Part Number", part], ["Base MAC Address", base]],
            "notes": ["read with `show platform syseeprom` (TlvInfo EEPROM)"]},
            {"syseeprom.txt": f"Product Name 0x21 {len(name)} {name}\n"
                              f"Part Number 0x22 {len(part)} {part}\n"
                              f"Serial Number 0x23 10 {sn}\nBase MAC Address 0x24 6 {base}\n"})

    def _probe_out(self, pos, host_ip):
        b = self.boots.get(pos)
        if not b or b["kernel"] > self.t:
            return None
        d = self.g.device(pos)
        h = self.host_macs.get(pos) or _mac(d.ou, d.col, 0x10).replace(":", "")
        mac = "-".join(h[i:i + 2] for i in range(0, 12, 2))
        if pos in self.wrong_host:
            mac = "02-fa-ff-ff-00-99"
        return (f"{b['boot_id']}\n{self.t - b['kernel']:.2f}\n"
                f"BOOT_IMAGE=/vmlinuz BOOTIF=01-{mac} pxeaddr={host_ip} rd.live\n")

    # ------------------------------------------------------------ Qualify stand-ins
    def boot_live(self, bmc_ip, host_ip, creds, *, expect_mac=None, deadline_s=None,
                  runner=None):
        from flax_rungs.result import RungResult
        pos = self.pos_of.get(host_ip)
        self.calls.append(("boot_live", pos, expect_mac))
        if pos in self.power and pos not in self.wrong_host and pos not in self.fail_pxe:
            self._new_boot(pos, self.t - 240)  # a fresh boot (its kernel already up)
            self.power[pos] = "on"
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

    def launch(self, host_ip, user, pw, *, stages=None, runner=None):
        from flax_rungs.agent import ORDER_STAGES
        from flax_rungs.result import RungResult
        pos = self.pos_of.get(host_ip)
        stages = stages or ORDER_STAGES
        self.calls.append(("launch", pos, stages))
        self.stages_of[pos] = stages.split(",")
        return RungResult("pass", {"mode": "fresh", "stages": stages})

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
        stages = self.stages_of.get(pos) or ORDER_STAGES.split(",")
        self.calls.append(("poll", pos, deadline_s, ",".join(stages)))
        self._pause()
        if self.poll_delay_s or pos in self.slow:
            import time
            time.sleep(self.poll_delay_s + self.slow.get(pos, 0.0))
        if pos in self.poll_timeout and "cpu-mem-stress" in stages:   # stress still running
            d = self.g.device(pos)
            out = {}
            for n in stages:
                done = n == "mem-pre"
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
        sn = f"FAKE{d.ou:02d}{d.col or 0}"
        dmi = ("Handle 0x0000, DMI type 0, 26 bytes\nBIOS Information\n\tVendor: American Megatrends Inc.\n"
               f"\tVersion: {'F09_3A20' if head else 'F09_3A23'}\n\tRelease Date: "
               f"{'03/11/2020' if head else '07/29/2021'}\n\n"
               f"Handle 0x0001, DMI type 1, 27 bytes\nSystem Information\n\tSerial Number: {sn}\n\n")
        fru = ("FRU Device Description : Builtin FRU Device (ID 0)\n Product Name          : "
               f"Tioga Pass Single Side\n Product Serial        : {sn}\n\n")
        out = {}
        for n in stages:
            macinv = _MACINV if pos not in self.pop_fail else "".join(
                ln + "\n" for ln in _MACINV.splitlines() if "Mellanox" not in ln)
            arts = {"macinv": macinv, "macinv-v": _macinv_v(sn, d.ou * 4 + (d.col or 0),
                                                          pos not in self.pop_fail),
                    "dmidecode": dmi, "ip-d-link": link, "lsblk": _LSBLK,
                    "ip-d-address": addr, "ipmitool-fru": fru, "dimmsum": _DIMMSUM,
                    "tpmstate": _TPMSTATE} if n == "inventory" else {f"{n}": f"{n}: ok\n"}
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

    def read_live(self, host, user, pw, *, runner=None, timeout=25):
        """The live link monitor's one-session read. Mgmt switch: the canned FDB (a
        booted host shows its host MAC too), a port up when it carries a MAC or is an
        uplink. Wedge: a geometry pcieN lane is up (with an LLDP row naming the first
        booted device cabled there) once that device has booted, else down. Each switch's
        eth0 sees its site_port turtle over LLDP. A switch id in live_down does not answer
        (unmeasured); a "<switch id> <iface>" in `pulled` (or a line of pull_file: the
        served fake's cable pull, <order dir>/.fake-pull) is down."""
        from flax_rungs.result import UNMEASURED, RungResult
        from flax_rungs.sonic import IfStatus, LldpEntry, parse_show_mac, wedge_iface
        s = next(s for s in self.g.switches if s.mgmt_ip == host)
        self.calls.append(("read_live", s.id))
        if s.id in getattr(self, "live_down", ()):
            return RungResult(UNMEASURED, {"headline": f"{host} did not answer ssh"})
        booted = {p for p, b in self.boots.items() if b["kernel"] <= self.t}
        ifaces, lldp, fdb = {}, [], []
        if s.role == "mgmt":
            fdb = [e for e in parse_show_mac(canned_fdb(self.g, hosts_up=booted)[0])]
            busy = {e.iface for e in fdb} | {f"Ethernet{p - 1}" for p in s.uplink_ports}
            ifaces = {f"Ethernet{p}": IfStatus("up" if f"Ethernet{p}" in busy else "down",
                                               "up", "1G") for p in range(52)}
        else:
            ifaces = {f"Ethernet{n}": IfStatus("down", "up", "25G") for n in range(128)}
            for d in self.g.devices:
                for name, l in sorted(d.links.items()):
                    if name not in _IFN or l.switch != s.id or d.pos not in booted:
                        continue
                    ifn = wedge_iface(l.port, l.lane)
                    if ifaces[ifn].oper == "up":
                        continue                   # one neighbour per lane
                    ifaces[ifn] = IfStatus("up", "up", "25G")
                    lldp.append(LldpEntry(ifn, f"{self.g.prefix}{d.pos.lower()}",
                                          self._macs(d.pos)[_IFN[name]], _IFN[name]))
        if s.site_port:
            lldp.append(LldpEntry("eth0", f"{s.site_port['switch']}.ehv.lab",
                                  s.site_port["port"], s.site_port["port"]))
        pulled = set(getattr(self, "pulled", ()))
        pf = getattr(self, "pull_file", None)
        if pf is not None and pf.exists():
            pulled |= {tuple(x.split()[:2]) for x in pf.read_text().splitlines() if x.strip()}
        for sid, ifn in pulled:
            if sid == s.id and ifn in ifaces:
                ifaces[ifn] = IfStatus("down", "up", ifaces[ifn].speed)
                lldp = [e for e in lldp if e.local_iface != ifn]
                fdb = [e for e in fdb if e.iface != ifn]
        return RungResult("pass", {"ifaces": ifaces, "lldp": lldp, "fdb": fdb})

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

    def host_run(self, host_ip, user, pw, script, *, timeout=None, runner=None):
        from flax_rungs.liveboot import _PROBE
        self.calls.append(("host_run", self.pos_of.get(host_ip), script.split("\n")[0][:40]))
        pos = self.pos_of.get(host_ip)
        if script == _PROBE:
            out = self._probe_out(pos, host_ip)
            return (0, out, "") if out else (255, "", "ssh: connect to host: No route to host")
        fw = self._fw_host(pos, script)
        if fw is not None:
            if self._probe_out(pos, host_ip) is None or pos in self.fw_host_down:
                return 255, "", "ssh: connect to host: No route to host"
            return 0, fw, ""
        return 0, "", ""

    # ------------------------------------------------------------ Firmware stand-ins
    # the loose-node manifests (fixture-sized); the real readers run against them
    FW_BMC_TARGET = "flax-onetree-1.1.4"
    FW_BIOS_TARGET = "TPC_P26F"
    FW_NIC = ("MT_2420110034", "14.32.1912")

    def fw_manifests(self):
        import re
        from types import SimpleNamespace
        from flax_post.biosd.manifest import BiosMatcher
        from flax_post.fwd.manifest import PostMatcher
        from flax_post.nicd.manifest import NicMatcher
        bmc = {"tiogapass-openbmc": {
            "match": {"product_name": ["(?i)tioga"], "bmc_kind": "openbmc"},
            "target_version": self.FW_BMC_TARGET,
            "check": {"method": "redfish-firmware-inventory", "id": "bmc_active"},
            "flash": {"method": "redfish-update-multipart", "artifact": "x.mtd.tar"}}}
        bios = [{"family": "tiogapass", "target": self.FW_BIOS_TARGET,
                 "afulnx_url": "http://bang/x/afulnx", "bin_url": "http://bang/x/b.zip",
                 "flags": "/p /b /n /x /k /l /me"}]
        psid, ver = self.FW_NIC
        nic = {psid: {"dir": "CX4-Lx", "bin": "fw-ConnectX4Lx", "target_version": ver,
                      "target_psid": psid}}
        fm = {"tiogapass": [re.compile("SV7220G3"), re.compile("Tioga Pass")]}
        return SimpleNamespace(bmc=PostMatcher(bmc), bios=BiosMatcher(bios, family_map=lambda: fm),
                               nic=NicMatcher(nic),
                               sources={"bmc": ("/etc/flax/bmc-firmware-versions.json", True),
                                        "bios": ("/etc/flax/bios-firmware-versions.json", True),
                                        "nic": ("/etc/flax/nic-firmware-versions.json", True)})

    def fw_bmc(self, bmc_ip, creds, matcher):
        from flax_rungs import fwcheck
        self.calls.append(("fw_bmc", self.pos_of.get(bmc_ip)))
        return fwcheck.tp_bmc(bmc_ip, creds, matcher,
                              client_factory=lambda ip, c: _FakeRedfish(self, ip))

    def fw_bios(self, host_ip, user, pw, matcher, *, run=None, runner=None):
        from flax_rungs import fwcheck
        return fwcheck.bios(host_ip, user, pw, matcher, run=run or self.host_run, runner=runner)

    def fw_nic(self, host_ip, user, pw, matcher, *, run=None, runner=None):
        from flax_rungs import fwcheck
        return fwcheck.nic(host_ip, user, pw, matcher, run=run or self.host_run, runner=runner)

    def _fw_host(self, pos, script):
        """Canned dmidecode / mstflint output for fwcheck's host scripts (None: another)."""
        from flax_post.nicd.driver import QUERY_SCRIPT
        if pos is None:
            return None
        if script.startswith("dmidecode -t system"):
            return ("# dmidecode 3.4\nHandle 0x0001, DMI type 1, 27 bytes\nSystem Information\n"
                    "\tManufacturer: Wiwynn\n\tProduct Name: SV7220G3\n\tSerial Number: "
                    f"{self._fru0(pos).split('Product Serial')[1].split(':')[1].strip()}\n")
        if script.startswith("dmidecode -t bios"):
            v = self.fw_bios_ver.get(pos, self.FW_BIOS_TARGET)
            return ("Handle 0x0000, DMI type 0, 26 bytes\nBIOS Information\n\tVendor: American "
                    f"Megatrends Inc.\n\tVersion: {v}\n\tRelease Date: 02/23/2021\n")
        if script == QUERY_SCRIPT:
            psid, ver = self.FW_NIC
            uefi = "False(0)" if pos in self.fw_nic_uefi_off else "True(1)"
            return (f"===CARD 5e:00.0===\nImage type:            FS3\nFW Version:            {ver}\n"
                    f"PSID:                  {psid}\nSecurity Attributes:   N/A\n"
                    f"===UEFI 5e:00.0===\n         EXP_ROM_UEFI_x86_ENABLE             {uefi}\n")
        return None

    def sol_start(self, bmc_ip, creds, path, *, runner=None):
        """A canned console in place of `sol activate`; sol_fail positions get none."""
        from pathlib import Path
        pos = self.pos_of.get(bmc_ip)
        self.calls.append(("sol_start", pos))
        if pos not in self.sol_fail:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(f"[SOL {bmc_ip}] Booting `flax live`\nWelcome to openSUSE Leap\n")
        return {"pos": pos, "path": str(path)}

    def sol_stop(self, cap):
        from pathlib import Path
        from flax_rungs.result import RungResult
        self.calls.append(("sol_stop", cap["pos"]))
        p = Path(cap["path"])
        n = p.stat().st_size if p.exists() else 0
        det = {"bytes": n, "cipher": 17, "user": "u", "drops": 0}
        return RungResult("pass", det) if n else \
            RungResult("fail", {**det, "error": "no sol capture (0 bytes)"})


    # ------------------------------------------------------------ Deploy stand-ins
    def _sol_add(self, path, text):
        if path is not None:
            from pathlib import Path
            p = Path(path)
            if p.exists():                 # the capture is running (sol_start wrote it)
                with open(p, "a") as f:
                    f.write(text)

    def install(self, bmc_ip, host_ip, hostname, creds, *, sol_path=None, deadline_s=None,
                runner=None):
        from flax_rungs.result import RungResult
        pos = self.pos_of.get(host_ip)
        self.calls.append(("install", pos, hostname))
        self._pause()
        con = ("Booting `Install Ubuntu 24.04`\nsubiquity/Meta/status_GET: running\n"
               "curtin command install\n")
        if pos in self.sol_findings:
            con += "[  612.204118] Buffer I/O error on dev sda, logical block 0, async page read\n"
        rows = [["hostname file", f"/srv/pxe/ipxe/hostname-{hostname}.ipxe"],
                ["boot override", "Pxe (redfish)"], ["power action", "cycle"],
                ["tftp (iPXE binary)", "+20 s"], ["boot.ipxe fetched", "+31 s"],
                ["hostname file fetched", "+32 s"], ["installer initrd", "+41 s"],
                ["installer kernel", "+44 s"], ["installer ISO", "+70 s"],
                ["autoinstall user-data", "+128 s"]]
        arts = {f"hostname-{hostname}.ipxe": f"#!ipxe\n\n# {hostname} wiwynn-tp-hmc to do "
                                             "ubuntu-24.04 created 20261030.020000\n"}
        if pos in self.install_fail:
            self._sol_add(sol_path, con + "Please enter the passphrase: \n")
            why = (f"never powered off within {deadline_s} s (installer running; installer hung "
                   "or waiting for input? see the SOL tail)")
            return RungResult("fail", {
                "headline": why, "error": why, "file_removed": True,
                "rows": rows + [["powered off", "no"], ["hostname file removed", "yes"]],
                "notes": [], "sol_tail": "Please enter the passphrase: "}, arts)
        self._sol_add(sol_path, con + "reboot: Power down\n")
        self.power[pos] = "off"
        return RungResult("pass", {
            "headline": "installed: powered off 912 s after power cycle", "off_after_s": 912,
            "file_removed": True, "override": "Pxe (redfish)", "power_action": "cycle",
            "rows": rows + [["powered off", "+912 s"], ["hostname file removed", "yes"]],
            "notes": ["the autoinstall ends `shutdown -h now`: power off after the installer "
                      "fetches = installed"]}, arts)

    def firstboot(self, bmc_ip, host_ip, hostname, creds, *, expect_mac=None, sol_path=None,
                  deadline_s=None, runner=None):
        from flax_rungs.result import RungResult
        pos = self.pos_of.get(host_ip)
        self.calls.append(("firstboot", pos, expect_mac))
        self._pause()
        self.power[pos] = "on"
        self._sol_add(sol_path, f"\nUbuntu 24.04.1 LTS {hostname} ttyS0\n\n{hostname} login: \n")
        user = creds.host[0] if creds.host else "flax"
        if pos in self.firstboot_fail:
            why = (f"ssh as {user}@{host_ip} refused (password; the autoinstall's users differ "
                   "from credentials-host.json?); no other user tried")
            return RungResult("fail", {"headline": why, "error": why, "ssh_user": user,
                                       "rows": [["ssh user", user], ["ssh after", "no answer"]],
                                       "notes": []})
        return RungResult("pass", {
            "headline": f"Ubuntu 24.04 up as {user} after 188 s, / on nvme0n1",
            "ssh_user": user, "ssh_after_s": 188, "version_id": "24.04", "disk": "nvme0n1",
            "rows": [["boot override", "Hdd (redfish)"], ["power action", "on"],
                     ["ssh user", user], ["ssh after", "188 s"], ["VERSION_ID", "24.04"],
                     ["cmdline", "BOOT_IMAGE=/vmlinuz-6.8.0-45-generic root=/dev/mapper/vg0-root"],
                     ["root", "/dev/mapper/vg0-root"], ["root disk", "nvme0n1"],
                     ["hostname", hostname]],
            "notes": ["the installed OS must answer ssh as flax after a fresh boot from disk"]})

    def sol_scan(self, path):
        from flax_rungs.solscan import scan_file
        return scan_file(path)

    def flax_user(self, hosts, user, pw, workdir, *, timeout=None, runner=None):
        from flax_rungs.result import RungResult
        self.calls.append(("flax_user", sorted(hosts)))
        self._pause()
        recap = "PLAY RECAP *********\n" + "".join(
            f"{ip:16s}: ok=9    changed=4    unreachable=0    failed=0    skipped=1\n"
            for ip in hosts.values())
        rows = [["ok", "9"], ["changed", "4"], ["unreachable", "0"], ["failed", "0"]]
        return {p: RungResult("pass", {"host": ip, "headline": "ok=9 changed=4", "rows": rows,
                                       "notes": [f"ansible-playbook flax_user.yml as {user}"]},
                              {"ansible-playbook.txt": recap})
                for p, ip in hosts.items()}

    def bond_config(self, host_ip, user, pw, side, cidr, *, runner=None):
        from flax_rungs.bond import HA_MODE
        from flax_rungs.result import RungResult
        pos = self.pos_of.get(host_ip)
        self.calls.append(("bond_config", pos, cidr, side["A"]["mac"], side["B"]["mac"]))
        self._pause()
        rows = [["primary (wedge A)", f"enp25s0f0np0 {side['A']['mac']}"],
                ["backup (wedge B)", f"enp25s0f1np1 {side['B']['mac']}"],
                ["bond0", f"active-backup, miimon 100 ms, {cidr}"],
                ["mgmt ping across apply", "max gap 0 ms, 0/41 lost"]]
        if pos in self.bond_fail:
            why = "bond0: enp25s0f1np1 (wedge B) MII down"
            return RungResult("fail", {"ha_mode": HA_MODE, "headline": why, "error": why,
                                       "rows": rows}, {"bond0.txt": "MII Status: down\n"})
        return RungResult("pass", {"ha_mode": HA_MODE, "address": cidr,
                                   "macs": {k: side[k]["mac"] for k in ("A", "B")},
                                   "headline": f"bond0 {cidr} active enp25s0f0np0 (wedge A)",
                                   "rows": rows + [["active slave", "enp25s0f0np0 (primary)"]]},
                          {"60-bond0.yaml": f"# rackpost Deploy (ha-config)\n# {cidr}\n",
                           "bond0.txt": "Bonding Mode: fault-tolerance (active-backup)\n"})

    def ha_test(self, nodes, server_pos, *, user, pw, runner=None):
        from flax_rungs.bond import HA_MODE
        from flax_rungs.hatest import NOTES
        from flax_rungs.result import RungResult
        self.calls.append(("ha_test", server_pos, sorted(n["pos"] for n in nodes)))
        self._pause()
        out = {}
        for n in nodes:
            srv = n["pos"] == server_pos
            rows = [["cut A (enp25s0f0np0): active", "enp25s0f0np0 → enp25s0f1np1"],
                    ["cut A: failover gap", "n/a (iperf server)" if srv else "≈200 ms (2/120 lost)"],
                    ["cut A: iperf over bond0", "pass" if srv else "tx 23.1 / rx 23.0 Gbit/s"],
                    ["A restored: active", "enp25s0f0np0"],
                    ["cut B (enp25s0f1np1): active", "enp25s0f0np0 → enp25s0f0np0"],
                    ["cut B: failover gap", "n/a (iperf server)" if srv else "≈0 ms (0/120 lost)"],
                    ["B restored: active", "enp25s0f0np0"]]
            det = {"ha_mode": HA_MODE, "rows": rows, "notes": list(NOTES)}
            if n["pos"] in self.hatest_fail:
                why = "cut A: failover gap 3400 ms (> 1000 ms)"
                out[n["pos"]] = RungResult("fail", {**det, "headline": why, "error": why})
            else:
                out[n["pos"]] = RungResult("pass", {
                    **det, "headline": ("iperf server" if srv else "failover gap ≤200 ms")
                    + ", back on enp25s0f0np0"},
                    {"ping-cut-A.txt": "120 packets transmitted, 118 received\n"} if not srv else {})
        return out

    # ------------------------------------------------------------ Done stand-ins
    def end_summary(self, host_ip, bmc_ip, *, host_user, host_pw, bmc_creds, runner=None):
        """endsummary.collect's shape: canned SDR/SEL judged by the real BENIGN_SEL rule,
        and the in-band captures only when a host IP is given."""
        from flax_rungs.endsummary import nonbenign
        from flax_rungs.result import RungResult
        pos = self.pos_of.get(bmc_ip)
        self.calls.append(("end_summary", pos, host_ip))
        self._pause()
        sdr = ("Inlet Temp       | 01h | ok  |  7.1 | 24 degrees C\n"
               "P0 Temp          | 02h | ok  |  3.0 | 41 degrees C\n"
               "HSC Input Power  | 0Ah | ok  | 10.1 | 312 Watts\n")
        sel = ("   1 | 10/30/2026 | 02:01:12 | Temperature Inlet Temp | Upper Critical going high | Asserted\n"
               "   2 | 10/30/2026 | 02:14:40 | Power Unit #0x3b | Unknown Chassis Pwr Sts | Asserted\n")
        if pos in self.sel_nonbenign:
            sel += ("   3 | 10/30/2026 | 03:02:51 | Memory #0x02 | Correctable ECC | Asserted"
                    " | DIMM_A3\n")
        arts = {"sdr-elist.txt": sdr}
        rows = [["BMC", f"{bmc_ip} as u, cipher 17"], ["sdr elist", "read"]]
        if pos in self.sel_unread:
            rows.append(["sel elist", "not read: timeout"])
        else:
            arts["sel-elist.txt"] = sel
            rows.append(["sel elist", "read"])
        inband = bool(host_ip)
        if inband:
            arts.update({"net.txt": "bond0  UP  02:fb:25:01:00:01\nbond0  UP  10.201.37.51/16\n",
                         "disks.txt": "NAME     MODEL                SERIAL        SIZE TYPE\n"
                                      "nvme0n1  WD_BLACK SN850X 1TB  222815805096  931.5G disk\n",
                         "bootorder.txt": ("BootCurrent: 0003\nBootOrder: "
                                           + ("0001,0003" if pos in self.pxe_first else "0003,0001")
                                           + "\nBoot0001* UEFI: PXE IP4 Mellanox Network Adapter"
                                             "\tPciRoot(0x0)/Pci(0x1,0x0)/MAC(98039b000001,1)/IPv4(0.0.0.0)\n"
                                             "Boot0003* ubuntu\tHD(1,GPT,...)/File(\\EFI\\ubuntu\\shimx64.efi)\n"),
                         "dimms.txt": "Memory Device\n\tSize: 32 GB\n\tLocator: DIMM A0\n"})
            rows.append(["in-band", f"{host_ip}: net.txt, disks.txt, bootorder.txt, dimms.txt"])
        else:
            rows.append(["in-band", "skipped: no installed OS to reach"])
        detail = {"inband": inband, "oob": True, "sel_nonbenign": [], "user": "u", "cipher": 17,
                  "rows": rows}
        if "sel-elist.txt" not in arts:
            head = "SEL not read: sel elist failed: timeout"
            return RungResult("unmeasured", {**detail, "headline": head, "reason": head}, arts)
        bad = nonbenign(sel)
        n = sum("|" in ln for ln in sel.splitlines())
        detail["sel_nonbenign"] = bad
        rows.append(["SEL", f"{n} entries, {len(bad)} non-benign"])
        if bad:
            head = f"SEL: {len(bad)} non-benign: {bad[0]}"
            return RungResult("fail", {**detail, "headline": head, "error": head}, arts)
        return RungResult("pass", {**detail, "headline": f"SEL clean ({n} benign)"
                                   + ("" if inband else "; in-band not read")}, arts)

    def pxe_remove(self, hostname):
        self.calls.append(("pxe_remove", hostname))
        if self.pxe_dir is None:
            return True, None
        from flax_rungs import pxeboot
        return pxeboot.remove(hostname, pxe_dir=self.pxe_dir)

    def clear_override(self, bmc_ip, creds, *, runner=None):
        from flax_rungs.result import RungResult
        pos = self.pos_of.get(bmc_ip)
        self.calls.append(("clear_override", pos))
        if pos in self.override_fail:
            return RungResult("fail", {"error": "http 400", "target": "None"})
        return RungResult("pass", {"method": "redfish", "user": "u", "target": "None"})


class _FakeRedfish:
    """fwd.redfish.RedfishClient's reads for the fake rack: a Tioga Pass at its
    FakeRungs.fw_bmc_ver version (else the target); a `down` BMC answers nothing.
    Only the reads probe_one makes."""

    def __init__(self, rungs, ip):
        self.R, self.ip = rungs, ip
        self.pos = rungs.pos_of.get(ip)

    def get_product_name(self):
        if self.ip in self.R.down:
            return None, "GET /redfish/v1/Systems transport error: timed out"
        return "Wiwynn Tioga Pass Single Side", "ok"

    def get_fw_version(self, fw_id):
        if self.ip in self.R.down:
            return None, f"GET /redfish/v1/UpdateService/FirmwareInventory/{fw_id} transport error"
        return self.R.fw_bmc_ver.get(self.pos, self.R.FW_BMC_TARGET), "ok"

    def get_redfish_root(self):
        return False, None, None
