"""Rackpost's switch reads, through the shared flax_switch_sense read drivers.

Every rack switch (geometry `nos`: sonic | eos | cumulus | ios) and the site turtle
(/etc/flax/switches.json `driver`) is read with the driver flax-switch-sense polls that
NOS with (flax_switch_sense.drivers.make_driver), logged in with the flat
/etc/flax/credentials.json keys (Creds.switch: sonicuser/sonicpass, eosuser/eospass,
cumuser/cumpass, cisco_user/cisco_pass). Read-only: nothing here configures a switch.
The customer rack switches are reached from the order's geometry only (never
switches.json / inventories / files/configs: operator ruling 2026-10-31).

The rung functions keep the RungResult shapes rackpost's stages always used:
  read_live       one batched read: {ifaces {iface: IfStatus}, lldp [LldpEntry] (the
                  mgmt port's neighbour too), fdb [FdbEntry]}       -- LinkPoller
  read_fdb        {fdb [FdbEntry dict], count}                      -- Discover
  read_ifstatus   {status {iface: IfStatus dict}, count}            -- Discover
  read_lldp       {lldp [LldpEntry dict]} front-panel ports only    -- Qualify lldp-<id>
  read_syseeprom  SONiC TlvInfo serial {serial, service_tag, base_mac, rows ...}
  mac_table       the site turtle's MACs on its given ports (ping first, one re-read)
Verdicts: unmeasured = the switch never answered (ssh 255 / our timeout, an eAPI
connect error); fail = it answered but the read failed (a wrong password is a fail);
pass otherwise. Artifacts are the raw command output (ssh drivers) or the parsed read
as JSON (eAPI).

Port names (flax_rungs.portname, re-exported): link_iface(sw, port, lane) is the
NOS's name for a geometry front-panel port, front_panel(sw, iface) the inverse:
  sonic   Dell N3248TE   port p           = Ethernet(p-1)
          wedge100s      port p lane l    = Ethernet((p-1)*4 + l-1)
  eos                    port p [lane l]  = Ethernet<p>[/<l>]  (Et<p>/<l> accepted)
  cumulus                port p [lane l]  = swp<p>[s<l-1>]     (breakout lanes are 0-based)
  ios                    port p           = Gi1/0/<p>
"""
import json
import re
from dataclasses import asdict, dataclass

from flax_rungs.bmc import ping as _ping
# the NOS port names (link_iface / front_panel) and LldpEntry live in flax_rungs so
# its rungs never import flax_rack; re-exported here for rackpost's callers.
from flax_rungs.portname import LldpEntry, front_panel, link_iface, wedge_iface  # noqa: F401
from flax_rungs.remote import UNREACHABLE_RC
from flax_rungs.result import UNMEASURED, RungResult
from flax_switch_sense.drivers import make_driver

# a switch's own management port (its LLDP neighbour is the site switch)
MGMT_IFACES = frozenset(("eth0", "mgmt", "Management1", "Management0", "Ma1"))


@dataclass
class FdbEntry:
    vlan: int
    mac: str      # lower-case, 12 hex, no separators
    iface: str


@dataclass
class IfStatus:
    oper: str
    admin: str | None
    speed: str | None


# ---------------------------------------------------------------- drivers
def driver(host: str, nos: str, cred: dict, *, runner=None):
    """The shared read driver for `nos` at `host` (runner: an ssh driver's runner(cmd),
    for tests; the driver's own sshpass -e ssh otherwise)."""
    d = make_driver({"name": host, "driver": nos, "host": host}, cred or {})
    if runner is not None:
        d.runner = runner
    return d


def _recording(d, arts: dict, name: str):
    """Wrap an ssh driver's runner so each command's raw output lands in arts[name]."""
    inner = getattr(d, "runner", None)
    if inner is None:
        return

    def run(cmd):
        out = inner(cmd)
        arts[name] = arts.get(name, "") + out
        return out
    d.runner = run


def _failure(e: Exception) -> tuple[bool, str]:
    """(unreachable, error) for a driver exception. ssh drivers carry `rc`; an eAPI
    error wraps the requests exception."""
    rc = getattr(e, "rc", None)
    if rc is not None:
        return rc in UNREACHABLE_RC, f"rc={rc} {e}"
    try:
        import requests
        if isinstance(e.__cause__, (requests.ConnectionError, requests.Timeout)):
            return True, str(e)
    except ImportError:
        pass
    return False, f"{type(e).__name__}: {e}"


def _bad(host, e, unmeasured_ok=True) -> RungResult:
    unr, err = _failure(e)
    if unr and unmeasured_ok:
        head = f"{host} did not answer ssh"
        return RungResult(UNMEASURED, {"error": err, "headline": head, "reason": head})
    return RungResult("fail", {"error": err})


