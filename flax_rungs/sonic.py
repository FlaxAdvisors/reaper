"""SONiC mgmt-switch reads: FDB (show mac) and interface status."""
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
