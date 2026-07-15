"""Cisco IOS classic 15.x read driver (ssh-driven, text `show` commands).

Mirrors the EosDriver contract: the same four public read methods returning the
same shapes, so slice.py + the state machine consume Cisco facts identically to
Arista (eAPI) and Cumulus (NCLU JSON). IOS has no structured output, so every
method text-parses a `show` command.

Parsing is lifted from the legacy scripts/switchportrecond.py IosDriver
(interfaces_status + mac_address_table) and reaper_leased.py CiscoIOSSSH (the ssh
transport workarounds); the `vlans()` derivation from the `show interfaces
status` Vlan column and the `lldp_neighbors_detail()` block parser are new (the
legacy IOS driver never implemented those two contract methods).

The runner callable abstracts ssh exactly like driver_cumulus.CumulusDriver, so
the driver is exercised with zero network. Auth: a priv-15 user lands directly in
privileged exec (no `enable`); every command is prefixed with `terminal length 0`
so long SHOW output never blocks on `--More--`.
"""
import logging
import re
import subprocess

log = logging.getLogger("flax-switch-sense.driver_ios")

# Real Ethernet port tokens on a classic IOS box: Gi/Te/Fa/Tw/Hu + slot path.
_PORT_RE = re.compile(r"^(?:Gi|Te|Fa|Tw|Hu|Fo|Twe)\d")

# `show interfaces status` Status keywords (the Name column is variable-width
# and multi-word, so scan tokens for a known keyword rather than fixed columns).
_LINK_UP = {"connected"}
_LINK_DOWN = {"notconnect", "disabled", "err-disabled", "errdisable",
              "suspended", "monitoring", "inactive"}
_STATUS_WORDS = _LINK_UP | _LINK_DOWN

_SSH_KNOWN_HOSTS = "/dev/null"

# Every `show` a single poll needs, run in ONE ssh session by refresh(). lldp is
# LAST so its `Local Intf:` block parser never engages on the line/mac output
# that precedes it. The parsers self-filter (port-status rows, dynamic-MAC rows,
# lldp blocks) so feeding the combined output to each is contamination-free.
_POLL_SHOWS = ("show interfaces status",
               "show mac address-table",
               "show lldp neighbors detail")


class IosError(Exception):
    """ssh transport or command failure (password never included)."""


def _dotted_to_colon(dotted: str) -> str:
    """98:03:9b:a6:fc:24 from 9803.9ba6.fc24 (Cisco dotted-triplet)."""
    digits = "".join(c for c in dotted.lower() if c in "0123456789abcdef")
    if len(digits) != 12:
        return ""
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


