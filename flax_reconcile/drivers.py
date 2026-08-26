# flax_reconcile/drivers.py
"""Write-capable switch drivers for flax-reconcile — the ONLY switch writer.

flap()/set_access_vlan() command sequences are lifted verbatim from
scripts/reaper_leased.py (AristaEAPI ~line 999, CumulusSSH ~line 1126,
CiscoIOSSSH ~line 1287). Per-process instances; switch-sense stays read-only
(spec §5). Both methods are ACTIVE: set_access_vlan is the VLAN-steering
mechanism, invoked only through the access-port-guarded enforcement in cycle.py.

Runners are injectable for tests. In production, load_switches() (Task B5b,
below) wires real eAPI/ssh callers from switches.json + the credential JSONs.

The real eAPI POST transport (_make_eapi_caller) and the IOS ssh runner
(_make_ios_runner) are LIFTED from scripts/reaper_leased.py
(AristaEAPI._call ~line 938, CiscoIOSSSH._default_runner ~line 1184). reaper's
scripts/ directory is NOT shipped in the flax-control image (the Dockerfile
COPYs only the flax_* packages + schema), so the transport must be copied here
rather than imported.

py3.11 note: IOS command strings use + concatenation, not f-strings, because
bang-fiesta runs Python 3.11 which forbids backslashes inside f-string
expressions. Arista uses lists (no issue). Cumulus has no \\n (also fine), but
uses concatenation for consistency.
"""
import base64
import http.client
import json
import logging
import os
import re
import ssl
import subprocess
import time

log = logging.getLogger("flax-reconcile.drivers")

FLAP_HOLD_SECONDS = 2  # mirrors reaper_leased AristaEAPI/CiscoIOSSSH default


class AristaEAPI:
    """eAPI-driven Arista driver. caller(list[str]) executes a config batch.

    Command sequences lifted verbatim from scripts/reaper_leased.py ~line 999.
    """

    def __init__(self, name, caller):
        self.name = name
        self._call = caller  # caller(list[str]) executes an eAPI config batch

    def set_access_vlan(self, interface, vid):
        """Force access VLAN on an Arista port and persist to flash.

        Lifted verbatim from reaper_leased.AristaEAPI.set_access_vlan (~line 999).
        write memory persists across reloads (Arista EOS default is RAM-only).
        """
        self._call([
            "configure terminal",
            "interface " + str(interface),
            "switchport access vlan " + str(vid),
            "end",
            "write memory",
        ])

    def flap(self, interface, hold_seconds=None):
        """Shutdown → sleep(hold) → no shutdown.

        Lifted verbatim from reaper_leased.AristaEAPI.flap (~line 1017).
        Two separate eAPI batches so the carrier-loss event is visible to DHCP
        clients. hold_seconds defaults to FLAP_HOLD_SECONDS (2s).
        """
        hold = hold_seconds if hold_seconds is not None else FLAP_HOLD_SECONDS
        self._call([
            "configure terminal",
            "interface " + str(interface),
            "shutdown",
            "end",
        ])
        time.sleep(hold)
        self._call([
            "configure terminal",
            "interface " + str(interface),
            "no shutdown",
            "end",
        ])

    def set_admin_up(self, interface):
        """Bring a port admin-up (the no-shutdown half of a flap).

        Used by the startup self-heal + SIGTERM completion path to un-strand a
        port that a flap left admin-down (`shutdown` issued, `no shutdown` never
        reached because the process died between the two eAPI batches). Same
        command form as flap()'s second batch.
        """
        self._call([
            "configure terminal",
            "interface " + str(interface),
            "no shutdown",
            "end",
        ])


