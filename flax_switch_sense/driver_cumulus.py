"""Cumulus Linux read driver (ssh-driven, JSON show commands).

Mirrors the EosDriver contract: same four public read methods returning the
same shapes so flax_switch_sense.slice + the downstream flax_observe state
machine consume Cumulus and Arista facts identically.

Where EOS uses eAPI (JSON-RPC over HTTPS), Cumulus has no eAPI; we run NCLU /
iproute2 commands over ssh and parse their JSON variants:

  interfaces_status()      net show interface all json
  vlans()                  net show bridge vlan json   (+ /etc/network/interfaces
                           fallback parse, lifted from reaper's
                           parse_cumulus_port_mask, when the bridge-vlan json is
                           empty/unavailable)
  mac_address_table()      bridge -j fdb show
  lldp_neighbors_detail()  net show lldp json          (lldpctl-backed)

The runner callable abstracts ssh exactly like reaper-leased's CumulusSSH and
like EosDriver's injectable session — tests inject a runner returning canned
JSON, so the driver is exercised with zero network.

Cumulus 3.7.3 is old and the JSON shapes drift between releases; every parse
here tolerates missing keys / unexpected types and skips the offending entry
rather than raising. Validated against the live turtle at deploy.
"""
import json
import logging
import os
import re
import subprocess


log = logging.getLogger("flax-switch-sense")

# Reuse the same ControlMaster socket dir + known_hosts handling as reaper's
# CumulusSSH so a co-resident reaper-leased and flax-switch-sense don't fight.
_CM_DIR = "/run/reaper-leased-ssh-cm"
_KNOWN_HOSTS = "/dev/null"


class CumulusError(Exception):
    """ssh failed or a show command produced unparseable output."""


# Read commands. We deliberately AVOID NCLU (`net show ...`): on the old
# turtle-lorax (Cumulus 3.7.3) every `net show` stalls ~5s+ inside the netd
# daemon (≈0.4s CPU, the rest blocked), and under contention that blows the ssh
# timeout — the root cause of the switch-sense poll timeouts. The kernel/lldpd
# commands below return the same facts in milliseconds. iproute2 on 3.7.3
# predates `ip -j`, so interfaces_status reads operstate from sysfs instead.
#
# NOTE on paths: the driver runs `ssh host '<cmd>'` (non-login), whose PATH
# lacks /sbin + /usr/sbin, so `bridge`/`lldpctl` are given absolute paths.
# The `|| true` on the operstate grep runs in the REMOTE shell, so it only
# swallows grep's no-match (→ empty result, i.e. no swp ports); an ssh/connect
# failure still exits non-zero on the client → CumulusError → unreachable,
# exactly as the JSON reads behaved.
_CMD_OPERSTATE = "grep -H . /sys/class/net/swp*/operstate 2>/dev/null || true"
_CMD_BRIDGE_VLAN = "/sbin/bridge -j vlan show"
_CMD_FDB = "sudo bridge -j fdb show"
_CMD_LLDP = "sudo /usr/sbin/lldpctl -f json"

# refresh() runs all four reads in ONE ssh (each ssh handshake to the old turtle
# is ~1.9s, so four separate connections cost ~8s; one batched connection ~2.2s).
# The commands' outputs are concatenated, so we bracket each with a marker line
# and split them back apart. (key, command) — order is the on-wire order.
_BATCH_MARK = "@@FLAX-SENSE@@"
_SECTION_CMDS = (
    ("operstate", _CMD_OPERSTATE),
    ("bridge_vlan", _CMD_BRIDGE_VLAN),
    ("fdb", _CMD_FDB),
    ("lldp", _CMD_LLDP),
)