class IosDriver:
    """ssh-driven Cisco IOS client implementing the EosDriver read contract.

    Constructor takes host + user + password + an injectable runner (defaults to
    an sshpass+ssh subprocess with the classic-IOS kex/hostkey workarounds) so
    tests run fully local.
    """

    def __init__(self, host: str, user: str, password: str, *,
                 runner=None, timeout: float = 30.0):
        self.host = host
        self.user = user or "admin"
        self.password = password
        self._timeout = timeout
        self.runner = runner or self._default_runner
        # Combined output of the current poll's batched fetch (set by refresh()).
        # None means "not yet fetched this poll" -> read methods fall back to a
        # per-command runner call (the lazy/test path).
        self._blob: str | None = None

    def _build_ssh_argv(self) -> list:
        """Build the one-shot sshpass+ssh argv (classic-IOS legacy kex/hostkey +
        pty workarounds lifted from reaper_leased CiscoIOSSSH). NO ControlMaster:
        a backgrounded master ssh reparents to PID-1 python and leaks as a zombie
        (see feedback_ios_ssh_controlmaster_session_is_a_trap). Every call is a
        plain foreground ssh that subprocess fully reaps. NEVER echo the argv
        (it carries `-p <password>`)."""
        return ["sshpass", "-p", self.password, "ssh", "-tt",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=" + _SSH_KNOWN_HOSTS,
                "-o", "KexAlgorithms=+diffie-hellman-group14-sha1",
                "-o", "HostKeyAlgorithms=+ssh-rsa",
                "-o", "LogLevel=ERROR",
                "-o", "ConnectTimeout=" + str(max(1, int(self._timeout))),
                self.user + "@" + self.host]

    def _default_runner(self, cmd: str) -> str:
        """Run `cmd` (one or more newline-separated `show` commands) in a single
        ssh session, returning the combined stdout. A `terminal length 0` prefix
        disables pagination so long SHOW output never blocks on `--More--`.
        """
        full = self._build_ssh_argv()
        payload = "terminal length 0\n" + cmd + "\n"
        try:
            return subprocess.check_output(full, input=payload,
                                           timeout=self._timeout, text=True)
        except subprocess.CalledProcessError as e:
            raise IosError(f"{self.host}: {cmd!r} exited {e.returncode}") from None
        except subprocess.TimeoutExpired:
            raise IosError(f"{self.host}: {cmd!r} timed out") from None
        except OSError as e:
            raise IosError(f"{self.host}: ssh exec failed: {e}") from None

    # -- per-poll snapshot (one connection, leak-proof) --

    def refresh(self) -> None:
        """Fetch every per-poll `show` in ONE owned ssh session and cache the
        combined blob. The read methods then parse their own lines out of it, so
        a poll is a single connection with no backgrounded ssh to orphan."""
        self._blob = self.runner("\n".join(_POLL_SHOWS))

    def _text(self, cmd: str) -> str:
        """This poll's combined blob if refresh() ran, else a per-command fetch
        (the lazy path used by unit tests + as a robustness fallback)."""
        return self._blob if self._blob is not None else self.runner(cmd)

    # -- public read methods (EosDriver contract) --

    def _ifstatus_rows(self):
        """Yield (port, status_kw, vlan_token) per real port line of
        `show interfaces status`. Shared by interfaces_status + vlans so the
        Vlan column is parsed once-consistently. Status + Vlan are adjacent
        single tokens, so vlan = the token right after the status keyword."""
        out = self._text("show interfaces status")
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            port = parts[0]
            if not _PORT_RE.match(port):
                continue
            status_idx = next((i for i, t in enumerate(parts[1:], start=1)
                               if t in _STATUS_WORDS), None)
            if status_idx is None:
                continue
            status_kw = parts[status_idx]
            vlan_token = parts[status_idx + 1] if status_idx + 1 < len(parts) else ""
            yield port, status_kw, vlan_token

    def interfaces_status(self) -> dict:
        """Return {port: 'link' | 'nolink' | 'unknown'} for every port."""
        out = {}
        for port, status_kw, _vlan in self._ifstatus_rows():
            if status_kw in _LINK_UP:
                out[port] = "link"
            elif status_kw in _LINK_DOWN:
                out[port] = "nolink"
            else:
                out[port] = "unknown"
        return out

    def vlans(self) -> dict:
        """Return {port: {access_vid: int | None, trunk: bool}} from the Vlan
        column of `show interfaces status`. An integer Vlan is an access vid;
        'trunk' sets trunk=True; 'routed'/'unassigned'/non-numeric leave both
        defaults (access_vid=None, trunk=False)."""
        out = {}
        for port, _status, vlan_token in self._ifstatus_rows():
            slot = {"access_vid": None, "trunk": False}
            if vlan_token == "trunk":
                slot["trunk"] = True
            elif vlan_token.isdigit():
                slot["access_vid"] = int(vlan_token)
            out[port] = slot
        return out

    def mac_address_table(self) -> list:
        """Return [(vlan, mac, port)] for every DYNAMIC unicast entry. STATIC
        (CPU, VRRP) + non-dynamic rows are skipped — infrastructure, not DUT
        presence. MAC normalized dotted-triplet -> colon-lower."""
        out = []
        text = self._text("show mac address-table")
        for line in text.splitlines():
            parts = line.split()
            # vlan  dotted-mac  type  port  (>= 4 tokens; mac has dots)
            if len(parts) < 4 or "." not in parts[1]:
                continue
            if parts[2].upper() != "DYNAMIC":
                continue
            if not parts[0].isdigit():
                continue
            mac = _dotted_to_colon(parts[1])
            if not mac:
                continue
            out.append((int(parts[0]), mac, parts[3]))
        return out

    def lldp_neighbors_detail(self) -> dict:
        """Return {port: [{mac, sysname, port_description, mgmt_addrs[]}]}.

        Parses `show lldp neighbors detail`, which IOS renders as blocks
        delimited by a `Local Intf:` line. Per block: Chassis id (mac, if
        mac-shaped), System Name, Port Description, and one or more
        `IP:` lines under `Management Addresses:`. Ports with no mac-typed
        chassis id are skipped (can't correlate against the MAC table)."""
        text = self._text("show lldp neighbors detail")
        out: dict = {}
        cur_port = None
        cur = None
        in_mgmt = False

        def flush():
            if cur_port and cur and cur.get("mac"):
                out.setdefault(cur_port, []).append({
                    "mac": cur["mac"],
                    "sysname": cur.get("sysname", ""),
                    "port_description": cur.get("port_description", ""),
                    "mgmt_addrs": cur.get("mgmt_addrs", []),
                })

        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("Local Intf:"):
                flush()
                cur_port = line.split(":", 1)[1].strip()
                cur = {"mgmt_addrs": []}
                in_mgmt = False
            elif cur is None:
                continue
            elif line.startswith("Chassis id:"):
                cur["mac"] = _dotted_to_colon(line.split(":", 1)[1].strip())
                in_mgmt = False
            elif line.startswith("System Name:"):
                cur["sysname"] = line.split(":", 1)[1].strip()
                in_mgmt = False
            elif line.startswith("Port Description:"):
                cur["port_description"] = line.split(":", 1)[1].strip()
                in_mgmt = False
            elif line.startswith("Management Addresses:"):
                in_mgmt = True
            elif in_mgmt and line.startswith("IP:"):
                ip = line.split(":", 1)[1].strip()
                if ip:
                    cur["mgmt_addrs"].append(ip)
            elif line and not line.startswith("IP:"):
                # any other non-IP labeled line ends the mgmt-address block
                in_mgmt = False
        flush()
        return out
