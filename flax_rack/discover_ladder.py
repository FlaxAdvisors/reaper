"""Discover for a rack order: loose-node Post's 16-step ladder, per device.

Step names and order are Post's (flax_post.blades DISCOVER_STEPS); so are the
budgets (LADDER_BUDGET_S, BMC_READY_RETRY_BUDGETS_S: copies, parity-tested -- this
daemon cannot import flax_post.blades, it needs psycopg). Every step records
Post's stepinfo evidence shape in its detail: {"headline", "rows": [[label, value]],
"notes": [...]}; raw reads are saved as artifacts/<key>/d<run>/<step>/<name>.txt.

Not lockstepped: every device (and every switch) walks its own ladder in its own
thread, at most MAX_PARALLEL at once. Within one ladder a failed step is a fault:
the steps after it are recorded `skip` ("<step> failed"); `serial` and
`host-mac-seen` never stop the walk (Post reads them beside the ladder).

  TiogaPass host+BMC (compute-tp, bc-head-tp): the mgmt switch's FDB + interface
    status, slot match as before (BMC = host+2) -> switchportlink, bmc-mac-seen;
    bmc-reserved writes the device's Kea reservations (host too: the PXE boot must
    get its address) and checks them back with kea.list; *-leased = the MAC's
    current Kea lease is the planned IP; serial = out-of-band FRU 0 read with
    Post's family-aware field; power-on = Plan 2's path (PXE override, then IPMI on
    or cycle); tftp/ipxe/live-iso-seen = flax_post.observe.bootlog against the
    planned host IP since the power-on (tftp also in the rotated dnsmasq.log-YYYYMMDD,
    for a boot that straddles the 00:00 rotation); host-ssh = a FRESH boot of the
    live image whose BOOTIF MAC is the discovered host MAC; bmc-ready = ping + FRU 0.
  Already live (ssh answers, /proc/cmdline has the live image's BOOTIF = this
    host MAC): NO power action. power-on passes "already in the live image", and the
    boot markers are proven at ssh like Post's _prove_boot: kernel boot time (bang
    clock - uptime) between an iPXE and an ISO fetch in the logs, else `skip`.
    A live image answering with ANOTHER host's BOOTIF fails power-on without acting.
  Up on its installed OS (a deployed node: ssh as flax answers, VERSION_ID 24.04, no
    BOOTIF= on /proc/cmdline, / on an internal disk, the discovered host MAC among its
    NICs): NO power action, NO PXE override. power-on passes "up on its installed OS",
    the boot markers are `skip` (no live boot to mark), host-ssh re-proves the
    installed OS. One without the discovered host MAC fails power-on without acting.
  JBOD (bc-jbod): each IOM endpoint is its own ladder, key "<pos>.iomN" (bmc-*
    steps, serial = fruid-util Board Serial, bmc-ready); host steps `skip`. The
    device's own rows are the per-step roll-up of its IOMs.
  Switches (key "_sw-<id>"): MACs from the site turtle port (geometry site_port,
    turtle host from /etc/flax/switches.json, cisco_user/cisco_pass); more MACs on
    the port than the switch has endpoints fails switchportlink and nothing is ssh'd
    (a wrong site_port must not hand obmc/sonic logins to arbitrary devices); IPs
    from the port MACs' ACTIVE leases only (vid 26 is a dynamic pool: discovered,
    never reserved);
    serial = weutil (wedge BMC, cross-checked with SONiC syseeprom) / syseeprom
    (Dell); bmc-ready = the wedge BMC answering ssh.

`positions` (device positions, <pos>.iomN, switch ids A or _sw-A) limits the run to
those devices/switches; the mgmt switch FDB read stays rack-wide (read only). The
rack's Cancel flag (stages.cancelled) is checked before every step: later steps are
recorded `skip` "cancelled"; a step already running (a power action) finishes.

flax_rungs functions never touch order.db; this module records them. ctx.rungs (a
namespace like real_rungs(); FakeRungs in tests and RACKPOST_FAKE) replaces them.
"""
import datetime
import glob
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from types import SimpleNamespace

from flax_post.observe import bootlog, family_map_live, fru
from flax_rungs import agent, bmc, hostssh, identity, install, ios, liveboot, redfish, sonic
from flax_rungs.result import UNMEASURED, RungResult
from flax_rungs.sonic import FdbEntry

from .addressing import plan_addresses
from .discover import _port_macs, match
from .kea_client import KeaError
from .stages import cancelled, creds, source

STAGE = "discover"
DISCOVER_STEPS = (      # = flax_post.blades.DISCOVER_STEPS (parity-tested)
    "switchportlink", "bmc-mac-seen", "bmc-reserved", "bmc-leased", "bmc-pinged",
    "serial", "power-on", "tftp-seen", "ipxe-seen", "host-mac-seen", "host-reserved",
    "host-leased", "live-iso-seen", "host-pinged", "host-ssh", "bmc-ready",
)
HOST_STEPS = ("power-on", "tftp-seen", "ipxe-seen", "host-mac-seen", "host-reserved",
              "host-leased", "live-iso-seen", "host-pinged", "host-ssh")
# Discover rung names before Plan 2b: still shown from order.db history
OLD_DISCOVER_RUNGS = ("slot-match", "reserve", "bmc-reach")
# = the Discover entries of flax_post.blades.LADDER_BUDGET_S (+ its power-unreadable)
LADDER_BUDGET_S = {
    "power-on": 60, "tftp-seen": 600, "ipxe-seen": 120, "host-leased": 120,
    "live-iso-seen": 300, "host-pinged": 600, "host-ssh": 120, "bmc-ready": 600,
    "power-unreadable": 600,
}
BMC_READY_RETRY_BUDGETS_S = (600, 720, 840)     # = blades.BMC_READY_RETRY_BUDGETS_S
BOOT_PROOF_BOUND_S = 120                         # = observe.ladder.BOOT_PROOF_BOUND_S
# rackpost's own waits where Post's ladder has none (it idles there forever)
BMC_LEASE_WAIT_S = 120
BMC_PING_WAIT_S = 60
LIVE_LOOKBACK_S = 900       # an already-live node: boot markers searched from kernel - this
POLL_S = 5
BMC_READY_POLL_S = 15
MAX_PARALLEL = 8
SITE_SWITCHES = "/etc/flax/switches.json"


def real_rungs():
    return SimpleNamespace(
        read_fdb=sonic.read_fdb, read_ifstatus=sonic.read_interfaces_status,
        mac_table=ios.mac_table, ping=bmc.ping, power_status=bmc.power_status,
        power_action=bmc.power_action, set_boot_pxe=redfish.set_boot_pxe,
        host_run=hostssh.run, tp_fru=identity.tp_fru, iom=identity.iom,
        wedge_bmc=identity.wedge_bmc, read_syseeprom=sonic.read_syseeprom,
        boot_marks=boot_marks, site_switches=site_switches,
        clock=time.monotonic, wall=time.time, sleep=time.sleep)


# ---------------------------------------------------------------- pure helpers
def _colon(h):
    return ":".join(h[i:i + 2] for i in range(0, 12, 2)) if h else None


