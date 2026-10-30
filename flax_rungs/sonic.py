"""SONiC switch reads: FDB (show mac), interface status, LLDP table, syseeprom."""
import re
import subprocess
from dataclasses import asdict, dataclass

from .remote import RemoteError, ssh_cmd, ssh_run, unreachable
from .result import UNMEASURED, RungResult

_MAC_ROW = re.compile(
    r"^\s*\d+\s+(\d+)\s+((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s+(\S+)")


@dataclass
class FdbEntry:
    vlan: int
    mac: str      # lower-case, 12 hex, no separators
    iface: str


@dataclass
class IfStatus:
    oper: str
    admin: str
    speed: str


def parse_show_mac(text: str) -> list[FdbEntry]:
    out = []
    for line in text.splitlines():
        m = _MAC_ROW.match(line)
        if m:
            out.append(FdbEntry(int(m.group(1)),
                                m.group(2).replace(":", "").lower(), m.group(3)))
    return out


def iface_port(iface: str) -> int:
    """SONiC EthernetN -> front-panel port N+1 (N3248TE)."""
    m = re.fullmatch(r"Ethernet(\d+)", iface)
    if not m:
        raise ValueError(f"not an Ethernet interface: {iface!r}")
    return int(m.group(1)) + 1


def parse_interfaces_status(text: str) -> dict[str, IfStatus]:
    cols = None
    out = {}
    for line in text.splitlines():
        t = line.split()
        if not t or set(line.strip()) <= {"-", " "}:
            continue
        if t[0] == "Interface":
            cols = {n: i for i, n in enumerate(t)}
            continue
        if cols and t[0].startswith("Ethernet") and len(t) > max(
                cols["Speed"], cols["Oper"], cols["Admin"]):
            out[t[0]] = IfStatus(t[cols["Oper"]], t[cols["Admin"]], t[cols["Speed"]])
    return out


def read_fdb(host, user, password, *, runner=subprocess.run) -> RungResult:
    try:
        text = ssh_run(host, user, password, "show mac", runner=runner)
    except RemoteError as e:
        return RungResult("fail", {"error": str(e)})
    entries = parse_show_mac(text)
    return RungResult("pass", {"fdb": [asdict(e) for e in entries],
                               "count": len(entries)},
                      {"show-mac.txt": text})


def read_interfaces_status(host, user, password, *, runner=subprocess.run) -> RungResult:
    """`show interfaces status` -> detail {"status": {EthernetN: {oper, admin, speed}}}."""
    try:
        text = ssh_run(host, user, password, "show interfaces status", runner=runner)
    except RemoteError as e:
        return RungResult("fail", {"error": str(e)})
    st = parse_interfaces_status(text)
    return RungResult("pass", {"status": {k: asdict(v) for k, v in st.items()},
                               "count": len(st)}, {"show-interfaces-status.txt": text})


@dataclass
class LldpEntry:
    local_iface: str
    remote_device: str    # the neighbour's system name (may be "localhost")
    remote_port: str      # RemotePortID: the neighbour port's MAC on the live image
    remote_descr: str = ""   # RemotePortDescr: the neighbour's ifname


def wedge_iface(port: int, lane: int | None) -> str:
    """wedge100s front port P (1-based) lane L (1-based) -> EthernetN, 4 lanes per port."""
    return f"Ethernet{(port - 1) * 4 + (lane - 1 if lane else 0)}"


def parse_show_lldp_table(text: str) -> list[LldpEntry]:
    """Columns are cut at the dash ruler under the header (names may hold spaces)."""
    starts = None
    out = []
    for line in text.splitlines():
        if starts is None:
            if line.startswith("LocalPort"):
                starts = []
            continue
        if not starts:
            # first ruler line: one dash run per column
            starts = [m.start() for m in re.finditer(r"-+", line)]
            continue
        if not line.strip() or set(line.strip()) <= {"-"} \
                or line.startswith("Total entries"):
            continue
        cells = [line[a:b].strip() for a, b in zip(starts, starts[1:] + [None])]
        if len(cells) >= 5 and cells[0].startswith("Ethernet"):
            out.append(LldpEntry(cells[0], cells[1], cells[2], cells[4]))
    return out


def read_lldp(host, user, password, *, runner=subprocess.run) -> RungResult:
    """`show lldp table`; a switch that does not answer ssh is `unmeasured`."""
    try:
        text = ssh_run(host, user, password, "show lldp table", runner=runner)
    except RemoteError as e:
        if e.unreachable:
            return RungResult(UNMEASURED, {"error": str(e), "headline":
                                           f"{host} did not answer ssh: {e}"})
        return RungResult("fail", {"error": str(e)})
    entries = parse_show_lldp_table(text)
    return RungResult("pass", {"lldp": [asdict(e) for e in entries]},
                      {"show-lldp-table.txt": text})


