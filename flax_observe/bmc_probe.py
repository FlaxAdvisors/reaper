"""BMC probe paths — lifted from scripts/switchportrecond.py.

Functions:
  - probe_bmc_kind                  — openbmc vs traditional probe
  - chassis_serial_traditional      — FRU read via ipmitool
  - chassis_serial_openbmc          — FRU read via openbmc-ssh
  - bmc_power_and_sdr_traditional   — power state + watts in one IPMI session
  - bmc_power_status_traditional    — power state via ipmitool
  - bmc_power_status_openbmc        — power state via ssh+ipmitool

All keep signatures + behavior; only the logger changes.

Helpers also lifted here (called by the above):
  - _tcp_port_open
  - _ipmi_responsive
  - _default_ssh_runner
  - _parse_fru_product_name
  - _serial_from_fru
  - _parse_power_from_ipmi_output
  - _parse_watts_from_ipmi_output
"""
import base64
import http.client
import json
import logging
import os
import socket
import ssl
import subprocess
import tempfile

from .ipmi import _default_ipmi_runner

log = logging.getLogger("flax-observe.bmc_probe")

# ---------------------------------------------------------------------------
# Constants (mirrored from scripts/switchportrecond.py)
# ---------------------------------------------------------------------------

SSH_KNOWN_HOSTS = "/opt/flax/var/ssh/known_hosts"
SSH_TIMEOUT_SECS = 8

# Redfish identification transport. BMCs ship self-signed certs + legacy
# ciphers, so verification is off and SECLEVEL is lowered -- identical to
# flax_post.fwd.redfish / flax_reconcile.bmc_reset. Bounded timeout: the
# unauth service root answers in ~1s, but a slow/flaky BMC must never stall the
# per-port poll (an authed Systems read on these AMI boards can hang the TLS
# handshake -- observed on braintree TiogaPass).
_REDFISH_TIMEOUT = 5
_REDFISH_SSL = ssl.create_default_context()
_REDFISH_SSL.check_hostname = False
_REDFISH_SSL.verify_mode = ssl.CERT_NONE
_REDFISH_SSL.set_ciphers("DEFAULT:@SECLEVEL=0")

# ---------------------------------------------------------------------------
# TCP/IPMI port probes
# ---------------------------------------------------------------------------