def parse_cumulus_port_mask(interfaces_text: str) -> dict:
    """Walk /etc/network/interfaces; classify swp* stanzas (lifted from
    reaper_leased.parse_cumulus_port_mask, plus capturing the access vid).

    Returns:
      {"access": {port: vid|None}, "trunk": set(port)}

    Access = stanza carrying 'bridge-access <vid>'.
    Trunk  = stanza carrying 'bridge-vids' or 'bridge-trunk' (and no access).
    """
    access: dict[str, int | None] = {}
    trunk: set[str] = set()
    cur = None
    cur_attrs: list[str] = []

    def flush():
        if cur is None:
            return
        joined = "\n".join(cur_attrs)
        m = re.search(r"bridge-access\s+(\d+)", joined)
        if m:
            access[cur] = int(m.group(1))
        elif "bridge-access" in joined:
            access[cur] = None
        elif "bridge-vids" in joined or "bridge-trunk" in joined:
            trunk.add(cur)

    for raw in interfaces_text.splitlines():
        line = raw.rstrip()
        if line.startswith("auto ") or line.startswith("iface "):
            flush()
            m = re.match(r"^iface (swp\d+)\s*", line)
            if m:
                cur = m.group(1).lower()
                cur_attrs = []
            else:
                cur = None
                cur_attrs = []
            continue
        if cur:
            cur_attrs.append(line)
    flush()
    return {"access": access, "trunk": trunk}