class IOS:
    """SSH-driven Cisco IOS classic driver. runner(multiline_str) runs a config
    session over ssh. to_iface(port_token) maps swp<N> -> GigabitEthernet1/0/N.

    Command sequences lifted verbatim from reaper_leased.CiscoIOSSSH (~line 1287).

    String building uses + concatenation (NOT f-strings) because bang-fiesta
    runs Python 3.11 which forbids backslashes inside f-string expressions.
    """

    def __init__(self, name, runner, to_iface):
        self.name = name
        self.runner = runner          # runner(multiline_str) runs a config session
        self._to_iface = to_iface     # port-token -> GigabitEthernet1/0/N

    def set_access_vlan(self, port, vid):
        """Force access VLAN on a Cisco IOS port and persist to NVRAM.

        Lifted verbatim from reaper_leased.CiscoIOSSSH.set_access_vlan (~line 1287).
        write memory persists; takes 5-10s on 3750X (slow enrollment path, acceptable).
        """
        iface = self._to_iface(port)
        # Single ssh invocation: IOS reads multi-line input as a config session.
        # Using + concatenation, not f-string, for py3.11 compatibility (no backslashes
        # inside f-string expressions).
        self.runner(
            "configure terminal\n"
            "interface " + iface + "\n"
            "switchport access vlan " + str(vid) + "\n"
            "end\n"
            "write memory\n"
        )

    def flap(self, port, hold_seconds=None):
        """Shutdown → sleep(hold) → no shutdown over ssh.

        Lifted verbatim from reaper_leased.CiscoIOSSSH.flap (~line 1297).
        Two separate runner calls so the carrier-loss event is visible to DHCP clients.
        """
        iface = self._to_iface(port)
        hold = hold_seconds if hold_seconds is not None else FLAP_HOLD_SECONDS
        self.runner(
            "configure terminal\n"
            "interface " + iface + "\n"
            "shutdown\n"
            "end\n"
        )
        time.sleep(hold)
        self.runner(
            "configure terminal\n"
            "interface " + iface + "\n"
            "no shutdown\n"
            "end\n"
        )

    def set_admin_up(self, port):
        """Bring a port admin-up (the no-shutdown half of a flap) over ssh.

        Used by the startup self-heal + SIGTERM completion path to un-strand a
        port a killed flap left admin-down. Same command form as flap()'s second
        runner call. Uses + concatenation (no backslashes inside f-string {} for
        py3.11 on bang-fiesta).
        """
        iface = self._to_iface(port)
        self.runner(
            "configure terminal\n"
            "interface " + iface + "\n"
            "no shutdown\n"
            "end\n"
        )


class Cumulus:
    """SSH-driven Cumulus Linux (NCLU) driver. runner(shell_str) runs over ssh.

    Command sequences lifted verbatim from reaper_leased.CumulusSSH (~line 1126).
    """

    def __init__(self, name, runner):
        self.name = name
        self.runner = runner          # runner(shell_str) runs over ssh

    def set_access_vlan(self, port, vid):
        """Set access VLAN via NCLU and commit.

        Lifted verbatim from reaper_leased.CumulusSSH.set_access_vlan (~line 1126).
        """
        self.runner("net add interface " + str(port) + " bridge access " + str(vid))
        self.runner("net commit")

    def flap(self, port, hold_seconds=1):
        """ip link down → sleep → ip link up in a single shell invocation.

        Lifted verbatim from reaper_leased.CumulusSSH.flap (~line 1130).
        Single runner call: the sleep is performed on the switch side so
        no Python-side time.sleep is needed (avoiding the py3.11 f-string issue
        is also moot here since there are no backslashes, but we keep + concat
        for uniformity).
        """
        self.runner(
            "sudo ip link set " + str(port) + " down && "
            "sleep " + str(int(hold_seconds)) + " && "
            "sudo ip link set " + str(port) + " up"
        )

    def set_admin_up(self, port):
        """Bring a port admin-up (the up half of a flap) over ssh.

        Used by the startup self-heal + SIGTERM completion path to un-strand a
        port a killed flap left admin-down. Same command form as the up half of
        flap(). + concatenation for uniformity (no backslashes either way).
        """
        self.runner("sudo ip link set " + str(port) + " up")


# === Task B5b: real transport wiring + load_switches factory ================
#
# The eAPI POST transport, the IOS ssh runner, and the Cumulus ssh runner below
# are lifted from scripts/reaper_leased.py (AristaEAPI._call,
# CiscoIOSSSH._default_runner, CumulusSSH) and from
# flax_switch_sense/driver_cumulus.py (_default_runner).
# They are copied rather than imported because reaper's scripts/ dir is not in
# the flax-control image. Keep the protocol byte-for-byte faithful to reaper.