def _ifaces(d) -> dict[str, IfStatus]:
    """oper/admin/speed: SONiC's full table; another NOS's link state as oper."""
    if hasattr(d, "interfaces_detail"):
        return {k: IfStatus(v["oper"], v.get("admin") or None, v.get("speed") or None)
                for k, v in d.interfaces_detail().items()}
    word = {"link": "up", "nolink": "down"}
    return {k: IfStatus(word.get(v, "unknown"), None, None)
            for k, v in d.interfaces_status().items()}


def _lldp(d, mgmt: bool) -> list[LldpEntry]:
    out = []
    for iface, nbs in d.lldp_neighbors_detail().items():
        if iface in MGMT_IFACES and not mgmt:
            continue
        for n in nbs:
            out.append(LldpEntry(iface, n.get("sysname") or "", n.get("port_id") or "",
                                 n.get("port_description") or ""))
    return out


def _fdb(d) -> list[FdbEntry]:
    return [FdbEntry(int(vlan), mac.replace(":", "").replace(".", "").lower(), iface)
            for vlan, mac, iface in d.mac_address_table()]


def _eapi_art(arts, name, data):
    if name not in arts:
        arts[name] = json.dumps(data, indent=1, default=lambda o: asdict(o)) + "\n"


# ---------------------------------------------------------------- rungs
def read_live(host, nos, cred, *, runner=None) -> RungResult:
    """Status + LLDP (with the mgmt port) + FDB in one batched read (one ssh on the ssh
    NOSes). pass: {"ifaces", "lldp", "fdb"}; unmeasured: the switch did not answer;
    fail: it answered but no interface status came back."""
    arts = {}
    try:
        d = driver(host, nos, cred, runner=runner)
        _recording(d, arts, "show-live.txt")
        d.refresh()
        ifaces, lldp, fdb = _ifaces(d), _lldp(d, mgmt=True), _fdb(d)
    except Exception as e:                  # noqa: BLE001 -- every driver error is a verdict
        r = _bad(host, e)
        if r.verdict == "fail":
            r.detail["headline"] = f"{host} not read: {r.detail['error']}"
        return RungResult(r.verdict, r.detail, arts)
    _eapi_art(arts, "live.json", {"ifaces": ifaces, "lldp": lldp, "fdb": fdb})
    if not ifaces:
        return RungResult("fail", {"error": "no interface status",
                                   "headline": f"no interface status from {host}"}, arts)
    return RungResult("pass", {"ifaces": ifaces, "lldp": lldp, "fdb": fdb}, arts)


def read_fdb(host, nos, cred, *, runner=None) -> RungResult:
    arts = {}
    try:
        d = driver(host, nos, cred, runner=runner)
        _recording(d, arts, "fdb.txt")
        entries = _fdb(d)
    except Exception as e:                  # noqa: BLE001
        return RungResult("fail", _bad(host, e, unmeasured_ok=False).detail, arts)
    _eapi_art(arts, "fdb.json", entries)
    return RungResult("pass", {"fdb": [asdict(e) for e in entries], "count": len(entries)},
                      arts)


def read_ifstatus(host, nos, cred, *, runner=None) -> RungResult:
    """detail {"status": {iface: {oper, admin, speed}}, "count"}."""
    arts = {}
    try:
        d = driver(host, nos, cred, runner=runner)
        _recording(d, arts, "interfaces-status.txt")
        st = _ifaces(d)
    except Exception as e:                  # noqa: BLE001
        return RungResult("fail", _bad(host, e, unmeasured_ok=False).detail, arts)
    _eapi_art(arts, "interfaces-status.json", st)
    if not st:
        return RungResult("fail", {"error": f"no interface status from {host}"}, arts)
    return RungResult("pass", {"status": {k: asdict(v) for k, v in st.items()},
                               "count": len(st)}, arts)


def read_lldp(host, nos, cred, *, runner=None) -> RungResult:
    """Front-panel LLDP neighbours; a switch that does not answer is `unmeasured`."""
    arts = {}
    try:
        d = driver(host, nos, cred, runner=runner)
        _recording(d, arts, "lldp.txt")
        entries = _lldp(d, mgmt=False)
    except Exception as e:                  # noqa: BLE001
        r = _bad(host, e)
        if r.verdict == UNMEASURED:
            r.detail["headline"] = f"{host} did not answer ssh: {r.detail['error']}"
        return RungResult(r.verdict, r.detail, arts)
    _eapi_art(arts, "lldp.json", entries)
    return RungResult("pass", {"lldp": [asdict(e) for e in entries]}, arts)


