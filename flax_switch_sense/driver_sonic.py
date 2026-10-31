"""SONiC read driver (ssh-driven, text `show` commands + lldpd JSON).

Mirrors the EosDriver contract: the same four public read methods returning the
same shapes, so slice.py + the state machine consume SONiC facts identically to
Arista (eAPI), Cumulus and IOS. Ports are named EthernetN.

  interfaces_status()      sudo -n show interfaces status   (Oper column)
  vlans()                  show vlan brief                  (Port Tagging column)
  mac_address_table()      show mac                         (Type Dynamic only)
  lldp_neighbors_detail()  sudo -n docker exec lldp lldpctl -f json
                           (the lldpd JSON `show lldp neighbors` renders; the
                           `show lldp table` summary has no chassis id)

Parsing of the show-text tables is ported from flax_rungs.sonic (rackpost's
readers, live-verified on the Dell N3248TE and the Wedge100S).

SONiC CLI rules learned live, honoured by every command here:
  - sudo is passwordless on these builds: always `sudo -n` (never prompts) and
    NOTHING on stdin — a password line on stdin once ran as a shell command.
  - plain-user `show interfaces status` dies on the Dell reading the platform
    EEPROM, hence `sudo -n`.
  - redis-cli (behind most `show` commands) eats stdin, so every command gets
    `</dev/null` and the ssh itself is fed an empty stdin.
The password travels in the SSHPASS env var (`sshpass -e`), never argv.
"""
import logging
import os
import re
import subprocess

from .driver_cumulus import CumulusDriver

log = logging.getLogger("flax-switch-sense.driver_sonic")

_CMD_IFSTATUS = "sudo -n show interfaces status </dev/null"
_CMD_VLAN = "show vlan brief </dev/null"
_CMD_MAC = "show mac </dev/null"
_CMD_LLDP = "sudo -n docker exec lldp lldpctl -f json </dev/null"

# refresh() runs every read in ONE ssh, each output bracketed by a marker line
# (the same batching as driver_cumulus). (key, command), on-wire order.
_BATCH_MARK = "@@FLAX-SENSE@@"
_SECTION_CMDS = (
    ("ifstatus", _CMD_IFSTATUS),
    ("vlan", _CMD_VLAN),
    ("mac", _CMD_MAC),
    ("lldp", _CMD_LLDP),
)

_MAC_ROW = re.compile(
    r"^\s*\d+\s+(\d+)\s+((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s+(\S+)\s+(\S+)")


class SonicError(Exception):
    """ssh transport or command failure (password never included)."""


def parse_interfaces_status(text: str) -> dict[str, dict]:
    """`show interfaces status` -> {EthernetN: {oper, admin, speed, vlan}}.
    Columns are located by the header row (Lanes can hold commas, but no cell
    holds spaces)."""
    cols = None
    out = {}
    for line in (text or "").splitlines():
        t = line.split()
        if not t or set(line.strip()) <= {"-", " "}:
            continue
        if t[0] == "Interface":
            cols = {n: i for i, n in enumerate(t)}
            continue
        if cols and t[0].startswith("Ethernet") and "Oper" in cols and \
                len(t) > max(cols.get("Speed", 0), cols["Oper"], cols.get("Admin", 0)):
            out[t[0]] = {"oper": t[cols["Oper"]],
                         "admin": t[cols["Admin"]] if "Admin" in cols else "",
                         "speed": t[cols["Speed"]] if "Speed" in cols else ""}
    return out


def parse_vlan_brief(text: str) -> dict[str, list[tuple[int, bool]]]:
    """`show vlan brief` grid -> {port: [(vid, tagged), ...]}. The VLAN ID cell
    is blank on a VLAN's continuation rows, so it carries down."""
    out: dict[str, list[tuple[int, bool]]] = {}
    vid = None
    for line in (text or "").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        if cells[0].isdigit():
            vid = int(cells[0])
        elif cells[0]:
            continue                      # header row
        port, tagging = cells[2], cells[3].lower()
        if vid is None or not port.startswith("Ethernet"):
            continue
        out.setdefault(port, []).append((vid, tagging == "tagged"))
    return out


def parse_show_mac(text: str) -> list[tuple[int, str, str, str]]:
    """`show mac` -> [(vlan, mac colon-lower, port, type lower)]."""
    out = []
    for line in (text or "").splitlines():
        m = _MAC_ROW.match(line)
        if m:
            out.append((int(m.group(1)), m.group(2).lower(), m.group(3),
                        m.group(4).lower()))
    return out


def _lldpd_interfaces(data) -> list[dict]:
    """SONiC's lldpctl JSON lists interfaces as [{"Ethernet4": {...}}, ...]
    (one name-keyed dict per neighbour); flatten to [{"name": .., ...}] — the
    form driver_cumulus' lldpd helpers read."""
    lldp = data.get("lldp") if isinstance(data, dict) else None
    ifaces = lldp.get("interface") if isinstance(lldp, dict) else None
    if isinstance(ifaces, dict):
        ifaces = [ifaces]
    out = []
    for item in ifaces or []:
        if not isinstance(item, dict):
            continue
        if "name" in item:
            out.append(item)
            continue
        for name, body in item.items():
            if isinstance(body, dict):
                out.append({**body, "name": name})
    return out