class SwitchUnreachable(Exception):
    """Raised when an eAPI/ssh write transport cannot reach the switch.

    Lifted from scripts/reaper_leased.py (~line 888) so callers (the kick
    ladder, the steer pass) can distinguish a transport failure from a logic
    error and fall through the ladder accordingly.
    """


# Unverified TLS context for eAPI: switches ship self-signed certs. reaper's
# context works on the host's older OpenSSL, but this code runs in the
# python:3.12-slim container whose OpenSSL defaults to SECLEVEL=2 and rejects
# Arista EOS's legacy eAPI cipher/cert with SSLV3_ALERT_HANDSHAKE_FAILURE.
# Drop to SECLEVEL=0 to accept it -- exactly what flax_switch_sense/driver_eos
# does for the same switches from this same container.
_UNVERIFIED_SSL = ssl.create_default_context()
_UNVERIFIED_SSL.check_hostname = False
_UNVERIFIED_SSL.verify_mode = ssl.CERT_NONE
_UNVERIFIED_SSL.set_ciphers("DEFAULT:@SECLEVEL=0")

# ssh known_hosts path mirrors reaper_leased.SSH_KNOWN_HOSTS (INSTALL_ROOT is
# /opt/flax in the deployed image). Used by the IOS ssh runner.
_INSTALL_ROOT = "/opt/flax"
_SSH_KNOWN_HOSTS = os.path.join(_INSTALL_ROOT, "var", "ssh", "known_hosts")


def _make_eapi_caller(host, user, password, scheme="https"):
    """Build a caller(list[str]) that POSTs a config batch via Arista eAPI.

    Lifted from scripts/reaper_leased.py AristaEAPI._call (~line 938): JSON-RPC
    runCmds with `enable` prepended for privileged mode, HTTP Basic auth, one
    fresh-connection retry on transport error. The returned closure is what the
    flax_reconcile.AristaEAPI(name, caller=...) driver invokes for every
    set_access_vlan / flap batch.

    Unlike reaper (which keeps a pooled keep-alive connection on a hot polling
    path), this caller opens a fresh connection per batch: flax-reconcile writes
    are infrequent (steer / kick events), so connection-pool complexity buys
    nothing and a fresh socket avoids stale-keepalive races entirely.
    """
    state = {"id": 0}

    def caller(cmds):
        wrapped = ["enable"] + list(cmds)
        state["id"] += 1
        body = json.dumps({
            "jsonrpc": "2.0",
            "method": "runCmds",
            "id": state["id"],
            "params": {"version": 1, "format": "json", "cmds": wrapped},
        }).encode()
        token = base64.b64encode((user + ":" + password).encode()).decode()
        headers = {"Content-Type": "application/json",
                   "Authorization": "Basic " + token,
                   "Connection": "close"}
        last_err = None
        for _attempt in (1, 2):
            try:
                if scheme == "https":
                    conn = http.client.HTTPSConnection(
                        host, timeout=10, context=_UNVERIFIED_SSL)
                else:
                    conn = http.client.HTTPConnection(host, timeout=10)
                conn.request("POST", "/command-api", body=body, headers=headers)
                r = conn.getresponse()
                raw = r.read()
                try:
                    conn.close()
                except Exception:
                    pass
                break
            except (http.client.HTTPException, ConnectionError, OSError) as e:
                last_err = e
        else:
            raise SwitchUnreachable(str(last_err))
        try:
            resp = json.loads(raw)
        except ValueError as e:
            raise SwitchUnreachable("non-JSON eAPI body: " + str(e))
        if "error" in resp:
            raise SwitchUnreachable(
                resp["error"].get("message", str(resp["error"])))
        # resp["result"][0] is the empty `enable` ack; drop it.
        return resp.get("result", [None])[1:]

    return caller


