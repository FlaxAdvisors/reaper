"""SONiC switch reads: FDB (show mac), interface status, LLDP table."""
import re
import subprocess
from dataclasses import asdict, dataclass

from .remote import RemoteError, ssh_run
from .result import RungResult

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
    try:
        text = ssh_run(host, user, password, "show lldp table", runner=runner)
    except RemoteError as e:
        return RungResult("fail", {"error": str(e)})
    entries = parse_show_lldp_table(text)
    return RungResult("pass", {"lldp": [asdict(e) for e in entries]},
                      {"show-lldp-table.txt": text})