def read_syseeprom(host, nos, cred, *, runner=None) -> RungResult:
    """Identity of a SONiC switch: Serial Number (+ Service Tag, Base MAC) from its
    TlvInfo EEPROM (driver_sonic.platform_info). pass when the EEPROM has a serial."""
    arts = {}
    if nos != "sonic":
        e = f"no EEPROM serial read for nos {nos}"
        return RungResult("fail", {"error": e, "headline": e, "rows": [], "notes": []})
    try:
        d = driver(host, nos, cred, runner=runner)
        _recording(d, arts, "syseeprom.txt")
        info = d.platform_info()
    except Exception as ex:                 # noqa: BLE001
        unr, e = _failure(ex)
        if unr:                             # nothing judged: the switch never answered ssh
            head = f"{host} did not answer ssh"
            return RungResult(UNMEASURED, {"error": e, "headline": head, "reason": head,
                                           "rows": [], "notes": [e]}, arts)
        return RungResult("fail", {"error": e, "headline": f"syseeprom not read from {host}",
                                   "rows": [], "notes": [e]}, arts)
    f = info["fields"]
    serial = f.get("Serial Number") or None
    base = f.get("Base MAC Address") or None
    detail = {"serial": serial, "service_tag": f.get("Service Tag"),
              "base_mac": base.lower() if base else None,
              "product_name": f.get("Product Name"), "part_number": f.get("Part Number"),
              "platform": f.get("Platform Name"), "fields": f,
              "rows": [["Serial Number", serial or "empty"],
                       ["Service Tag", f.get("Service Tag") or "-"],
                       ["Product Name", f.get("Product Name") or "-"],
                       ["Base MAC Address", base or "-"]],
              "notes": ["read with `show platform syseeprom` (TlvInfo EEPROM)"]}
    if not info["checksum_valid"]:
        detail["notes"].append("the EEPROM checksum was not reported valid")
    if not serial:
        return RungResult("fail", {**detail, "error": "syseeprom has no Serial Number",
                                   "headline": "switch EEPROM has no Serial Number"}, arts)
    return RungResult("pass", {**detail, "headline": f"SN {serial}"}, arts)


# ---------------------------------------------------------------- the site turtle
_LONG = (("TwentyFiveGigE", "Twe"), ("TenGigabitEthernet", "Te"), ("GigabitEthernet", "Gi"),
         ("FastEthernet", "Fa"), ("HundredGigE", "Hu"), ("FortyGigabitEthernet", "Fo"))
_PORT_ARG = re.compile(r"^(?:Gi|Te|Fa|Tw|Hu|Fo|Twe)\d+(?:/\d+){0,3}$")


def short_port(port: str) -> str:
    """Gi1/0/18 from GigabitEthernet1/0/18 (IOS prints the short form in its tables)."""
    for long, short in _LONG:
        if port.startswith(long):
            return short + port[len(long):]
    return port


def mac_table(host, nos, cred, ports, *, ping_ips=(), runner=None,
              ping=_ping) -> RungResult:
    """The site switch's MACs on `ports` (the rack switches' mgmt ports). The turtle
    ages a MAC out after 300 s, so a quiet device looks absent: the device IPs are
    pinged first, and the table is re-read once (after another ping) before a port is
    reported empty. detail {"ports": {port: [mac12, ...]}, "pinged": {ip: bool},
    "reread": bool} + headline/rows/notes; pass when every port shows a MAC."""
    bad = [p for p in ports if not _PORT_ARG.match(short_port(p))] if nos == "ios" else []
    if not ports or bad:
        return RungResult("fail", {"error": f"bad port name(s): {bad or 'none given'}",
                                   "headline": "no turtle read", "rows": [], "notes": []})
    pinged = {ip: ping(ip) for ip in ping_ips}
    arts, reread = {}, False
    want = {short_port(p): p for p in ports}

    def read(name):
        d = driver(host, nos, cred, runner=runner)
        _recording(d, arts, name)
        got = {p: [] for p in ports}
        for _vlan, mac, port in d.mac_address_table():
            p = want.get(short_port(port))
            m = mac.replace(":", "").replace(".", "").lower()
            if p is not None and m not in got[p]:
                got[p].append(m)
        return got

    try:
        got = read("show-mac.txt")
        if any(not v for v in got.values()):
            reread = True
            pinged.update({ip: ping(ip) or pinged.get(ip, False) for ip in ping_ips})
            got = read("show-mac-reread.txt")
    except Exception as e:                  # noqa: BLE001
        err = _failure(e)[1]
        return RungResult("fail", {"error": err, "pinged": pinged,
                                   "headline": f"turtle {host} not read", "rows": [],
                                   "notes": [err]}, arts)
    empty = [p for p, v in got.items() if not v]
    rows = [[p, ", ".join(v) or "no MAC"] for p, v in got.items()]
    rows += [[f"ping {ip}", "reply" if ok else "no reply"] for ip, ok in pinged.items()]
    notes = ["the turtle ages a MAC out after 300 s: device IPs are pinged before the read"]
    if reread:
        notes.append("a port was empty on the first read; re-read once after another ping")
    detail = {"ports": got, "pinged": pinged, "reread": reread, "rows": rows, "notes": notes}
    if empty:
        detail["error"] = f"no MAC on {', '.join(empty)}"
        detail["headline"] = f"no MAC on {', '.join(empty)} at {host}"
        return RungResult("fail", detail, arts)
    detail["headline"] = f"{sum(map(len, got.values()))} MAC(s) on {len(got)} port(s)"
    return RungResult("pass", detail, arts)