class CumulusDriver:
    """ssh-driven Cumulus client implementing the EosDriver read contract.

    Constructor takes host + user + password + an injectable runner (defaults
    to an sshpass+ssh subprocess) so tests run fully local.
    """

    def __init__(self, host: str, user: str, password: str, *,
                 runner=None, timeout: float = 15.0):
        self.host = host
        self.user = user or "root"
        self.password = password
        self._timeout = timeout
        self.runner = runner or self._default_runner
        # Per-poll batched section cache set by refresh(); None means "not
        # fetched this poll" -> reads fall back to a per-command runner call
        # (the lazy/test path + robustness fallback).
        self._sections: dict[str, str] | None = None

    def _default_runner(self, cmd: str) -> str:
        """Run one command over ssh, returning stdout text. Plain sshpass+ssh:
        reaper's CumulusSSH used `-tt` + a host-side ControlMaster ControlPath
        (`/run/reaper-leased-ssh-cm/`) that does NOT exist in this container,
        which made `net show ... json` fail with exit 3. The commands succeed
        with a plain ssh (verified on the live turtle 3.7.3). Accept any host
        key (UserKnownHostsFile=/dev/null) since switch certs rotate."""
        full = ["sshpass", "-p", self.password, "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "LogLevel=ERROR",
                "-o", f"ConnectTimeout={max(1, int(self._timeout))}",
                f"{self.user}@{self.host}", cmd]
        try:
            return subprocess.check_output(full, timeout=self._timeout, text=True)
        except subprocess.CalledProcessError as e:
            # NEVER echo `full` (it contains `-p <password>`); report only the
            # remote command + exit code.
            raise CumulusError(
                f"{self.host}: {cmd!r} exited {e.returncode}") from None
        except subprocess.TimeoutExpired:
            raise CumulusError(f"{self.host}: {cmd!r} timed out") from None
        except OSError as e:
            raise CumulusError(f"{self.host}: ssh exec failed: {e}") from None

    @staticmethod
    def _decode_json(raw):
        """JSON-decode command stdout. A stray banner / carriage return can
        precede the payload, so we find the first '{' or '[' and decode from
        there. Returns None (not raise) on undecodable output so callers fall
        back to empty containers."""
        if not raw:
            return None
        start = None
        for i, ch in enumerate(raw):
            if ch in "{[":
                start = i
                break
        if start is None:
            return None
        try:
            return json.loads(raw[start:])
        except (ValueError, TypeError):
            return None

    # -- per-poll batched snapshot (one ssh, split back into sections) --

    @staticmethod
    def _build_batch_script() -> str:
        """A shell script that emits each read's output bracketed by a marker
        line. The trailing `echo ...end` makes the script exit 0 whenever ssh
        connected, so one flaky sub-command yields an empty section (its parser
        tolerates that) rather than failing the whole poll; a real connection
        failure still exits non-zero on the client -> unreachable."""
        lines = []
        for key, cmd in _SECTION_CMDS:
            lines.append(f"echo {_BATCH_MARK}{key}")
            lines.append(cmd)
        lines.append(f"echo {_BATCH_MARK}end")
        return "\n".join(lines)

    @staticmethod
    def _split_sections(raw: str) -> dict[str, str]:
        """Split marker-bracketed batch output into {section_key: text}. The
        trailing 'end' sentinel is dropped."""
        sections: dict[str, str] = {}
        cur = None
        buf: list[str] = []
        for line in (raw or "").splitlines():
            if line.startswith(_BATCH_MARK):
                if cur is not None:
                    sections[cur] = "\n".join(buf)
                key = line[len(_BATCH_MARK):].strip()
                cur = None if key == "end" else key
                buf = []
            elif cur is not None:
                buf.append(line)
        if cur is not None:
            sections[cur] = "\n".join(buf)
        return sections

    def refresh(self) -> None:
        """Fetch every per-poll read in ONE ssh session and cache the split
        sections. The read methods then parse their own section out of the
        cache, so a poll is a single connection (~2s) instead of four (~8s)."""
        self._sections = self._split_sections(self.runner(self._build_batch_script()))

    def _section(self, key: str, cmd: str) -> str:
        """This poll's cached section if refresh() ran, else a per-command
        runner call (the lazy path used by unit tests + a robustness fallback)."""
        if self._sections is not None and key in self._sections:
            return self._sections[key]
        return self.runner(cmd)

    def _json_section(self, key: str, cmd: str):
        """JSON-decode a section (batched cache or per-command fallback)."""
        return self._decode_json(self._section(key, cmd))

    # -- public read methods (EosDriver contract) --

    def interfaces_status(self) -> dict[str, str]:
        """Return {port_name: 'link' | 'nolink' | 'unknown'} for every swp port.

        Source: sysfs operstate (`grep -H . /sys/class/net/swp*/operstate`),
        one line per port as ``<path>:<state>`` — e.g.
        ``/sys/class/net/swp1/operstate:up``. The RFC-2863 operstate is the same
        signal NCLU's ``net show interface`` linkstate reports, without the ~5s
        netd stall. We only surface physical swp* ports (bridges/bonds/loopback
        are infra, not DUT slots), matching reaper's swp-only port universe."""
        out: dict[str, str] = {}
        raw = self._section("operstate", _CMD_OPERSTATE)
        for line in (raw or "").splitlines():
            path, sep, value = line.strip().rpartition(":")
            if not sep:
                continue
            parts = path.split("/")
            if len(parts) < 2 or parts[-1] != "operstate":
                continue
            iface = parts[-2]
            if not iface.startswith("swp"):
                continue
            state = value.strip().lower()
            if state == "up":
                out[iface] = "link"
            elif state in ("down", "lowerlayerdown", "admindown"):
                out[iface] = "nolink"
            else:
                out[iface] = "unknown"
        return out

    def vlans(self) -> dict[str, dict]:
        """Return {port_name: {access_vid: int | None, trunk: bool}} for every
        swp port that carries vlan config.

        Primary source: `net show bridge vlan json`, shaped as
        {iface: [{"vlan": N, "flags": ["PVID","Egress Untagged"]|[...]} , ...]}.
        A port carrying exactly one vlan that is its PVID+untagged is an access
        port on that vid; a port carrying multiple vlans (or tagged vlans) is a
        trunk. When the bridge-vlan json is empty/unavailable we fall back to
        parsing /etc/network/interfaces (reaper's parse_cumulus_port_mask)."""
        out: dict[str, dict] = {}
        data = self._json_section("bridge_vlan", _CMD_BRIDGE_VLAN)
        if isinstance(data, dict) and data:
            for iface, entries in data.items():
                if not iface.startswith("swp"):
                    continue
                if not isinstance(entries, list):
                    continue
                vids: list[int] = []
                pvid_untagged: int | None = None
                for ent in entries:
                    if not isinstance(ent, dict):
                        continue
                    try:
                        vid = int(ent.get("vlan"))
                    except (TypeError, ValueError):
                        continue
                    vids.append(vid)
                    flags = ent.get("flags") or []
                    flags_str = " ".join(str(f) for f in flags).lower()
                    if "pvid" in flags_str and "untagged" in flags_str:
                        pvid_untagged = vid
                if not vids:
                    continue
                if len(vids) == 1 and pvid_untagged is not None:
                    out[iface] = {"access_vid": pvid_untagged, "trunk": False}
                else:
                    out[iface] = {"access_vid": None, "trunk": True}
            if out:
                return out

        # Fallback: /etc/network/interfaces stanza parse.
        try:
            interfaces_text = self.runner("cat /etc/network/interfaces")
        except CumulusError:
            return out
        mask = parse_cumulus_port_mask(interfaces_text or "")
        for port, vid in mask["access"].items():
            out[port] = {"access_vid": vid, "trunk": False}
        for port in mask["trunk"]:
            out.setdefault(port, {"access_vid": None, "trunk": True})
        return out

    def mac_address_table(self) -> list[tuple[int, str, str]]:
        """Return [(vlan, mac, port_name)] for every DYNAMIC unicast fdb entry
        on a swp port.

        Source: `sudo bridge -j fdb show`. Each entry is
        {"mac": "..", "dev": "swpN", "vlan": N, "master": "bridge",
         "state": "", "flags": [...]}. We skip:
          - permanent/static entries (flags contain 'permanent' or 'static'),
            the switch's own infra rather than learned DUT presence
            (matches EOS dropping entryType != dynamic);
          - non-swp devs (bridge, bond, vlan subifs);
          - the all-zero / broadcast / multicast macs.
        Entries with no vlan (untagged single-vlan bridges sometimes omit it)
        default to vlan 0 — slice ignores the vlan field, keying on port."""
        data = self._json_section("fdb", _CMD_FDB)
        out: list[tuple[int, str, str]] = []
        if not isinstance(data, list):
            return out
        for ent in data:
            if not isinstance(ent, dict):
                continue
            flags = ent.get("flags") or []
            flags_str = " ".join(str(f) for f in flags).lower()
            state = str(ent.get("state", "")).lower()
            if "permanent" in flags_str or "static" in flags_str or \
                    "permanent" in state or "static" in state:
                continue
            dev = ent.get("dev", "")
            if not isinstance(dev, str) or not dev.startswith("swp"):
                continue
            mac = ent.get("mac")
            if not isinstance(mac, str) or not mac:
                continue
            mac = mac.lower()
            if mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
                continue
            # Skip multicast (low bit of first octet set).
            try:
                first_octet = int(mac.split(":")[0], 16)
                if first_octet & 1:
                    continue
            except (ValueError, IndexError):
                pass
            try:
                vlan = int(ent.get("vlan", 0))
            except (TypeError, ValueError):
                vlan = 0
            out.append((vlan, mac, dev))
        return out

    def lldp_neighbors_detail(self) -> dict[str, list[dict]]:
        """Return {port_name: [{mac, sysname, port_description, mgmt_addrs[]}]}.

        Source: `net show lldp json` (lldpctl-backed). lldpd's JSON nests as
        {"lldp": {"interface": [{"name":"swpN", "chassis": {...},
                                 "port": {...}}, ...]}}. Some lldpd versions
        emit dict-of-dict instead of list, and key the neighbor under the
        local-port name; we tolerate both. chassis.id.value is the neighbor
        chassis-id (mac when subtype mac); chassis.mgmt-ip carries management
        addresses; port.descr is the neighbor's port description."""
        out: dict[str, list[dict]] = {}
        data = self._json_section("lldp", _CMD_LLDP)
        if not isinstance(data, dict):
            return out

        interfaces = self._lldp_interface_list(data)
        for iface_entry in interfaces:
            if not isinstance(iface_entry, dict):
                continue
            port = iface_entry.get("name")
            if not isinstance(port, str) or not port:
                continue
            for chassis in self._lldp_chassis_list(iface_entry):
                mac = self._lldp_chassis_mac(chassis)
                if not mac:
                    continue
                neighbor = {
                    "mac": mac,
                    "sysname": self._lldp_sysname(chassis),
                    "port_description": self._lldp_port_descr(iface_entry),
                    "mgmt_addrs": self._lldp_mgmt_addrs(chassis),
                }
                out.setdefault(port, []).append(neighbor)
        return out

    # -- lldp shape helpers (lldpd JSON is irregular across versions) --

    @staticmethod
    def _as_list(value) -> list:
        """lldpd's JSON gives a bare dict for a single element and a list for
        many; normalise to a list."""
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    @classmethod
    def _lldp_interface_list(cls, data: dict) -> list:
        lldp = data.get("lldp")
        # Form A: {"lldp": {"interface": [...]}}
        if isinstance(lldp, dict):
            ifaces = lldp.get("interface")
            if isinstance(ifaces, dict):
                # Form B: {"lldp":{"interface":{"swp1":{...}}}} — key is name.
                out = []
                for name, body in ifaces.items():
                    if isinstance(body, dict):
                        body = dict(body)
                        body.setdefault("name", name)
                        out.append(body)
                return out
            return cls._as_list(ifaces)
        # Form C: {"lldp": [{"interface": {...}}, ...]}
        if isinstance(lldp, list):
            out = []
            for item in lldp:
                if isinstance(item, dict) and "interface" in item:
                    out.extend(cls._as_list(item.get("interface")))
            return out
        return []

    @classmethod
    def _lldp_chassis_list(cls, iface_entry: dict) -> list:
        chassis = iface_entry.get("chassis")
        if isinstance(chassis, dict):
            # Form: {"chassis": {"<sysname>": {...}}} — the sysname is the key.
            # Heuristic: if no 'id'/'name' key at top level, treat as name-keyed.
            if "id" not in chassis and "name" not in chassis and \
                    "mgmt-ip" not in chassis and "descr" not in chassis:
                out = []
                for name, body in chassis.items():
                    if isinstance(body, dict):
                        body = dict(body)
                        body.setdefault("name", name)
                        out.append(body)
                if out:
                    return out
            return [chassis]
        return cls._as_list(chassis)

    @classmethod
    def _lldp_chassis_mac(cls, chassis: dict) -> str:
        cid = chassis.get("id")
        # lldpctl -f json list-wraps id as [{"type":"mac","value":".."}]; the
        # NCLU net-show form gives a bare dict. Unwrap the single-element list.
        if isinstance(cid, list) and cid:
            cid = cid[0]
        if isinstance(cid, dict):
            subtype = str(cid.get("type", "")).lower()
            value = cid.get("value", "")
            if value and (subtype in ("", "mac") or ":" in str(value)):
                return str(value).lower()
            return str(value).lower() if value else ""
        if isinstance(cid, str) and cid:
            return cid.lower()
        return ""

    @classmethod
    def _lldp_sysname(cls, chassis: dict) -> str:
        name = chassis.get("name")
        if isinstance(name, dict):
            name = name.get("value", "")
        if isinstance(name, list) and name:
            name = name[0]
            if isinstance(name, dict):
                name = name.get("value", "")
        return str(name) if name else ""

    @classmethod
    def _lldp_port_descr(cls, iface_entry: dict) -> str:
        port = iface_entry.get("port")
        if isinstance(port, list) and port:
            port = port[0]
        if not isinstance(port, dict):
            return ""
        descr = port.get("descr")
        if isinstance(descr, list) and descr:
            descr = descr[0]
        if isinstance(descr, dict):
            descr = descr.get("value", "")
        if descr:
            return str(descr)
        # lldpctl -f json often omits descr; fall back to the neighbor's port
        # id (its ifname), which net-show-lldp surfaced as the description.
        pid = port.get("id")
        if isinstance(pid, list) and pid:
            pid = pid[0]
        if isinstance(pid, dict):
            return str(pid.get("value", "") or "")
        return ""

    @classmethod
    def _lldp_mgmt_addrs(cls, chassis: dict) -> list[str]:
        addrs: list[str] = []
        mgmt = chassis.get("mgmt-ip")
        for item in cls._as_list(mgmt):
            if isinstance(item, dict):
                val = item.get("value", "")
            else:
                val = item
            if val:
                addrs.append(str(val))
        return addrs