def _make_ios_runner(host, user, password):
    """Build a runner(multiline_str) that pushes a config session over ssh.

    Lifted from scripts/reaper_leased.py CiscoIOSSSH._default_runner (~line
    1184): sshpass + ssh with the classic-IOS kex/hostkey workarounds and an
    unconditional `terminal length 0` prefix so SHOW output never blocks on
    --More--. 60s timeout covers the slow `write memory` flash-write path on a
    3750X.

    NO ssh ControlMaster: a backgrounded master ssh reparents to the container's
    PID-1 python (no reaper) and leaks as a zombie until PID exhaustion (see
    feedback_ios_ssh_controlmaster_session_is_a_trap). The runner already sends
    the entire config session in one payload, so each call is a single plain
    foreground ssh that subprocess fully reaps — no warm socket needed.
    """
    def runner(cmd):
        full = ["sshpass", "-p", password, "ssh",
                "-tt",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=" + _SSH_KNOWN_HOSTS,
                "-o", "KexAlgorithms=+diffie-hellman-group14-sha1",
                "-o", "HostKeyAlgorithms=+ssh-rsa",
                "-o", "LogLevel=ERROR",
                user + "@" + host]
        prefix = "terminal length 0\n"
        payload = cmd if cmd.startswith("terminal length 0") else prefix + cmd
        if not payload.endswith("\n"):
            payload += "\n"
        try:
            return subprocess.check_output(full, input=payload, timeout=60,
                                           text=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            raise SwitchUnreachable(str(e))

    return runner


def _make_cumulus_runner(host, user, password, timeout=15):
    """Build a runner(cmd) -> str that runs a single shell command over ssh.

    Mirrors flax_switch_sense.driver_cumulus.CumulusDriver._default_runner:
    plain sshpass+ssh, no -tt, no ControlMaster (the ControlPath
    /run/reaper-leased-ssh-cm/ does NOT exist in the flax-control container and
    caused exit 3 failures with reaper's original -tt approach). Accept any host
    key via UserKnownHostsFile=/dev/null because switch certs rotate.

    Password redaction: on error we NEVER echo the subprocess argv (which
    contains '-p <password>'); we report only the remote command + exit code
    so the password is never leaked into logs or tracebacks.

    py3.11 note: no f-strings with backslashes are used here; ConnectTimeout
    uses str() + int(), not an f-string expression.
    """
    def runner(cmd):
        full = ["sshpass", "-p", password, "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "LogLevel=ERROR",
                "-o", "ConnectTimeout=" + str(max(1, int(timeout))),
                user + "@" + host, cmd]
        try:
            return subprocess.check_output(full, timeout=timeout, text=True)
        except subprocess.CalledProcessError as e:
            # NEVER include `full` (contains '-p <password>') in the exception.
            raise SwitchUnreachable(host + ": " + repr(cmd) + " exited " + str(e.returncode))
        except subprocess.TimeoutExpired:
            raise SwitchUnreachable(host + ": " + repr(cmd) + " timed out")
        except OSError as e:
            raise SwitchUnreachable(host + ": ssh exec failed: " + str(e))

    return runner


def _ios_to_iface(port):
    """swp<N> -> GigabitEthernet1/0/<N>. Lifted from reaper CiscoIOSSSH._to_iface
    (~line 1247). Raises ValueError on an unexpected port form so a malformed
    desired_port row faults loudly rather than pushing a bogus interface."""
    m = re.match(r"swp(\d+)$", str(port))
    if not m:
        raise ValueError("unexpected port form: " + repr(port))
    return "GigabitEthernet1/0/" + m.group(1)


def _obmc_creds_from_switch_creds(switch_creds):
    """Extract (obmc_user, obmc_pass) ssh pair for the BMC-LL kick rung.

    The openbmc *ssh* credentials live in credentials.json as the flat keys
    ``obmcuser`` / ``obmcpass`` -- the same file that carries eosuser/eospass
    and cisco_user/cisco_pass. This was confirmed against scripts/reaper_leased.py
    lines 1469/1485: ``ssh_runner(ip, creds["obmcuser"], creds["obmcpass"], ...)``
    where ``creds`` is the credentials.json dict loaded at line 4811.

    credentials-bmc.json (a list of {bmcuser, bmcpass}) is the IPMI cred list
    used only by the IPMI/observe path; flax-reconcile's kick is ssh-based and
    does NOT use it.

    Returns ('', '') when no usable cred is present -- kick_via_bmc_ll already
    no-ops on empty creds, so the BMC rung simply falls through to host_ssh /
    flap.
    """
    if not isinstance(switch_creds, dict):
        return ("", "")
    return (switch_creds.get("obmcuser", ""), switch_creds.get("obmcpass", ""))



def load_switches(switches_path, switch_creds_path, host_creds_path):
    """Factory: build the write-capable driver per switches.json entry + load
    the BMC/host credentials the kick ladder needs.

    Mirrors flax_switch_sense.fetcher.load_switches/make_driver (same
    switches.json schema: [{name, driver, host, credentials_key?}]) but builds
    the WRITE-capable flax_reconcile drivers with real eAPI/ssh transport.

    Both the switch transport creds (eosuser/eospass, cisco_user/cisco_pass) AND
    the openbmc ssh creds (obmcuser/obmcpass) come from the SAME credentials.json
    file passed as ``switch_creds_path``. This matches how scripts/reaper_leased.py
    uses them (lines 1469/1485: ssh_runner(ip, creds["obmcuser"], creds["obmcpass"])
    where creds is credentials.json). credentials-bmc.json is the IPMI cred list
    used only by the IPMI/observe path; flax-reconcile does NOT use IPMI.

    Args:
        switches_path:    path to switches.json
        switch_creds_path: path to credentials.json (eosuser/eospass +
                           cisco_user/cisco_pass + obmcuser/obmcpass)
        host_creds_path:  path to credentials-host.json

    Returns:
        switches:   dict[name -> driver]   (AristaEAPI / IOS / Cumulus instances)
        obmc_user:  str                    (BMC-LL ssh user, '' if absent)
        obmc_pass:  str
        host_creds: list[{user, pass}]     (host-ssh kick rung)

    Supported drivers: 'eos' (Arista eAPI write), 'ios' (Cisco IOS ssh write),
    'cumulus' (Cumulus Linux NCLU ssh write via plain sshpass+ssh — no -tt,
    no ControlMaster, password never logged). Unknown driver types are skipped
    with a warning so reconcile degrades gracefully when a new type appears in
    switches.json before its write transport is implemented.
    """
    with open(switches_path) as f:
        entries = json.load(f)
    if not isinstance(entries, list):
        raise ValueError(switches_path + ": expected a JSON list of switch entries")

    with open(switch_creds_path) as f:
        switch_creds = json.load(f)

    switches = {}
    for entry in entries:
        for key in ("name", "driver", "host"):
            if key not in entry:
                raise ValueError(
                    switches_path + ": entry " + repr(entry) + " missing key "
                    + repr(key))
        name = entry["name"]
        kind = entry["driver"]
        host = entry["host"]
        if kind == "eos":
            caller = _make_eapi_caller(
                host,
                switch_creds.get("eosuser", ""),
                switch_creds.get("eospass", ""))
            switches[name] = AristaEAPI(name, caller=caller)
        elif kind == "ios":
            runner = _make_ios_runner(
                host,
                switch_creds.get("cisco_user", ""),
                switch_creds.get("cisco_pass", ""))
            switches[name] = IOS(name, runner=runner, to_iface=_ios_to_iface)
        elif kind == "cumulus":
            runner = _make_cumulus_runner(
                host,
                switch_creds.get("cumuser", ""),
                switch_creds.get("cumpass", ""))
            switches[name] = Cumulus(name, runner=runner)
        else:
            # Skip any genuinely unknown driver type gracefully so reconcile
            # does not crash when a new switch type is added to switches.json
            # before its write transport is implemented.
            log.warning("load_switches: skipping switch %s -- driver %r is "
                        "not supported by flax-reconcile",
                        name, kind)
            continue

    with open(host_creds_path) as f:
        host_creds = json.load(f)
    obmc_user, obmc_pass = _obmc_creds_from_switch_creds(switch_creds)

    log.info("load_switches: built %d driver(s): %s", len(switches),
             ", ".join(sorted(switches)))
    return switches, obmc_user, obmc_pass, host_creds