def _tcp_port_open(host, port, timeout=1.0):
    """True iff a TCP connect to host:port completes within timeout."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def _ipmi_responsive(host, timeout=2.0):
    """True iff an IPMI handshake to host:623 elicits any response.

    Sends `channel info` with throwaway creds — any non-timeout return
    means a listener is there. Replaces nmap_ipmi (Task 16) with a probe
    that doesn't depend on `nmap` being installed.
    """
    try:
        subprocess.run(
            ["ipmitool", "-I", "lanplus", "-N", "1", "-R", "0",
             "-U", "x", "-P", "x", "-H", host, "channel", "info"],
            timeout=timeout, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# ---------------------------------------------------------------------------
# SSH runner
# ---------------------------------------------------------------------------

def _default_ssh_runner(host, user, password, cmd, timeout=SSH_TIMEOUT_SECS):
    """sshpass + ssh, returns stdout text. Caller catches exceptions.

    host may be a zoned IPv6 link-local literal (fe80::EUI64%<iface>); ssh
    accepts that directly via the user@host form, so the same runner reaches
    the OpenBMC over IPv6-LL (no IPv4 lease needed) as well as over IPv4.

    The legacy KEX / host-key algorithm opts let us reach old Wedge OpenBMC
    builds whose dropbear only offers diffie-hellman-group14-sha1 + ssh-rsa.
    "+algo" *adds* to the default set, so modern BMCs are unaffected.

    The password is passed to sshpass as an argv item (-p) and is never
    logged here -- this runner emits no log records.
    """
    # No pseudo-TTY: this runs a single non-interactive command whose stdout we
    # parse (os-release, FRU, ipmitool). Forcing one (-tt) makes Phosphor
    # OpenBMC (Tioga Pass) drop into an interactive console that returns NO
    # command output and exits non-zero -> the openbmc probe branch silently
    # fails and the board misclassifies as 'redfish'/OEM off the flaky IPMI
    # fallback. Interactive SOL uses a different path, not this runner.
    full = ["sshpass", "-p", password, "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", f"UserKnownHostsFile={SSH_KNOWN_HOSTS}",
            "-o", "ConnectTimeout=3",
            "-o", "KexAlgorithms=+diffie-hellman-group14-sha1",
            "-o", "HostKeyAlgorithms=+ssh-rsa",
            f"{user}@{host}", cmd]
    return subprocess.check_output(full, timeout=timeout, text=True,
                                   stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# FRU text parsers
# ---------------------------------------------------------------------------

def _parse_fru_product_name(fru_text):
    """Pull the product name from FRU text — handles two formats:

    ipmitool fru:  'Product Name         : Leopard'
    weutil:        'Product Name: WEDGE100S12V'

    Both are handled by splitting on the first ':' and checking that the
    stripped left-hand side is exactly 'Product Name' (so 'Product Part
    Number', 'Product Serial Number', etc. are never matched).
    """
    for line in fru_text.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        if k.strip() == "Product Name":
            return v.strip()
    return None


def _serial_from_fru(fru_text):
    """Pull 'Product Serial' (preferred) or 'Chassis Serial' (fallback)
    from `ipmitool fru` output.

    Skips empty values: ipmitool fru on a chassis with multiple FRU
    devices (e.g. Tioga Pass + Ava-M.2-SSD-Adap carrier) emits one
    'Product Serial' line per FRU. The carrier card's value is empty
    on most boards, and depending on enumeration order it can shadow
    the real builtin-FRU value. Walking past empty matches gets us to
    the first FRU that actually has a serial set."""
    for needle in ("Product Serial", "Chassis Serial"):
        for line in fru_text.splitlines():
            if needle in line:
                v = line.split(":", 1)[1].strip()
                if v:
                    return v
    return None


# ---------------------------------------------------------------------------
# Power + SDR parsers
# ---------------------------------------------------------------------------

def _parse_power_from_ipmi_output(text):
    """'on'/'off'/'unknown' from any line containing 'is on'/'is off'."""
    o = text.lower()
    if "is on" in o:
        return "on"
    if "is off" in o:
        return "off"
    return "unknown"


def _parse_watts_from_ipmi_output(text):
    """'NNN W' from the HSC input-power line of an `ipmitool sdr` dump.

    The sensor label varies by BMC firmware: the original boards expose
    'HSC Input Power', while Wiwynn OEM FW truncates the 16-char sensor-ID
    field and carries an 'MB ' prefix, yielding 'MB HSC In Power' (Input
    becomes In). Match on the NAME column containing both 'hsc' and 'power'
    AND a Watts-valued reading, so the HSC current/voltage/temperature rows
    (same 'HSC' stem) are never mistaken for input power.
    """
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) < 2:
            continue
        name = parts[0].lower()
        value = parts[1].strip()
        if "hsc" in name and "power" in name and "Watts" in value:
            return value.replace(" Watts", " W")
    return None


# ---------------------------------------------------------------------------
# BMC kind probe
# ---------------------------------------------------------------------------

def probe_bmc_kind(ip, credentials, bmc_creds,
                   ssh_runner=None, ipmi_runner=None, port_probe=None,
                   redfish_probe=None, redfish_creds=None):
    """Identify what kind of BMC sits at `ip`.

    Returns {"kind": "openbmc"|"traditional"|"redfish"|"unknown",
             "product_name": str|None, "creds_used": (user, pass)|None}.

    Strategy (first positive identification wins):
      1. Probe TCP:22 + UDP:623 + TCP:443 to fast-fail non-BMC IPs.
      2. If ssh:22 open AND `cat /etc/os-release` contains 'openbmc':
         openbmc path with credentials['obmcuser'/'obmcpass'].
      3. Elif udp:623 responsive: walk bmc_creds via ipmitool fru -> traditional.
      4. Elif tcp:443 serves a Redfish service root: 'redfish'. A Redfish-first
         BMC (SSH closed or an AMI board whose /etc/os-release isn't openbmc,
         and whose IPMI/creds don't answer) is invisible to steps 2-3 -- e.g.
         braintree's TiogaPass AMI BMCs. Identified via the UNAUTHENTICATED
         service root (fast, no creds); product_name is a best-effort authed
         read (often None on these boards -- see _default_redfish_probe). This
         is a FALLBACK after ssh/ipmi so an OpenBMC that also exposes 443 still
         resolves as 'openbmc' (no eindhoven regression).
      5. Else: 'unknown' (closed and unknown are binned together).
    """
    if ssh_runner is None:
        ssh_runner = _default_ssh_runner
    if ipmi_runner is None:
        ipmi_runner = _default_ipmi_runner
    if redfish_probe is None:
        redfish_probe = _default_redfish_probe
    if port_probe is None:
        port_probe = lambda h: {
            "ssh":  _tcp_port_open(h, 22, timeout=1.0),
            "ipmi": _ipmi_responsive(h, timeout=2.0),
            "redfish": _tcp_port_open(h, 443, timeout=1.0),
        }

    ports = port_probe(ip)

    if ports.get("ssh"):
        try:
            rel = ssh_runner(ip, credentials["obmcuser"],
                             credentials["obmcpass"], "cat /etc/os-release")
            if "openbmc" in rel.lower():
                pn = None
                try:
                    fru = ssh_runner(
                        ip, credentials["obmcuser"], credentials["obmcpass"],
                        "ipmitool fru 2>/dev/null"
                        " || cat /run/fru 2>/dev/null"
                        " || /usr/local/fbpackages/fruid/fruid-util iom 2>/dev/null"
                        " || weutil 2>/dev/null"
                        " || true")
                    pn = _parse_fru_product_name(fru)
                except Exception:
                    pass
                return {"kind": "openbmc", "product_name": pn,
                        "creds_used": (credentials["obmcuser"],
                                       credentials["obmcpass"])}
        except Exception:
            pass

    if ports.get("ipmi"):
        for c in bmc_creds:
            try:
                fru = ipmi_runner(ip, c["bmcuser"], c["bmcpass"], ["fru"])
                pn = _parse_fru_product_name(fru)
                return {"kind": "traditional", "product_name": pn,
                        "creds_used": (c["bmcuser"], c["bmcpass"])}
            except Exception:
                continue

    if ports.get("redfish"):
        # Feed the REDFISH creds (credentials-redfish.json, rfuser/rfpass ->
        # Administrator on these AMI boards), NOT bmc_creds (the USERID
        # IPMI/openbmc set) which these boards 401. Falls back to bmc_creds
        # only if no redfish creds were wired (best-effort product_name).
        try:
            info = redfish_probe(ip, redfish_creds or bmc_creds)
        except Exception:
            info = {"is_bmc": False, "product_name": None}
        if info.get("is_bmc"):
            # creds_used stays None: identification is unauthenticated (the
            # Redfish service root), and these boards' authed reads are
            # unreliable (401/timeout), so downstream power/serial -- gated on
            # `and creds_used` -- correctly no-ops rather than flailing at a
            # BMC we hold no working credential for.
            return {"kind": "redfish",
                    "product_name": info.get("product_name"),
                    "creds_used": None,
                    "redfish_version": info.get("redfish_version")}

    return {"kind": "unknown", "product_name": None, "creds_used": None}


def _default_redfish_probe(ip, redfish_creds, timeout=_REDFISH_TIMEOUT):
    """Identify a Redfish BMC at `ip`; capture redfish_version + product_name.

    Returns {"is_bmc": bool, "product_name": str|None, "redfish_version":
    str|None}. Identification + redfish_version come from an UNAUTHENTICATED GET
    /redfish/v1/ (a service root whose @odata.type names ServiceRoot, or that
    links Managers, is proof of a BMC -- fast ~1s, no creds, HOST-POWER
    INDEPENDENT). RedfishVersion is the signal bmc-fw uses to recognise an OEM
    board it can't update (the OEM AMI build's Redfish version is lower than
    flax-onetree's, and no higher OEM FW exists).

    product_name is best-effort: prefer the authed first-Systems-member
    Manufacturer+Model (walking redfish_creds -- Administrator on these AMI
    boards, from credentials-redfish.json), but that is SMBIOS-backed and blank
    when the host is powered off, so fall back to the service root's own Product
    ('AMI Redfish Server') so a redfish BMC always carries a stable identifier.
    Transport is legacy-TLS http.client, mirroring flax_post.fwd.redfish."""
    root = _redfish_get_json(ip, "/redfish/v1/", None, timeout)
    if not isinstance(root, dict):
        return {"is_bmc": False, "product_name": None, "redfish_version": None}
    is_bmc = ("serviceroot" in str(root.get("@odata.type", "")).lower()
              or "Managers" in root)
    if not is_bmc:
        return {"is_bmc": False, "product_name": None, "redfish_version": None}
    redfish_version = (root.get("RedfishVersion") or "").strip() or None
    product_name = None
    for c in (redfish_creds or []):
        cred = (c.get("bmcuser"), c.get("bmcpass"))
        if not all(cred):
            continue
        coll = _redfish_get_json(ip, "/redfish/v1/Systems", cred, timeout)
        members = coll.get("Members") if isinstance(coll, dict) else None
        if not members:
            continue
        odata = members[0].get("@odata.id")
        obj = _redfish_get_json(ip, odata, cred, timeout) if odata else None
        if isinstance(obj, dict):
            name = ((obj.get("Manufacturer") or "").strip() + " "
                    + (obj.get("Model") or "").strip()).strip()
            if name:
                product_name = name
                break
    if not product_name:
        # Host-off / blank-SMBIOS fallback: the service root's Product is BMC-
        # resident (e.g. 'AMI Redfish Server' on the v1.5.0 AMI build).
        product_name = (root.get("Product") or "").strip() or None
    if not product_name:
        # Last resort: some AMI builds (v1.17.0) expose NO Product either, only
        # an Oem vendor block. Synthesize a stable '<Vendor> Redfish' identifier
        # (e.g. 'Ami Redfish') so an OEM board that hides both Systems.Model and
        # root Product still carries a manifest-matchable product_name -- without
        # it these boards read 'unknown' and never become a bmc-fw candidate.
        oem = root.get("Oem")
        if isinstance(oem, dict) and oem:
            product_name = (next(iter(oem)).strip() + " Redfish").strip() or None
    return {"is_bmc": True, "product_name": product_name,
            "redfish_version": redfish_version}


def _redfish_get_json(ip, path, cred, timeout):
    """GET a Redfish path over legacy-TLS http.client; parsed JSON dict or None.

    cred is (user, pass) for HTTP Basic, or None for an unauthenticated GET.
    Any transport/HTTP/parse failure -> None (never raises): the caller treats
    absence as 'not identified' / 'no product name', never a crash."""
    if not ip or not path:
        return None
    headers = {"Connection": "close"}
    if cred and all(cred):
        token = base64.b64encode((cred[0] + ":" + cred[1]).encode()).decode()
        headers["Authorization"] = "Basic " + token
    try:
        conn = http.client.HTTPSConnection(ip, timeout=timeout,
                                           context=_REDFISH_SSL)
        conn.request("GET", path, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        try:
            conn.close()
        except Exception:
            pass
        if r.status != 200:
            return None
        return json.loads(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Power status probes
# ---------------------------------------------------------------------------

def bmc_power_status_traditional(ip, creds_pair):
    """ipmitool -U user -P pw -H ip power status -> 'on'/'off'/'unknown'."""
    try:
        out = _default_ipmi_runner(ip, creds_pair[0], creds_pair[1],
                                   ["power", "status"])
    except Exception:
        return "unknown"
    o = out.lower()
    if "is on" in o:
        return "on"
    if "is off" in o:
        return "off"
    return "unknown"


def bmc_power_status_openbmc(ip, creds_pair):
    """ssh root@bmc 'ipmitool power status'. Same parse as traditional."""
    try:
        out = _default_ssh_runner(ip, creds_pair[0], creds_pair[1],
                                  "ipmitool power status")
    except Exception:
        return "unknown"
    o = out.lower()
    if "is on" in o:
        return "on"
    if "is off" in o:
        return "off"
    return "unknown"


# ---------------------------------------------------------------------------
# Chassis serial probes
# ---------------------------------------------------------------------------

def chassis_serial_traditional(ip, creds_pair):
    """ipmitool fru -> Product Serial (or Chassis Serial fallback)."""
    try:
        out = _default_ipmi_runner(ip, creds_pair[0], creds_pair[1], ["fru"])
    except Exception:
        return None
    return _serial_from_fru(out)


def chassis_serial_openbmc(ip, creds_pair):
    """ssh + FRU chain -> Product/Chassis Serial.

    Tries the same chain as probe_bmc_kind: ipmitool fru → /run/fru →
    fruid-util iom → weutil (Wedge100s/Wedge400).
    """
    try:
        out = _default_ssh_runner(
            ip, creds_pair[0], creds_pair[1],
            "ipmitool fru 2>/dev/null"
            " || cat /run/fru 2>/dev/null"
            " || /usr/local/fbpackages/fruid/fruid-util iom 2>/dev/null"
            " || weutil 2>/dev/null"
            " || true",
        )
    except Exception:
        return None
    return _serial_from_fru(out)


# ---------------------------------------------------------------------------
# Combined power + SDR probe (one RMCP+ session)
# ---------------------------------------------------------------------------

def bmc_power_and_sdr_traditional(ip, creds_pair):
    """One-shot replacement for `bmc_power_status_traditional` +
    `bmc_input_power_traditional`. Both commands run in a SINGLE RMCP+
    session via `ipmitool ... exec FILE` — ipmitool keeps the same
    interface handle open across every line of the exec script.

    Cuts the per-poll BMC RMCP+ session count from 3 to 2 on AMI
    BMCs, which leaves more headroom in the BMC's session table for
    a long-running soltriage SOL session (the original motivation —
    eindhoven 2026-05-20).

    Returns (power, watts) where power in {'on','off','unknown'} and
    watts is 'NNN W' or None. Watts are read regardless of on/off: a
    powered-off host still draws standby power through the HSC, and the UI
    shows that real reading while the on/off status conveys the power state.
    """
    pwr = "unknown"
    watts = None
    tmp = tempfile.NamedTemporaryFile(
        mode="w", prefix="ipmi-cmds-", suffix=".txt", delete=False)
    try:
        tmp.write("power status\nsdr\n")
        tmp.close()
        try:
            out = _default_ipmi_runner(ip, creds_pair[0], creds_pair[1],
                                       ["exec", tmp.name])
        except Exception:
            return ("unknown", None)
        pwr = _parse_power_from_ipmi_output(out)
        watts = _parse_watts_from_ipmi_output(out)
    finally:
        try:
            os.unlink(tmp.name)
        except FileNotFoundError:
            pass
    return (pwr, watts)