# Root-only on community SONiC. sudo is passwordless on these builds: `sudo -n` never
# prompts, and nothing is ever sent on stdin (a password line once ran as a command).
SYSEEPROM = "sudo -n show platform syseeprom"


def parse_syseeprom(text: str) -> dict[str, str]:
    """`show platform syseeprom` TLV table -> {TLV Name: Value}. Columns are cut at the
    dash ruler under the `TLV Name  Code  Len  Value` header; Value runs to line end."""
    starts, out = None, {}
    for line in text.splitlines():
        if starts is None:
            if line.startswith("TLV Name"):
                starts = []
            continue
        if not starts:
            starts = [m.start() for m in re.finditer(r"-+", line)]
            continue
        if not line.strip() or len(starts) < 4:
            break
        name, value = line[:starts[1]].strip(), line[starts[3]:].strip()
        if name and name not in out:
            out[name] = value
    return out


def read_syseeprom(host, user, password, *, runner=subprocess.run) -> RungResult:
    """Identity of a SONiC switch: Serial Number (+ Service Tag, Base MAC). pass when
    the EEPROM has a Serial Number."""
    rc, out, err = ssh_cmd(host, user, password, SYSEEPROM, runner=runner)
    arts = {"syseeprom.txt": out + err}
    if rc != 0:
        e = f"rc={rc} {(err or out).strip()[-300:]}"
        if unreachable(rc):         # nothing judged: the switch never answered ssh
            head = f"{host} did not answer ssh"
            return RungResult(UNMEASURED, {"error": e, "headline": head, "reason": head,
                                           "rows": [], "notes": [e]}, arts)
        return RungResult("fail", {"error": e, "headline": f"syseeprom not read from {host}",
                                   "rows": [], "notes": [e]}, arts)
    f = parse_syseeprom(out)
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
    if "checksum valid" not in out:
        detail["notes"].append("the EEPROM checksum was not reported valid")
    if not serial:
        return RungResult("fail", {**detail, "error": "syseeprom has no Serial Number",
                                   "headline": "switch EEPROM has no Serial Number"}, arts)
    return RungResult("pass", {**detail, "headline": f"SN {serial}"}, arts)


# The live link monitor's one-session read: three read-only show commands in ONE ssh
# (one login per switch per poll), split by echoed marker lines. stdin stays empty.
_LIVE_MARK = "@@flax-live:{}@@"
# `sudo -n`: as a plain user the Dell N3248TE's intfutil dies reading the platform EEPROM
# ("Eeprom is not Programmed"); sudo is passwordless on these SONiC builds.
LIVE_CMD = "; ".join(("sudo -n show interfaces status </dev/null", f"echo '{_LIVE_MARK.format('lldp')}'",
                      "show lldp table", f"echo '{_LIVE_MARK.format('mac')}'", "show mac"))


def _live_sections(text: str) -> dict[str, str]:
    out, cur = {"ifaces": [], "lldp": [], "mac": []}, "ifaces"
    for line in text.splitlines():
        s = line.strip()
        if s == _LIVE_MARK.format("lldp"):
            cur = "lldp"
        elif s == _LIVE_MARK.format("mac"):
            cur = "mac"
        else:
            out[cur].append(line)
    return {k: "\n".join(v) + "\n" for k, v in out.items()}


def read_live(host, user, password, *, runner=subprocess.run, timeout=25) -> RungResult:
    """`show interfaces status; show lldp table; show mac` in one ssh session.
    pass: detail {"ifaces": {iface: IfStatus}, "lldp": [LldpEntry], "fdb": [FdbEntry]};
    unmeasured: the switch did not answer ssh; fail: it answered but no interface
    status came back (a missing lldp/mac section is just empty)."""
    rc, out, err = ssh_cmd(host, user, password, LIVE_CMD, runner=runner, timeout=timeout)
    arts = {"show-live.txt": out + err}
    if rc != 0 and unreachable(rc):
        return RungResult(UNMEASURED, {"error": f"rc={rc} {(err or out).strip()[-300:]}",
                                       "headline": f"{host} did not answer ssh"}, arts)
    sec = _live_sections(out)
    ifaces = parse_interfaces_status(sec["ifaces"])
    if not ifaces:
        e = f"rc={rc} {(err or out).strip()[-300:]}"
        return RungResult("fail", {"error": e, "headline": f"no interface status from {host}"},
                          arts)
    return RungResult("pass", {"ifaces": ifaces, "lldp": parse_show_lldp_table(sec["lldp"]),
                               "fdb": parse_show_mac(sec["mac"])}, arts)