class SonicDriver:
    """ssh-driven SONiC client implementing the EosDriver read contract.

    Constructor takes host + user + password + an injectable runner (defaults to
    an `sshpass -e` + ssh subprocess) so tests run fully local.
    """

    def __init__(self, host: str, user: str, password: str, *,
                 runner=None, timeout: float = 30.0):
        self.host = host
        self.user = user or "admin"
        self.password = password
        self._timeout = timeout
        self.runner = runner or self._default_runner
        # Per-poll batched section cache set by refresh(); None means "not
        # fetched this poll" -> reads fall back to a per-command runner call.
        self._sections: dict[str, str] | None = None

    def _default_runner(self, cmd: str) -> str:
        """Run `cmd` over one ssh with an EMPTY stdin; password via SSHPASS."""
        argv = ["sshpass", "-e", "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "LogLevel=ERROR",
                "-o", f"ConnectTimeout={max(1, min(10, int(self._timeout)))}",
                f"{self.user}@{self.host}", cmd]
        env = dict(os.environ, SSHPASS=self.password or "")
        try:
            return subprocess.check_output(argv, input="", env=env,
                                           timeout=self._timeout, text=True)
        except subprocess.CalledProcessError as e:
            raise SonicError(f"{self.host}: ssh exited {e.returncode}") from None
        except subprocess.TimeoutExpired:
            raise SonicError(f"{self.host}: ssh timed out") from None
        except OSError as e:
            raise SonicError(f"{self.host}: ssh exec failed: {e}") from None

    # -- per-poll batched snapshot (one ssh, split back into sections) --

    @staticmethod
    def _build_batch_script() -> str:
        """Each read bracketed by a marker; the trailing echo makes the script
        exit 0 whenever ssh connected, so one failed show is an empty section
        while a connection failure still raises -> unreachable."""
        lines = []
        for key, cmd in _SECTION_CMDS:
            lines.append(f"echo {_BATCH_MARK}{key}")
            lines.append(cmd)
        lines.append(f"echo {_BATCH_MARK}end")
        return "\n".join(lines)

    _split_sections = staticmethod(CumulusDriver._split_sections)

    def refresh(self) -> None:
        """Fetch every per-poll read in ONE ssh session and cache the sections."""
        self._sections = self._split_sections(self.runner(self._build_batch_script()))

    def _section(self, key: str, cmd: str) -> str:
        if self._sections is not None and key in self._sections:
            return self._sections[key]
        return self.runner(cmd)

    # -- public read methods (EosDriver contract) --

    def interfaces_status(self) -> dict[str, str]:
        """Return {port_name: 'link' | 'nolink' | 'unknown'} for every port."""
        out = {}
        for port, st in parse_interfaces_status(
                self._section("ifstatus", _CMD_IFSTATUS)).items():
            oper = st["oper"].lower()
            out[port] = ("link" if oper == "up" else
                         "nolink" if oper == "down" else "unknown")
        return out

    def vlans(self) -> dict[str, dict]:
        """Return {port_name: {access_vid: int | None, trunk: bool}}. A port in
        exactly one VLAN, untagged, is access on it; anything else is a trunk.
        (The Vlan column of `show interfaces status` says `trunk` even for an
        untagged-only member, so `show vlan brief` is the source.)"""
        out = {}
        for port, members in parse_vlan_brief(
                self._section("vlan", _CMD_VLAN)).items():
            if len(members) == 1 and not members[0][1]:
                out[port] = {"access_vid": members[0][0], "trunk": False}
            else:
                out[port] = {"access_vid": None, "trunk": True}
        return out

    def mac_address_table(self) -> list[tuple[int, str, str]]:
        """Return [(vlan, mac, port_name)] for every DYNAMIC entry on an
        EthernetN port (static / PortChannel / CPU rows are infrastructure)."""
        return [(vlan, mac, port)
                for vlan, mac, port, kind in parse_show_mac(
                    self._section("mac", _CMD_MAC))
                if kind == "dynamic" and port.startswith("Ethernet")]

    def lldp_neighbors_detail(self) -> dict[str, list[dict]]:
        """Return {port_name: [{mac, sysname, port_description, mgmt_addrs[]}]}
        from lldpd's JSON (chassis id = neighbour mac, as on the other NOSes)."""
        data = CumulusDriver._decode_json(self._section("lldp", _CMD_LLDP))
        out: dict[str, list[dict]] = {}
        for entry in _lldpd_interfaces(data):
            port = entry.get("name")
            if not isinstance(port, str) or not port:
                continue
            for chassis in CumulusDriver._lldp_chassis_list(entry):
                mac = CumulusDriver._lldp_chassis_mac(chassis)
                if not mac:
                    continue
                out.setdefault(port, []).append({
                    "mac": mac,
                    "sysname": CumulusDriver._lldp_sysname(chassis),
                    "port_description": CumulusDriver._lldp_port_descr(entry),
                    "mgmt_addrs": CumulusDriver._lldp_mgmt_addrs(chassis),
                })
        return out