def _t(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else None


def _dur(a, b):
    return f"+{int(round(b - a))}s" if a is not None and b is not None and b >= a else ""


def ev(verdict, headline, rows=(), notes=(), arts=None, **extra) -> RungResult:
    """A step result in Post's stepinfo shape (rows with an empty value are dropped)."""
    d = {"headline": headline,
         "rows": [[k, str(v)] for k, v in rows if v is not None and v != ""],
         "notes": list(notes), **extra}
    if verdict == "fail":
        d.setdefault("error", headline)
    if verdict in ("skip", UNMEASURED):
        d.setdefault("reason", headline)
    if verdict == "skip":                   # na: the step does not apply to this endpoint
        d["na"] = bool(d.get("na"))
    return RungResult(verdict, d, arts or {})


def site_switches(path=SITE_SWITCHES) -> dict[str, str]:
    """{name: host} from the site switches.json ([{name, driver, host}])."""
    with open(path) as f:
        data = json.load(f)
    return {e["name"]: e["host"] for e in data if isinstance(e, dict)
            and e.get("name") and e.get("host")}


def rotated_dnsmasq(path, since) -> list[str]:
    """The uncompressed daily rotations of `path` (path-YYYYMMDD) that can hold lines
    at or after `since`: the 00:00 copytruncate moves a straddling boot's tftp line
    into the file named for the rotation day (>= since's local date)."""
    d0 = datetime.date.fromtimestamp(since)
    out = []
    for p in sorted(glob.glob(glob.escape(path) + "-" + "[0-9]" * 8)):
        try:
            if datetime.datetime.strptime(p[-8:], "%Y%m%d").date() >= d0:
                out.append(p)
        except ValueError:
            continue
    return out


def boot_marks(host_ip, since, *, dnsmasq=None, nginx=None) -> dict:
    """bootlog.boot_times for host_ip since `since`, the tftp list merged with the
    rotated dnsmasq logs. Adds "sources" (files read) and "rotated_tftp" (times found
    only in a rotated file)."""
    dn = dnsmasq or bootlog.DNSMASQ_LOG
    ng = nginx or bootlog.NGINX_ACCESS_LOG
    t = bootlog.boot_times(dn, ng, host_ip, since)
    rot = rotated_dnsmasq(dn, since)
    extra = set()
    for p in rot:
        extra |= set(bootlog.boot_times(p, os.devnull, host_ip, since)["tftp"])
    t["rotated_tftp"] = sorted(extra - set(t["tftp"]))
    t["tftp"] = sorted(set(t["tftp"]) | extra)
    t["sources"] = {"tftp": [dn, *rot], "ipxe": [ng], "iso": [ng]}
    t["readable"] = {p: os.access(p, os.R_OK) for p in (dn, ng)}
    return t


def prove_boot(born_at, times, kernel_boot):
    """= flax_post.observe.ladder._prove_boot (parity-tested): {tftp, ipxe, iso, kernel}
    when the running kernel came from a PXE boot logged after born_at, else None."""
    if kernel_boot is None:
        return None
    ipxes = [t for t in times.get("ipxe") or [] if t >= born_at]
    if not ipxes:
        return None
    ipxe = max(ipxes)
    bound = max(BOOT_PROOF_BOUND_S, LADDER_BUDGET_S["ipxe-seen"])
    tftps = [t for t in times.get("tftp") or [] if born_at <= t <= ipxe and ipxe - t <= bound]
    isos = [t for t in times.get("iso") or [] if ipxe <= t <= ipxe + BOOT_PROOF_BOUND_S]
    if not tftps or not isos:
        return None
    iso = min(isos)
    if not ipxe <= kernel_boot <= iso:
        return None
    return {"tftp": max(tftps), "ipxe": ipxe, "iso": iso, "kernel": kernel_boot}


def lease_active(lease, now) -> bool:
    """Kea state 0 (default; 1 = declined, 2 = expired-reclaimed) and expire in the
    future. A row without expire (the fakes) counts by its state alone."""
    if not lease or (lease.get("state") or 0) != 0:
        return False
    exp = lease.get("expire")
    if not exp:
        return True
    try:
        return datetime.datetime.fromisoformat(exp).timestamp() > now
    except ValueError:
        return False


# ---------------------------------------------------------------- recording
def _save(ctx, key, rid, step, arts, prefix="d") -> dict:
    keep = {n.removesuffix(".txt"): t for n, t in (arts or {}).items() if isinstance(t, str)}
    if not keep:
        return {}
    return dict(zip(keep, agent.save_artifacts(ctx.dir, key, f"{prefix}{rid}", step, keep)))


def _shaped(r: RungResult) -> RungResult:
    d = dict(r.detail)
    d.setdefault("headline", d.get("error") or d.get("reason") or r.verdict)
    d.setdefault("rows", [])
    d.setdefault("notes", [])
    return RungResult(r.verdict, d, r.artifacts)


def run_step(ctx, key, step, fn, stage=STAGE) -> RungResult:
    """One step: a 'running' row while fn works, then the result with its artifacts
    saved as files (artifacts/<key>/<d|f><run>/<step>/). An exception is a fail; a
    row never stays running."""
    rid = ctx.db.start_run(key, stage, step)
    try:
        r = _shaped(fn())
        r = RungResult(r.verdict, r.detail, _save(ctx, key, rid, step, r.artifacts, stage[0]))
    except Exception as e:                  # noqa: BLE001
        r = ev("fail", f"{type(e).__name__}: {e}")
    ctx.db.finish_run(rid, r)
    return r


class Walk:
    """One endpoint's ladder: every DISCOVER_STEPS step recorded once, in order. After
    a gating fail every later step() is `skip` ("<step> failed"; "<step> not measured"
    after an unmeasured one); skip() records a structural skip (no host on an IOM, ...)
    whatever came before."""

    def __init__(self, ctx, key):
        self.ctx, self.key, self.blocked, self.res = ctx, key, None, {}

    def step(self, name, fn, gate=True) -> RungResult:
        if cancelled(self.ctx):
            return self.skip(name, "cancelled")
        if self.blocked:
            word = "not measured" if self.res[self.blocked].verdict == UNMEASURED else "failed"
            return self.skip(name, f"{self.blocked} {word}")
        r = run_step(self.ctx, self.key, name, fn)
        self.res[name] = r
        if r.verdict in ("fail", UNMEASURED) and gate:
            self.blocked = name
        return r

    def skip(self, name, why, na=False) -> RungResult:
        r = run_step(self.ctx, self.key, name, lambda: ev("skip", why, na=na))
        self.res[name] = r
        return r


# ---------------------------------------------------------------- shared reads
@dataclass
class Env:
    R: object
    cr: object
    sw: object = None                   # the mgmt switch
    fdb_err: str | None = None
    by_port: dict = field(default_factory=dict)
    match: object = None
    ifstatus: dict | None = None
    ifstatus_err: str | None = None
    ips: dict = field(default_factory=dict)
    addrs: dict = field(default_factory=dict)


def _wait(R, budget, poll, probe):
    """probe() until it returns something truthy or `budget` seconds pass; (value, waited)."""
    t0 = R.clock()
    while True:
        v = probe()
        if v:
            return v, R.clock() - t0
        if R.clock() - t0 + poll > budget:
            return None, R.clock() - t0
        R.sleep(poll)


def _portlink(env, link, port) -> RungResult:
    sw = env.sw
    if env.fdb_err:
        return ev("fail", f"mgmt switch FDB not read: {env.fdb_err}")
    iface = f"Ethernet{port - 1}"
    st = (env.ifstatus or {}).get(iface)
    macs = env.by_port.get(port, [])
    rows = [["link", f"{link} -> {sw.id}:{port} ({iface})"],
            ["oper", (st or {}).get("oper") or "unread"], ["admin", (st or {}).get("admin")],
            ["speed", (st or {}).get("speed")],
            ["MACs on wire", ", ".join(_colon(m) for m in macs) or "none"]]
    notes = [f"`show interfaces status` + `show mac` on the mgmt switch {sw.id} ({sw.mgmt_ip})"]
    if st:
        if st.get("oper") == "up":
            return ev("pass", f"{iface} up · {len(macs)} MAC(s)", rows, notes)
        return ev("fail", f"{iface} oper {st.get('oper')}", rows, notes)
    notes.append("interface status not read (" + (env.ifstatus_err or f"no {iface} row")
                 + "): judged by the MACs on the wire")
    if macs:
        return ev("pass", f"{len(macs)} MAC(s) on {iface}", rows, notes)
    return ev("fail", f"no link evidence on {iface}", rows, notes)


def _reserved_row(ctx, mac):
    return next((r for r in ctx.kea.list(source(ctx)) if r.get("mac") == mac), None)


def _check_reserved(ctx, kind, mac, ip, notes=()) -> RungResult:
    row = _reserved_row(ctx, mac)
    rows = [[f"{kind} MAC", _colon(mac)], ["reservation IP", (row or {}).get("ipv4") or "none"],
            ["planned IP", ip], ["hostname", (row or {}).get("hostname")],
            ["source", source(ctx)]]
    if row and row.get("ipv4") == ip:
        return ev("pass", f"{ip} reserved", rows, notes)
    return ev("fail", f"no {source(ctx)} reservation {ip} for {_colon(mac)}"
              if not row else f"reserved {row.get('ipv4')}, planned {ip}", rows, notes)


def _leased(ctx, R, kind, mac, want_ip, budget, dynamic=False) -> RungResult:
    """The MAC's current Kea lease: pass when it is active and (unless dynamic) the
    planned IP. Polled for `budget` seconds."""
    seen = {}

    def probe():
        try:
            lease = ctx.kea.leases([mac]).get(mac)
        except KeaError as e:
            seen["error"] = str(e)
            return None
        seen["lease"] = lease
        if lease and lease_active(lease, R.wall()) and (dynamic or lease.get("ip") == want_ip):
            return lease
        return None

    lease, waited = _wait(R, budget, POLL_S, probe)
    last = lease or seen.get("lease") or {}
    rows = [[f"{kind} MAC", _colon(mac)], [f"{kind} IP", last.get("ip") or "none"],
            ["planned IP", None if dynamic else want_ip], ["lease expires", last.get("expire")],
            ["lease hostname", last.get("hostname")], ["active lease", "yes" if lease else "no"],
            ["budget", f"{budget} s"]]
    notes = ["the MAC's latest Kea lease (flax-control GET /api/v1/leases)"]
    if dynamic:
        notes.append("a switch endpoint holds a dynamic lease on the site mgmt pool: "
                     "discovered by MAC, never reserved")
    if lease:
        return ev("pass", f"leased {lease['ip']}", rows, notes, ip=lease["ip"])
    if seen.get("error"):
        return ev("fail", f"lease read failed: {seen['error']}", rows, notes)
    if not last:
        return ev("fail", f"no lease for {_colon(mac)} after {int(waited)}s", rows, notes)
    why = (f"lease {last.get('ip')} expired" if not lease_active(last, R.wall())
           else f"leased {last.get('ip')}, reserved {want_ip}")
    if not dynamic and last.get("ip") != want_ip:
        notes.append("a device holding a pool lease moves to its reservation at its next "
                     "DHCP renewal")
    return ev("fail", why, rows, notes)


def _pinged(R, ctx, ip, budget) -> tuple[bool, float]:
    ok, waited = _wait(R, budget, POLL_S, lambda: R.ping(ip, runner=ctx.runner))
    return bool(ok), waited


# ---------------------------------------------------------------- TiogaPass ladder
@dataclass
class _Boot:
    live: bool = False
    installed: bool = False             # up on its installed OS (a deployed node)
    power_at: float | None = None
    old_boot_id: str | None = None
    boot_id: str | None = None
    kernel_boot: float | None = None
    proof: dict | None = None
    marks: dict = field(default_factory=dict)
    times: dict = field(default_factory=dict)


def _fru_ev(r: RungResult, bmc_ip, step) -> tuple[RungResult, dict | None]:
    """tp_fru's read through Post's fru.read_baseboard (family-aware serial field)."""
    text = (r.artifacts or {}).get("fru0.txt")
    if not text:
        # serial: no IPMI answer is `unmeasured` (nothing judged); bmc-ready is the
        # reachability measurement itself, so there it stays a fail
        no_answer = step == "serial" and r.verdict == UNMEASURED
        why = r.detail.get("error") or f"FRU 0 not read from {bmc_ip}"
        return ev(UNMEASURED if no_answer else "fail",
                  f"FRU 0 not read: no IPMI answer from {bmc_ip}" if no_answer else why,
                  [["BMC IP", bmc_ip], ["last error", why if no_answer else None]],
                  ["`ipmitool fru print 0` out of band (cipher 17, then 3)"]), None
    bb = fru.read_baseboard(text, family_map_live.current())
    field_ = bb.get("serial_field") or "Product Serial"
    state = {"ok": "ok", "no_serial": "no ship serial",
             "absent": f"not present: {bb.get('reason') or 'no FRU 0'}"}[bb["state"]]
    board = f"{bb.get('board_mfg') or ''} {bb.get('board_product') or bb.get('product_name') or ''}".strip()
    rows = [["BMC IP", bmc_ip], ["FRU 0", state], [f"serial ({field_})", bb.get("serial") or "empty"],
            ["family", bb.get("family") or "unknown"], ["board", board or "not read"],
            ["cipher", r.detail.get("cipher")]]
    notes = ["out of band from the BMC: `ipmitool fru print 0` (FRU ID 0 only), read with "
             "Post's family-aware serial field"]
    extra = {"serial": bb.get("serial"), "serial_field": field_, "family": bb.get("family"),
             "fru_state": bb["state"]}
    if step == "serial":
        if bb["state"] == "ok":
            return ev("pass", f"SN {bb['serial']}", rows, notes, r.artifacts, **extra), bb
        return ev("fail", f"FRU 0 {state}", rows, notes, r.artifacts, **extra), bb
    return ev("pass" if bb["state"] != "absent" else "fail", f"FRU 0 {state}", rows, notes,
              r.artifacts, **extra), bb


def _host_ladder(ctx, env, d):
    R, cr, g = env.R, env.cr, ctx.g
    w = Walk(ctx, d.pos)
    ips = env.ips.get(d.pos, {})
    host_ip, bmc_ip = ips.get("host"), ips.get("bmc")
    dm = env.match.devices.get(d.pos) if env.match else None
    mezz = d.links.get("mezz0")
    port = mezz.port if mezz and env.sw and mezz.switch == env.sw.id else None
    st = _Boot()
    want = (dm.host_mac if dm else None)

    def link():
        if port is None:
            return ev("fail", f"no {env.sw.id if env.sw else 'mgmt'} mezz0 link in geometry")
        return _portlink(env, "mezz0", port)

    def bmc_seen():
        if dm is None or dm.status != "found":
            ctx.db.set_macs(d.pos, host_mac=None, bmc_mac=None, iom_macs=[])
            st_ = dm.status if dm else "missing"
            note = dm.note if dm else ""
            return ev("fail", f"{st_}: {note}".rstrip(": "),
                      [["MACs on wire", ", ".join(_colon(m) for m in env.by_port.get(port, []))
                        or "none"]], ["want host + BMC (host+2) on mezz0"],
                      status=st_, note=note, host_mac=None, bmc_mac=None, iom_macs=[])
        ctx.db.set_macs(d.pos, host_mac=dm.host_mac, bmc_mac=dm.bmc_mac, iom_macs=[])
        macs = env.by_port.get(port, [])
        rows = [["BMC MAC", _colon(dm.bmc_mac)], ["seen on this port", "yes"],
                ["host MAC", _colon(dm.host_mac) + (" (derived: BMC-2)" if dm.note else "")],
                ["MACs on wire", ", ".join(_colon(m) for m in macs)]]
        notes = ["BMC = host+2 is a hard rule on this platform"]
        if dm.note:
            notes.append("only the BMC talks on a powered-off node: the host MAC is BMC-2 "
                         "until host-mac-seen sees it")
        return ev("pass", f"BMC {_colon(dm.bmc_mac)}", rows, notes, status="found",
                  note=dm.note, host_mac=dm.host_mac, bmc_mac=dm.bmc_mac, iom_macs=[])

    def reserve():
        done = []
        for a in env.addrs.get(d.pos, []):
            mac = {"host": dm.host_mac, "bmc": dm.bmc_mac}.get(a.role)
            if not mac:
                return ev("fail", f"{a.role}: no MAC matched", reserved=done)
            try:
                ctx.kea.upsert(source(ctx), mac, a.ip, a.name, g.vid)
            except KeaError as e:
                return ev("fail", f"{a.role}: {e}", [[a.role, f"{a.ip} {_colon(mac)}"]],
                          reserved=done)
            done.append({"role": a.role, "ip": a.ip, "name": a.name, "mac": mac})
        r = _check_reserved(ctx, "BMC", dm.bmc_mac, bmc_ip,
                            ["the device's reservations (BMC and host) are written here, before "
                             "the power-on, so its PXE boot gets the planned host address"])
        return RungResult(r.verdict, {**r.detail, "reserved": done})

    def bmc_pinged():
        ok, waited = _pinged(R, ctx, bmc_ip, BMC_PING_WAIT_S)
        info = {}
        power = R.power_status(bmc_ip, cr.bmc, info=info, runner=ctx.runner) if ok else None
        rows = [["BMC IP", bmc_ip], ["ping", "yes" if ok else "no"],
                ["chassis power", power or "unreadable"], ["cipher", info.get("cipher")]]
        if ok:
            return ev("pass", f"{bmc_ip} answers" + (f" · power {power}" if power else ""),
                      rows, power=power)
        return ev("fail", f"no ping from {bmc_ip} in {int(waited)}s", rows)

    def serial():
        return _fru_ev(R.tp_fru(bmc_ip, cr.bmc, runner=ctx.runner), bmc_ip, "serial")[0]

    w.step("switchportlink", link)
    w.step("bmc-mac-seen", bmc_seen)
    w.step("bmc-reserved", reserve)
    w.step("bmc-leased", lambda: _leased(ctx, R, "BMC", dm.bmc_mac, bmc_ip, BMC_LEASE_WAIT_S))
    w.step("bmc-pinged", bmc_pinged)
    w.step("serial", serial, gate=False)
    w.step("power-on", lambda: _power_on(ctx, env, st, bmc_ip, host_ip, want))
    for step, key in (("tftp-seen", "tftp"), ("ipxe-seen", "ipxe")):
        w.step(step, lambda s=step, k=key: _marker(ctx, env, st, host_ip, s, k))
    w.step("host-mac-seen", lambda: _host_mac_seen(ctx, env, d, port, want, host_ip), gate=False)
    w.step("host-reserved", lambda: _check_reserved(
        ctx, "host", want, host_ip, ["written with the BMC's at bmc-reserved, before the power-on"]))
    w.step("host-leased", lambda: _leased(ctx, R, "host", want, host_ip,
                                          LADDER_BUDGET_S["host-leased"]))
    w.step("live-iso-seen", lambda: _marker(ctx, env, st, host_ip, "live-iso-seen", "iso"))
    w.step("host-pinged", lambda: _host_pinged(ctx, env, st, host_ip))
    w.step("host-ssh", lambda: _host_ssh(ctx, env, st, host_ip, want))
    w.step("bmc-ready", lambda: _bmc_ready(ctx, env, st, bmc_ip))
    return w


_PROBE = liveboot._PROBE


def _probe(ctx, env, host_ip):
    user, pw = env.cr.host
    # no sudo: the probe reads /proc only, and an installed node's flax user may not have
    # NOPASSWD yet (a sudo prompt would read as "not the live image" and cycle it)
    rc, out, err = env.R.host_run(host_ip, user, pw, _PROBE, timeout=20, sudo=False,
                                  runner=ctx.runner)
    return rc, out, err, env.R.wall()


def _power_on(ctx, env, st, bmc_ip, host_ip, want) -> RungResult:
    R, cr = env.R, env.cr
    notes = ["Plan 2's path: Redfish PXE override (IPMI bootdev fallback), then IPMI "
             "`chassis power on` (off) or `cycle` (on, not in the live image)"]
    probe_note = None
    if cr.host:
        rc, out, _err, at = _probe(ctx, env, host_ip)
        if rc == 0:
            bid, up, cmdline = liveboot.parse_probe(out)
            st.old_boot_id = bid
            live = "BOOTIF=" in cmdline and "pxeaddr=" in cmdline
            got = liveboot.bootif_mac(cmdline)
            if live and got != want:
                return ev("fail", f"wrong host at {host_ip}: BOOTIF {_colon(got) or 'unparsed'} != "
                                  f"discovered {_colon(want)}; not acting",
                          [["host IP", host_ip], ["BOOTIF", _colon(got)], ["boot_id", bid]],
                          ["a live image answers at this device's host IP with another host's "
                           "BOOTIF MAC: nothing is powered until that is explained"])
            if live:
                return _already_live(ctx, env, st, bmc_ip, host_ip, bid, up, at, got)
            os_ = _installed_os(ctx, env, host_ip)
            if os_ is not None:
                return _already_installed(ctx, env, st, bmc_ip, host_ip, bid, up, want, os_)
            probe_note = "the host answers ssh but is not in the live image: it is cycled"
    info = {}
    state = R.power_status(bmc_ip, cr.bmc, info=info, runner=ctx.runner)
    rows = [["chassis power before", state or "unreadable"], ["cipher", info.get("cipher")]]
    if state is None:
        return ev("fail", "power state unknown (no IPMI answer); not acting", rows, notes)
    if state == "on" and not cr.host:
        return ev("fail", "power is on and the live image cannot be checked (add "
                          "credentials-host.json); not cycling", rows, notes)
    if probe_note:
        notes.append(probe_note)
    ov = R.set_boot_pxe(bmc_ip, cr.redfish, ipmi_creds=cr.bmc, runner=ctx.runner)
    rows.append(["boot override", ov.detail.get("method") if ov.verdict == "pass"
                 else f"failed: {ov.detail.get('error')}"])
    if ov.verdict != "pass":
        return ev("fail", f"boot override: {ov.detail.get('error')}", rows, notes)
    action = "cycle" if state == "on" else "on"
    t0, st.power_at = R.clock(), R.wall()
    if not R.power_action(bmc_ip, cr.bmc, action, runner=ctx.runner):
        return ev("fail", f"chassis power {action} failed", rows, notes, power_action=action)
    rows += [["power action", action], ["engine power-on at", _t(st.power_at)]]
    last = {}

    def on():
        last["p"] = R.power_status(bmc_ip, cr.bmc, runner=ctx.runner)
        return last["p"] == "on"

    while True:
        if on():
            rows.append(["chassis power", "on"])
            return ev("pass", f"power {action} at {_t(st.power_at)}", rows, notes,
                      power_action=action, power_on_at=st.power_at)
        el = R.clock() - t0
        if last["p"] == "off" and el > LADDER_BUDGET_S["power-on"]:
            return ev("fail", f"not on after {LADDER_BUDGET_S['power-on']}s", rows, notes,
                      power_action=action, power_on_at=st.power_at)
        if last["p"] is None and el > LADDER_BUDGET_S["power-unreadable"]:
            return ev("fail", f"power unreadable {LADDER_BUDGET_S['power-unreadable']}s after "
                              f"power {action}", rows, notes, power_action=action,
                      power_on_at=st.power_at)
        R.sleep(POLL_S)


def _already_live(ctx, env, st, bmc_ip, host_ip, bid, up, at, got) -> RungResult:
    """No power action: the node already runs this device's live image. The boot is
    proven at ssh (Post's _prove_boot) from the kernel boot time and the logs."""
    R = env.R
    st.live, st.boot_id = True, bid
    st.kernel_boot = at - up if up is not None else None
    power = R.power_status(bmc_ip, env.cr.bmc, runner=ctx.runner)
    if st.kernel_boot is not None:
        born = st.kernel_boot - LIVE_LOOKBACK_S
        st.times = R.boot_marks(host_ip, born)
        st.proof = prove_boot(born, st.times, st.kernel_boot)
    rows = [["chassis power", power or "unreadable"], ["host IP", host_ip],
            ["BOOTIF", _colon(got)], ["boot_id", bid], ["uptime", f"{int(up)} s" if up else None],
            ["kernel booted at", _t(st.kernel_boot)],
            ["boot proven at ssh", "yes" if st.proof else "no (markers not in the logs)"]]
    notes = ["the node was already in this device's live image when Discover started "
             "(ssh answers; BOOTIF = the discovered host MAC): no power action",
             "kernel boot time = the bang clock at the ssh read minus /proc/uptime"]
    return ev("pass", "already in the live image: no power action", rows, notes,
              already_live=True, boot_id=bid, kernel_boot=st.kernel_boot, power=power,
              proof=st.proof)


def _installed_os(ctx, env, host_ip) -> dict | None:
    """install's first-boot probe when the host answers it as the installed OS
    (VERSION_ID 24.04, no BOOTIF=, / on an internal disk), else None."""
    user, pw = env.cr.host
    rc, out, _err = env.R.host_run(host_ip, user, pw, install._PROBE, timeout=20,
                                   sudo=False, runner=ctx.runner)  # as install's own probe
    if rc != 0:
        return None
    p = install.parse_probe(out)
    disk, _why = install.root_disk(p)
    if "BOOTIF=" in p["cmdline"] or p["version_id"] != install.VERSION_ID or not disk:
        return None
    return {**p, "disk": disk}


def _os_rows(host_ip, p, want) -> list[list]:
    return [["host IP", host_ip], ["VERSION_ID", p["version_id"]], ["hostname", p["hostname"]],
            ["root disk", p["disk"]], ["boot_id", p["boot_id"]],
            ["host MAC on a NIC", "yes" if want in p["macs"] else f"no ({_colon(want)})"]]


def _already_installed(ctx, env, st, bmc_ip, host_ip, bid, up, want, p) -> RungResult:
    """No power action, no PXE override: the node runs its installed OS (Deploy got it
    there); pulling it back to the live image would undo nothing Discover needs."""
    rows = _os_rows(host_ip, p, want)
    if want not in p["macs"]:
        return ev("fail", f"wrong host at {host_ip}: an installed OS without the discovered "
                          f"host MAC {_colon(want)}; not acting", rows,
                  ["an installed OS answers at this device's host IP but none of its NICs "
                   "carries the discovered host MAC: nothing is powered until that is explained"])
    st.installed, st.boot_id = True, bid
    power = env.R.power_status(bmc_ip, env.cr.bmc, runner=ctx.runner)
    rows = [["chassis power", power or "unreadable"], *rows,
            ["uptime", f"{int(up)} s" if up is not None else None]]
    return ev("pass", "up on its installed OS: no power action", rows,
              ["the node was up on its installed OS (Ubuntu "
               f"{p['version_id']}, / on {p['disk']}) when Discover looked: no power cycle, "
               "no PXE override"],
              installed_os=True, boot_id=bid, power=power, version_id=p["version_id"])


_MARK_SRC = {"tftp": "dnsmasq log (TFTP sent to the host IP)",
             "ipxe": "nginx access log (iPXE fetched its script)",
             "iso": "nginx access log (live ISO fetched)"}


def _marker(ctx, env, st, host_ip, step, key) -> RungResult:
    R = env.R
    budget = LADDER_BUDGET_S[step]
    if st.installed:
        return ev("skip", "up on its installed OS: no live boot to mark",
                  [["evidence", _MARK_SRC[key]]],
                  ["the node was not booted by Discover (it runs its installed OS)"], na=True)
    if st.live:
        ts = (st.proof or {}).get(key)
        rows = [["seen at", _t(ts) or "not seen"], ["kernel booted at", _t(st.kernel_boot)],
                ["evidence", _MARK_SRC[key]]]
        if ts:
            return ev("pass", f"proven at ssh: {_t(ts)}", rows,
                      ["the node was already on when Discover looked; this boot was proven at "
                       f"ssh: the host kernel booted at {_t(st.kernel_boot)}, between the iPXE and "
                       "ISO fetches seen in the logs"], seen_at=ts, proven_at_ssh=True)
        return ev("skip", "already in the live image; this boot's markers are not in the logs",
                  rows, ["the node was already on when Discover looked and its boot could not be "
                         "bracketed by the logs (rotated, or older than the tail read): the "
                         "markers are unknown; BOOTIF at ssh still proves the device"], na=True)
    since = {"tftp": st.power_at, "ipxe": st.marks.get("tftp"),
             "iso": st.marks.get("ipxe")}[key]
    seen = {}

    def probe():
        t = R.boot_marks(host_ip, since)
        seen["t"] = t
        hits = [x for x in t.get(key) or [] if x >= since]
        return min(hits) if hits else None

    ts, waited = _wait(R, budget, POLL_S, probe)
    t = seen.get("t") or {}
    files = (t.get("sources") or {}).get(key) or []
    rows = [["seen at", _t(ts) or "not seen"], ["after power-on", _dur(st.power_at, ts)],
            ["evidence", _MARK_SRC[key]], ["files read", ", ".join(files)],
            ["budget", f"{budget} s"]]
    notes = []
    if key == "tftp":
        notes.append("the dnsmasq log rotates at 00:00 (copytruncate): the rotated "
                     "dnsmasq.log-YYYYMMDD files are searched too")
        if ts and ts in (t.get("rotated_tftp") or []):
            notes.append("found only in a rotated dnsmasq log (the boot straddled the rotation)")
    unread = [p for p, ok in (t.get("readable") or {}).items() if not ok]
    if unread:
        notes.append("not readable by rackpost: " + ", ".join(unread))
    if ts:
        st.marks[key] = ts
        return ev("pass", f"{key} at {_t(ts)}", rows, notes, seen_at=ts)
    return ev("fail", f"no {key} after {budget}s", rows, notes)


def _host_mac_seen(ctx, env, d, port, want, host_ip) -> RungResult:
    R = env.R
    rows = [["host MAC", _colon(want)], ["host IP", host_ip]]
    if want in env.by_port.get(port, []):
        return ev("pass", f"{_colon(want)} seen at Discover start",
                  rows + [["seen on this port", "yes"]])
    R.ping(host_ip, runner=ctx.runner)          # let the switch learn it again
    user, pw = env.cr.sonic
    r = R.read_fdb(env.sw.mgmt_ip, user, pw, runner=ctx.runner)
    if r.verdict != "pass":
        return ev("fail", f"FDB re-read failed: {r.detail.get('error')}", rows)
    macs = _port_macs([FdbEntry(**e) for e in r.detail["fdb"]]).get(port, [])
    rows += [["seen on this port", "yes" if want in macs else "no"],
             ["MACs on wire (re-read)", ", ".join(_colon(m) for m in macs) or "none"]]
    notes = ["not on the port at Discover start (a powered-off node): the FDB is re-read "
             "after the boot"]
    if want in macs:
        return ev("pass", f"{_colon(want)} seen after the boot", rows, notes)
    return ev("fail", f"host MAC {_colon(want)} not on the port after the boot", rows,
              notes + ["the host MAC was derived as BMC-2; the cabling or the derivation is wrong"])


def _host_pinged(ctx, env, st, host_ip) -> RungResult:
    budget = LADDER_BUDGET_S["host-pinged"]
    ok, waited = _pinged(env.R, ctx, host_ip, budget)
    at = env.R.wall()
    rows = [["host IP", host_ip], ["seen at", _t(at) if ok else "not yet"],
            ["after power-on", _dur(st.power_at, at) if ok else ""], ["budget", f"{budget} s"]]
    if ok:
        return ev("pass", f"{host_ip} answers", rows)
    return ev("fail", f"no ping after {budget}s", rows)


def _host_ssh(ctx, env, st, host_ip, want) -> RungResult:
    """A fresh boot of the live image (uptime shorter than the time since the power
    action; boot_id changed when the old OS answered first) whose BOOTIF is the
    discovered host MAC. Already live: the same boot_id must still answer."""
    R, budget = env.R, LADDER_BUDGET_S["host-ssh"]
    notes = ["ssh as the live ISO's host user (flax); /proc/cmdline must carry the live "
             "image's BOOTIF= and pxeaddr=, and BOOTIF must be the discovered host MAC"]
    if not env.cr.host:
        return ev("fail", "add credentials-host.json (flax row)")
    if st.installed:
        return _host_ssh_installed(ctx, env, st, host_ip, want)
    last = {}

    def probe():
        rc, out, err, at = _probe(ctx, env, host_ip)
        if rc != 0:
            last["why"] = f"ssh rc={rc} {err.strip()[-120:]}".strip()
            return None
        bid, up, cmdline = liveboot.parse_probe(out)
        stale = []
        if not st.live:
            if up is None:
                stale.append("uptime unreadable")
            elif up >= at - st.power_at:
                stale.append(f"uptime {int(up)}s >= {int(at - st.power_at)}s since the power action")
            if st.old_boot_id and bid == st.old_boot_id:
                stale.append("boot_id unchanged")
        if stale:
            last["why"] = "old OS still answering: " + "; ".join(stale)
            return None
        return {"boot_id": bid, "uptime": up, "cmdline": cmdline, "at": at}

    got, waited = _wait(R, budget, POLL_S, probe)
    if not got:
        return ev("fail", f"no ssh to a fresh boot after {budget}s",
                  [["host IP", host_ip], ["last", last.get("why")], ["budget", f"{budget} s"]], notes)
    bootif = liveboot.bootif_mac(got["cmdline"])
    kernel = got["at"] - got["uptime"] if got["uptime"] is not None else None
    rows = [["host IP", host_ip], ["seen at", _t(got["at"])],
            ["after power-on", _dur(st.power_at, got["at"])], ["BOOTIF", _colon(bootif)],
            ["boot_id", got["boot_id"]], ["kernel booted at", _t(kernel)],
            ["budget", f"{budget} s"]]
    extra = {"boot_id": got["boot_id"], "kernel_boot": kernel, "bootif": _colon(bootif),
             "already_live": st.live}
    if "BOOTIF=" not in got["cmdline"] or "pxeaddr=" not in got["cmdline"]:
        return ev("fail", "not the live image (no BOOTIF/pxeaddr)", rows, notes, **extra)
    if bootif != want:
        return ev("fail", f"wrong host at {host_ip}: BOOTIF {_colon(bootif) or 'unparsed'} != "
                          f"discovered {_colon(want)}", rows, notes, **extra)
    if st.live and st.boot_id and got["boot_id"] != st.boot_id:
        notes.append("the boot_id changed since power-on read it: the node rebooted meanwhile")
    return ev("pass", ("live image (already running)" if st.live else "fresh live boot")
              + f" · BOOTIF {_colon(bootif)}", rows, notes, **extra)


def _host_ssh_installed(ctx, env, st, host_ip, want) -> RungResult:
    """The installed OS still answers as itself: VERSION_ID, no BOOTIF, internal root,
    the discovered host MAC on one of its NICs."""
    notes = ["ssh as flax to the installed OS (no power action): /etc/os-release VERSION_ID "
             f"{install.VERSION_ID}, no BOOTIF= on /proc/cmdline, / on an internal disk, and "
             "the discovered host MAC on one of its NICs"]
    p = _installed_os(ctx, env, host_ip)
    if p is None:
        return ev("fail", "the installed OS no longer answers as itself",
                  [["host IP", host_ip]], notes)
    rows = _os_rows(host_ip, p, want)
    if want not in p["macs"]:
        return ev("fail", f"wrong host at {host_ip}: host MAC {_colon(want)} on no NIC", rows,
                  notes)
    if st.boot_id and p["boot_id"] != st.boot_id:
        notes.append("the boot_id changed since power-on read it: the node rebooted meanwhile")
    return ev("pass", f"installed OS (Ubuntu {p['version_id']}) · host MAC {_colon(want)}", rows,
              notes, boot_id=p["boot_id"], installed_os=True, already_live=False)


def _bmc_ready(ctx, env, st, bmc_ip) -> RungResult:
    """Ping + FRU 0 answered (ok or no_serial; absent is retried), Post's budgets: the
    first wait, then each retry budget in turn."""
    R = env.R
    budgets = (LADDER_BUDGET_S["bmc-ready"], *BMC_READY_RETRY_BUDGETS_S)
    last = {}

    def probe():
        if not R.ping(bmc_ip, runner=ctx.runner):
            last["why"] = "no ping"
            return None
        r, bb = _fru_ev(R.tp_fru(bmc_ip, env.cr.bmc, runner=ctx.runner), bmc_ip, "bmc-ready")
        last["r"] = r
        return r if r.verdict == "pass" else None

    for n, budget in enumerate(budgets):
        r, waited = _wait(R, budget, BMC_READY_POLL_S, probe)
        if r:
            at = R.wall()
            d = r.detail
            d["rows"] = d["rows"][:1] + [["BMC ping", "yes"], ["data read at", _t(at)],
                                         ["after power-on", _dur(st.power_at, at)]] + d["rows"][1:] \
                + [["retries", f"{n} of {len(BMC_READY_RETRY_BUDGETS_S)}"]]
            d["notes"].append("the BMC must answer ping AND a real data read (FRU 0); the NIC and "
                              "M.2 FRU devices never count")
            return RungResult("pass", d, r.artifacts)
    lr = last.get("r")
    why = (lr.detail["headline"] if lr else "BMC not answering data reads")
    return ev("fail", f"{why} after {len(budgets)} attempts ({sum(budgets)}s)",
              [["BMC IP", bmc_ip], ["last", last.get("why") or why]],
              arts=lr.artifacts if lr else None)


# ---------------------------------------------------------------- JBOD (IOM endpoints)
def _iom_ladder(ctx, env, d, n, ip) -> Walk:
    R, cr = env.R, env.cr
    key = f"{d.pos}.iom{n}"
    w = Walk(ctx, key)
    link = d.links.get(f"iom{n}")
    port = link.port if link and env.sw and link.switch == env.sw.id else None
    macs = env.by_port.get(port, []) if port else []
    mac = macs[0] if len(macs) == 1 else None
    out = {"mac": mac}

    def portlink():
        if port is None:
            return ev("fail", f"no {env.sw.id if env.sw else 'mgmt'} iom{n} link in geometry")
        return _portlink(env, f"iom{n}", port)

    def seen():
        rows = [["IOM MAC", _colon(mac) or "none"],
                ["MACs on wire", ", ".join(_colon(m) for m in macs) or "none"]]
        if mac:
            return ev("pass", f"IOM{n} {_colon(mac)}", rows, status="found", iom_mac=mac)
        st = "missing" if not macs else "ambiguous"
        return ev("fail", f"{st}: want one MAC on the iom{n} port", rows, status=st)

    def reserve():
        a = next(a for a in env.addrs[d.pos] if a.role == f"iom{n}")
        try:
            ctx.kea.upsert(source(ctx), mac, a.ip, a.name, ctx.g.vid)
        except KeaError as e:
            return ev("fail", f"iom{n}: {e}")
        r = _check_reserved(ctx, "IOM", mac, ip)
        return RungResult(r.verdict, {**r.detail, "reserved": [
            {"role": a.role, "ip": a.ip, "name": a.name, "mac": mac}]})

    def pinged():
        ok, waited = _pinged(R, ctx, ip, BMC_PING_WAIT_S)
        rows = [["IOM IP", ip], ["ping", "yes" if ok else "no"]]
        return ev("pass", f"{ip} answers", rows) if ok else \
            ev("fail", f"no ping from {ip} in {int(waited)}s", rows)

    def serial():
        if not cr.obmc:
            return ev("fail", "add obmcuser/obmcpass to the vaulted credentials.json")
        r = R.iom(ip, *cr.obmc, runner=ctx.runner)
        out["serial"] = r
        return r

    def ready():
        if not R.ping(ip, runner=ctx.runner):
            return ev("fail", f"no ping from {ip}", [["IOM IP", ip]])
        s = out.get("serial")
        if s is None or (s.detail.get("error") or "").startswith("fruid-util: rc="):
            return ev("fail", "fruid-util did not answer", [["IOM IP", ip]])
        return ev("pass", f"{ip} answers ping and fruid-util",
                  [["IOM IP", ip], ["ping", "yes"], ["data read", "fruid-util iom (serial step)"]],
                  ["an IOM has no IPMI: its data read is the serial step's `fruid-util iom`"])

    w.step("switchportlink", portlink)
    w.step("bmc-mac-seen", seen)
    w.step("bmc-reserved", reserve)
    w.step("bmc-leased", lambda: _leased(ctx, R, "IOM", mac, ip, BMC_LEASE_WAIT_S))
    w.step("bmc-pinged", pinged)
    w.step("serial", serial, gate=False)
    for s in HOST_STEPS:
        w.skip(s, "an IOM has no host", na=True)
    w.step("bmc-ready", ready)
    w.mac = mac
    return w


def _rollup(ctx, pos, walks: dict[str, Walk], extra_of=None, steps=DISCOVER_STEPS,
            stage=STAGE):
    """The device's own row per step: pass when every endpoint passed (or skipped with
    one passing), fail when one failed, else unmeasured when one was not measured, skip
    when all skipped. `walks` values need only a .res {step: RungResult} (Firmware
    passes its own)."""
    for step in steps:
        res = {k: w.res.get(step) for k, w in walks.items()}
        vs = [r.verdict for r in res.values() if r]
        verdict = ("fail" if "fail" in vs else UNMEASURED if UNMEASURED in vs
                   else "skip" if all(v == "skip" for v in vs) else "pass")
        rows = [[k, f"{r.verdict}: {r.detail.get('headline')}"] for k, r in res.items() if r]
        bad = [k for k, r in res.items() if r and r.verdict == verdict
               and verdict in ("fail", UNMEASURED)]
        heads = {r.detail.get("headline") for r in res.values() if r}
        if bad:
            head = f"{', '.join(bad)}: " + "; ".join(res[k].detail.get("headline") for k in bad)
        elif verdict == "skip" and len(heads) == 1:
            head = heads.pop()
        else:
            head = "; ".join(f"{k} {r.detail.get('headline')}" for k, r in res.items() if r)
        extra = (extra_of or (lambda s: {}))(step)
        if verdict == "skip":               # n/a only when every endpoint's skip is
            extra = {**extra, "na": all(r.detail.get("na") for r in res.values() if r)}
        run_step(ctx, pos, step, lambda: ev(verdict, head, rows,
                                            ["the device's step rolls up its endpoints' own ladders"],
                                            endpoints={k: r.verdict for k, r in res.items() if r},
                                            **extra), stage)


def _jbod(ctx, env, d):
    ips = env.ips.get(d.pos, {})
    walks = {}
    for n in range(1, d.endpoints["ioms"] + 1):
        walks[f"iom{n}"] = _iom_ladder(ctx, env, d, n, ips.get(f"iom{n}"))
    macs = [w.mac for w in walks.values()]
    found = all(macs)
    ctx.db.set_macs(d.pos, host_mac=None, bmc_mac=None, iom_macs=macs if found else [])

    def extra(step):
        if step == "bmc-mac-seen":
            return {"status": "found" if found else "missing", "iom_macs": macs if found else [],
                    "note": "" if found else "no MAC on " + ",".join(
                        k for k, w in walks.items() if not w.mac)}
        if step == "bmc-reserved":
            return {"reserved": [x for w in walks.values() if w.res.get("bmc-reserved")
                                 for x in w.res["bmc-reserved"].detail.get("reserved") or []]}
        return {}

    _rollup(ctx, d.pos, walks, extra)


def _unsupported(ctx, env, d):
    w = Walk(ctx, d.pos)
    dm = env.match.devices.get(d.pos) if env.match else None
    ctx.db.set_macs(d.pos, host_mac=None, bmc_mac=None, iom_macs=[])
    w.step("switchportlink", lambda: ev(
        "fail", (f"mgmt switch FDB not read: {env.fdb_err}" if env.fdb_err
                 else f"{(dm.status if dm else 'unsupported')}: {dm.note if dm else d.kind}"),
        status=dm.status if dm else "unsupported", note=dm.note if dm else ""))
    for s in DISCOVER_STEPS[1:]:
        w.skip(s, "switchportlink failed")


# ---------------------------------------------------------------- switches
def _switch_ladder(ctx, env, s, turtles, prev):
    """Wedge (sonic + bmc) / Dell (sonic): MACs from the turtle port, IPs from leases."""
    R, cr = env.R, env.cr
    key = f"_sw-{s.id}"
    w = Walk(ctx, key)
    ep = s.endpoints
    info = {"macs": [], "role": {}, "lease": {}}

    def link():
        sp = s.site_port
        if not ep:
            return ev("fail", f"no switch endpoints for model {s.model}")
        if not sp:
            return ev("fail", f"switch {s.id} has no site_port in geometry.json")
        host = turtles.get(sp["switch"])
        if not host:
            return ev("fail", f"{sp['switch']} not in {SITE_SWITCHES}")
        if not cr.cisco:
            return ev("fail", "add cisco_user/cisco_pass to the vaulted credentials.json")
        ping_ips = [ip for ip in (s.mgmt_ip, *prev) if ip and not str(ip).startswith("fake-")]
        r = R.mac_table(host, *cr.cisco, [sp["port"]], ping_ips=ping_ips, runner=ctx.runner)
        info["macs"] = (r.detail.get("ports") or {}).get(sp["port"]) or []
        d = dict(r.detail)
        d["rows"] = [["turtle", f"{sp['switch']} ({host})"], ["port", sp["port"]]] + d.get("rows", [])
        d["headline"] = (f"{sp['switch']} {sp['port']}: {len(info['macs'])} MAC(s)"
                         if r.verdict == "pass" else d.get("headline"))
        n_ep = sum(ep.values())
        if r.verdict == "pass" and len(info["macs"]) > n_ep:
            # a wrong site_port (an uplink, a trunk): its MACs are not this switch's
            d["headline"] = d["error"] = (f"unexpected MACs on {sp['port']}: "
                                          + ", ".join(_colon(m) for m in info["macs"]))
            d["notes"] = list(d.get("notes", [])) + [
                f"{s.model} has {n_ep} endpoint(s) on its turtle port; nothing is ssh'd "
                "until site_port names the right port"]
            info["macs"] = []
            return RungResult("fail", d, r.artifacts)
        return RungResult(r.verdict, d, r.artifacts)

    def identify():
        """Which turtle MAC is which endpoint (by lease IP; a wedge's BMC is the one
        whose weutil answers with that MAC as its Local MAC)."""
        macs = info["macs"]
        try:
            got = ctx.kea.leases(macs) if macs else {}
        except KeaError as e:
            got, info["lease_err"] = {}, str(e)
        now = R.wall()
        # only a port MAC's ACTIVE lease names an IP: an expired one's IP may be
        # another machine's by now, and no login ever goes to it
        info["lease"] = {m: l for m, l in got.items() if m in macs and lease_active(l, now)}
        if ep.get("bmc"):
            if not cr.obmc:
                info["role_err"] = "add obmcuser/obmcpass to the vaulted credentials.json"
                return
            for m in macs:
                ip = (info["lease"].get(m) or {}).get("ip")
                if not ip:
                    continue
                r = R.wedge_bmc(ip, *cr.obmc, runner=ctx.runner)
                local = (r.detail.get("local_mac") or "").replace(":", "").lower()
                if local == m:
                    info["role"]["bmc"], info["bmc_r"] = m, r
                    break
            rest = [m for m in macs if m != info["role"].get("bmc")]
            if "bmc" in info["role"] and len(rest) == 1:
                info["role"]["sonic"] = rest[0]
            elif "bmc" not in info["role"]:
                info["role_err"] = "no turtle MAC answered weutil as its own Local MAC"
            else:
                info["role_err"] = f"want one SONiC MAC beside the BMC, saw {len(rest)}"
        elif len(macs) == 1:
            info["role"]["sonic"] = macs[0]
        else:
            info["role_err"] = f"want one MAC on the port, saw {len(macs)}"

    def mac_seen(role):
        m = info["role"].get(role)
        label = "BMC" if role == "bmc" else "SONiC"
        rows = [[f"{label} MAC", _colon(m) or "none"],
                ["MACs on the turtle port", ", ".join(_colon(x) for x in info["macs"]) or "none"]]
        notes = ["the wedge's BMC is the turtle MAC whose `weutil` Local MAC is itself; the "
                 "other is SONiC"] if ep.get("bmc") else []
        if m:
            return ev("pass", f"{label} {_colon(m)}", rows, notes, mac=m)
        return ev("fail", info.get("role_err") or f"no {label} MAC", rows, notes)

    def ip_of(role):
        return ((info["lease"].get(info["role"].get(role)) or {}).get("ip"))

    def pinged(role):
        ip = ip_of(role)
        label = "BMC" if role == "bmc" else "SONiC"
        if not ip:
            return ev("fail", f"no {label} IP (no lease)")
        ok, waited = _pinged(R, ctx, ip, BMC_PING_WAIT_S)
        rows = [[f"{label} IP", ip], ["ping", "yes" if ok else "no"]]
        return ev("pass", f"{ip} answers", rows) if ok else \
            ev("fail", f"no ping from {ip} in {int(waited)}s", rows)

    sonic_r = {}

    def serial():
        sip = ip_of("sonic")
        if sip:
            sonic_r["r"] = R.read_syseeprom(sip, *cr.sonic, runner=ctx.runner)
        sys_ = sonic_r.get("r")
        if ep.get("bmc"):
            bip = ip_of("bmc")
            if not bip:
                return ev("fail", "no BMC IP (no active lease)")
            if not cr.obmc:
                return ev("fail", "add obmcuser/obmcpass to the vaulted credentials.json")
            se = sys_.detail if sys_ is not None and sys_.verdict == "pass" else None
            r = R.wedge_bmc(bip, *cr.obmc, syseeprom=se, runner=ctx.runner)
            if se is None:
                d = dict(r.detail)
                d["notes"] = list(d.get("notes", [])) + [
                    "SONiC syseeprom not read: " + ((sys_.detail.get("error") if sys_ else None)
                                                    or "no SONiC IP") + " (no cross-check)"]
                r = RungResult(r.verdict, d, r.artifacts)
            if sys_ is not None:
                r = RungResult(r.verdict, r.detail, {**r.artifacts, **sys_.artifacts})
            return r
        if sys_ is None:
            return ev("fail", "no SONiC IP (no lease)")
        return sys_

    def sonic_ssh():
        r = sonic_r.get("r")
        if r is None:
            return ev("fail", "no SONiC IP (no lease)")
        ip = ip_of("sonic")
        if not (r.detail.get("error") or "").startswith("rc="):     # ssh answered
            return ev("pass", f"ssh to SONiC {ip} answered",
                      [["SONiC IP", ip], ["read", "show platform syseeprom (serial step)"]])
        return ev("fail", f"ssh to SONiC {ip}: {r.detail.get('error')}", [["SONiC IP", ip]])

    def ready():
        ip = ip_of("bmc")
        if not R.ping(ip, runner=ctx.runner):
            return ev("fail", f"no ping from {ip}", [["BMC IP", ip]])
        r = info.get("bmc_r")
        if r is None or not r.detail.get("serial"):
            return ev("fail", "weutil did not answer", [["BMC IP", ip]])
        return ev("pass", f"{ip} answers ping and weutil",
                  [["BMC IP", ip], ["ping", "yes"], ["data read", "weutil (FRU EEPROM)"]])

    no_res = "switch endpoints are discovered, not reserved (vid 26 is a dynamic pool)"
    no_boot = "a switch has no PXE live boot"
    w.step("switchportlink", link)
    if not w.blocked and not cancelled(ctx):
        identify()
    if ep.get("bmc"):
        w.step("bmc-mac-seen", lambda: mac_seen("bmc"))
        w.skip("bmc-reserved", no_res, na=True)
        w.step("bmc-leased", lambda: _leased(ctx, R, "BMC", info["role"]["bmc"], None,
                                             BMC_LEASE_WAIT_S, dynamic=True))
        w.step("bmc-pinged", lambda: pinged("bmc"))
    else:
        for st_ in ("bmc-mac-seen", "bmc-reserved", "bmc-leased", "bmc-pinged"):
            w.skip(st_, f"no BMC ({s.model} endpoints: {', '.join(ep) or 'none'})", na=True)
    w.step("serial", serial, gate=False)
    for st_ in ("power-on", "tftp-seen", "ipxe-seen"):
        w.skip(st_, no_boot, na=True)
    # the SONiC half walks on after a BMC-half fault (only a turtle fault stops it);
    # bmc-ready then waits on the BMC half again
    bmc_block = w.blocked
    if w.blocked != "switchportlink":
        w.blocked = None
    w.step("host-mac-seen", lambda: mac_seen("sonic"))
    w.skip("host-reserved", no_res, na=True)
    w.step("host-leased", lambda: _leased(ctx, R, "SONiC", info["role"]["sonic"], None,
                                          BMC_LEASE_WAIT_S, dynamic=True))
    w.skip("live-iso-seen", no_boot, na=True)
    w.step("host-pinged", lambda: pinged("sonic"))
    w.step("host-ssh", sonic_ssh)
    w.blocked = bmc_block
    if ep.get("bmc"):
        w.step("bmc-ready", ready)
    else:
        w.skip("bmc-ready", f"no BMC ({s.model})", na=True)
    return w


# ---------------------------------------------------------------- stage
def _read_mgmt(ctx, env):
    g, R = ctx.g, env.R
    env.sw = next((s for s in g.switches if s.role == "mgmt" and s.mgmt_ip), None)
    if env.sw is None:
        env.fdb_err = "no mgmt switch with mgmt_ip in geometry"
        run_step(ctx, "_rack", "switch-fdb", lambda: ev("fail", env.fdb_err))
        return
    user, pw = env.cr.sonic
    r = run_step(ctx, "_rack", "switch-fdb",
                 lambda: _fdb_ev(R.read_fdb(env.sw.mgmt_ip, user, pw, runner=ctx.runner)))
    if r.verdict != "pass":
        env.fdb_err = r.detail.get("error") or "fail"
        return
    fdb = [FdbEntry(**e) for e in r.detail["fdb"]]
    env.by_port = _port_macs(fdb)
    env.match = match(g, fdb, env.sw.id)
    # MACs on ports no slot claims: the other half of a mis-cabled device's 'missing'
    m = env.match
    run_step(ctx, "_rack", "slot-match", lambda: ev(
        "pass", f"{len(m.unexpected)} unexpected MAC(s)",
        [[f"port {p}", _colon(mac)] for p, mac in m.unexpected],
        ["MACs on mgmt-switch ports that no geometry slot (or uplink) claims"],
        unexpected=[{"port": p, "mac": mac} for p, mac in m.unexpected],
        count=len(m.unexpected)))
    try:
        s = R.read_ifstatus(env.sw.mgmt_ip, user, pw, runner=ctx.runner)
        if s.verdict == "pass":
            env.ifstatus = s.detail.get("status") or {}
        else:
            env.ifstatus_err = s.detail.get("error") or "fail"
    except Exception as e:                  # noqa: BLE001 -- only evidence for switchportlink
        env.ifstatus_err = f"{type(e).__name__}: {e}"


def _fdb_ev(r: RungResult) -> RungResult:
    d = dict(r.detail)
    d.setdefault("headline", f"{d.get('count')} FDB entries" if r.verdict == "pass"
                 else d.get("error"))
    return RungResult(r.verdict, d, r.artifacts)


_SEL = re.compile(r"(_sw-)?([A-Za-z0-9][A-Za-z0-9_-]*)(\.(?:iom\d+|bmc))?")


def wanted(g, positions):
    """(devices, switches) the positions name: device positions (a JBOD's <pos>.iomN
    names its device), switch ids (A, _sw-A or _sw-A.bmc); ValueError on a name not in
    the rack. None = all."""
    if positions is None:
        return list(g.devices), list(g.switches)
    poss, sw_ids = {d.pos for d in g.devices}, {s.id for s in g.switches}
    want, bad = set(), []
    for p in positions:
        m = _SEL.fullmatch(p)
        known = m and (m[2] in sw_ids if m[1] else m[2] in poss | sw_ids)
        if known and (not m[3] or (m[3] == ".bmc") == bool(m[1])):
            want.add(m[2])
        else:
            bad.append(p)
    if bad:
        raise ValueError(f"not a device or switch of this rack: {', '.join(sorted(bad))}")
    return ([d for d in g.devices if d.pos in want], [s for s in g.switches if s.id in want])


def run_discover(ctx, *, positions: list[str] | None = None) -> None:
    if ctx.db.closed_at():
        raise RuntimeError("order is closed")
    g = ctx.g
    devs, sws = wanted(g, positions)
    env = Env(ctx.rungs or real_rungs(), creds(ctx))
    for a in plan_addresses(g):
        env.ips.setdefault(a.pos, {})[a.role] = a.ip
        env.addrs.setdefault(a.pos, []).append(a)
    _read_mgmt(ctx, env)
    try:
        turtles = env.R.site_switches()
    except Exception as e:                  # noqa: BLE001 -- each switch says so
        turtles, terr = {}, f"{type(e).__name__}: {e}"
        ctx.db.add_event("discover", f"site switches not read: {terr}")
    # the leases last proved in any pass (a switch's IP is a fact, not a verdict)
    leased = ctx.db.last_passed(("host-leased", "bmc-leased"), all_passes=True)

    def prev_ips(s):
        return [(leased.get((f"_sw-{s.id}", r)) or {}).get("ip")
                for r in ("host-leased", "bmc-leased")]

    if positions is not None:
        ctx.db.add_event("discover", f"discover started: {', '.join(positions)}")
    jobs = []
    for d in devs:
        ep = d.endpoints
        dm = env.match.devices.get(d.pos) if env.match else None
        if ep["ioms"] and not ep["hosts"] and not ep["bmc"]:
            jobs.append((d.pos, lambda d=d: _jbod(ctx, env, d)))
        elif env.fdb_err is None and dm is not None and dm.status == "unsupported":
            jobs.append((d.pos, lambda d=d: _unsupported(ctx, env, d)))
        else:
            jobs.append((d.pos, lambda d=d: _host_ladder(ctx, env, d)))
    for s in sws:
        if s.site_port or s.endpoints:
            jobs.append((f"_sw-{s.id}", lambda s=s: _switch_ladder(ctx, env, s, turtles, prev_ips(s))))

    def one(job):
        key, fn = job
        try:
            fn()
        except Exception as e:              # noqa: BLE001 -- recording itself failed
            ctx.db.add_event("error", f"discover {key}: {type(e).__name__}: {e}")

    with ThreadPoolExecutor(max_workers=max(1, min(MAX_PARALLEL, len(jobs))),
                            thread_name_prefix=f"discover-{g.url_prefix}") as ex:
        list(ex.map(one, jobs))
    lr = ctx.db.latest_runs()
    vs = {d.pos: {(lr.get((d.pos, s)) or {}).get("verdict") for s in DISCOVER_STEPS}
          for d in devs}
    bad = [p for p, v in vs.items() if "fail" in v]
    unm = [p for p, v in vs.items() if UNMEASURED in v and "fail" not in v]
    n_unexp = len(env.match.unexpected) if env.match else 0
    ctx.db.add_event("discover", ("discover cancelled" if cancelled(ctx) else "discover done")
                     + f"; {n_unexp} unexpected MAC(s)"
                     + (f"; faulted: {', '.join(bad)}" if bad else "")
                     + (f"; not measured: {', '.join(unm)}" if unm else "")
                     + ("" if bad or unm else "; every device passed"))
