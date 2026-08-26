"""ipmitool runner — lifted from scripts/switchportrecond.py:_default_ipmi_runner.

Cipher fallback chain (3 → no-cipher) preserved verbatim per the
parallel branch's RAKP-retry tuning. Retry budget kept at -R 3.

Also carries the Redfish-reset side-effect helper that fires when the
BMC returns 'insufficient resources for session' (AMI MegaRAC session-
table exhaustion). Rate-limited per BMC to one fire per hour.
"""
import json
import logging
import os
import subprocess
import threading
import time as _time_mod

log = logging.getLogger("flax-observe.ipmi")

# ---------------------------------------------------------------------------
# Constants (mirrored from scripts/switchportrecond.py)
# ---------------------------------------------------------------------------

IPMITOOL_TIMEOUT_SECS = 15

REDFISH_CREDENTIALS_PATH = "/etc/flax/credentials-redfish.json"
REDFISH_RESET_BIN = "/opt/flax/bin/bmc-reset-via-redfish"
REDFISH_RESET_RATE_LIMIT_SECS = 3600

# ---------------------------------------------------------------------------
# Redfish reset side-effect (session-table exhaustion recovery)
# ---------------------------------------------------------------------------

_REDFISH_RESET_LOCK = threading.Lock()
_LAST_REDFISH_RESET: dict = {}  # bmc_ip → time.time() of last fire

_IPMI_SESSION_EXHAUSTION_PATTERN = b"insufficient resources for session"


def _load_redfish_credentials(path=None):
    """Load /etc/flax/credentials-redfish.json, normalized to a list of
    {bmcuser, bmcpass} dicts (the shape every consumer already indexes).

    The file's real schema is rfuser/rfpass (Redfish creds -- e.g.
    Administrator on the braintree TiogaPass AMI BMCs, distinct from the
    USERID IPMI/openbmc set in credentials-bmc.json). ACCEPT BOTH: rfuser/
    rfpass (canonical) and bmcuser/bmcpass (back-compat) -- the old
    bmcuser-only filter silently dropped every rfuser entry, so both the
    Redfish-reset side-effect here AND observe's Redfish BMC probe got NO
    creds and 401'd. Returns [] on any error; best-effort, a missing/malformed
    file is not a polling-cycle failure."""
    if path is None:
        path = REDFISH_CREDENTIALS_PATH
    try:
        with open(path) as f:
            first = f.readline()
            if first.startswith("$ANSIBLE_VAULT"):
                return []
            data = json.loads(first + f.read())
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for c in data:
        if not isinstance(c, dict):
            continue
        user = c.get("rfuser", c.get("bmcuser"))
        password = c.get("rfpass", c.get("bmcpass"))
        if user and password:
            out.append({"bmcuser": user, "bmcpass": password})
    return out


def _maybe_fire_redfish_reset(bmc_ip):
    """Background-spawn `bmc-reset-via-redfish` for bmc_ip, rate-limited.

    Triggered from `_default_ipmi_runner` when the BMC returns
    'insufficient resources for session' — the AMI MegaRAC session-
    table-exhaustion symptom. Manager.Reset (ForceRestart) clears the
    table; ~3 min BMC outage with the host CPU left running.

    Rate-limited per-BMC to one fire per REDFISH_RESET_RATE_LIMIT_SECS
    so a misbehaving BMC that re-fills the session table immediately
    can't be reset-thrashed. Fire-and-forget via Popen — the recovery
    script blocks 3-5 min waiting for the BMC to come back; the poll
    cycle must not.
    """
    if not bmc_ip:
        return False
    now = _time_mod.time()
    with _REDFISH_RESET_LOCK:
        last = _LAST_REDFISH_RESET.get(bmc_ip, 0)
        if now - last < REDFISH_RESET_RATE_LIMIT_SECS:
            return False
        _LAST_REDFISH_RESET[bmc_ip] = now
    if not os.path.exists(REDFISH_RESET_BIN):
        return False
    creds = _load_redfish_credentials()
    cmd = [REDFISH_RESET_BIN, bmc_ip]
    if creds:
        cmd += [creds[0]["bmcuser"], creds[0]["bmcpass"]]
    try:
        subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# IPMI runner
# ---------------------------------------------------------------------------

def _default_ipmi_runner(host, user, password, args,
                         timeout=IPMITOOL_TIMEOUT_SECS):
    """Single ipmitool invocation, returns stdout text. Caller catches.

    Tries cipher suite 3 first (the historical default that works for
    Wedge OpenBMC and most traditional IPMI BMCs). If the BMC rejects
    it with 'invalid authentication algorithm' (common on newer
    Tioga Pass openBMC), falls back to no explicit -C flag — that
    lets ipmitool auto-negotiate the cipher.

    When both attempts fail and the combined stderr contains
    'insufficient resources for session' (AMI MegaRAC RMCP+ session
    table full), fires `bmc-reset-via-redfish` in the background to
    recover the BMC. Rate-limited; see `_maybe_fire_redfish_reset`.
    """
    common = ["-I", "lanplus", "-N", "2", "-R", "3",
              "-U", user, "-P", password, "-H", host]
    last_err = b""
    try:
        result = subprocess.run(
            ["ipmitool", "-C", "3"] + common + args,
            timeout=timeout, capture_output=True, check=True,
        )
        return result.stdout.decode("utf-8", errors="replace")
    except subprocess.CalledProcessError as e:
        last_err = e.stderr or b""
    try:
        result = subprocess.run(
            ["ipmitool"] + common + args,
            timeout=timeout, capture_output=True, check=True,
        )
        return result.stdout.decode("utf-8", errors="replace")
    except subprocess.CalledProcessError as e:
        last_err = last_err + b"\n" + (e.stderr or b"")
        if _IPMI_SESSION_EXHAUSTION_PATTERN in last_err.lower():
            _maybe_fire_redfish_reset(host)
        raise
