"""Redfish Manager.Reset (ForceRestart) — operator-initiated BMC recovery.

Lifted from scripts/bmc-reset-via-redfish.sh: GET /redfish/v1/Managers (HTTP
Basic auth) -> resolve Members[0]["@odata.id"] -> POST <mgr>/Actions/Manager.Reset
with {"ResetType":"ForceRestart"}. The same recovery flax_observe auto-fires on
the "insufficient resources for session" symptom; this module is the executor
the flax-reconcile cycle drains when an operator clicks "Reset BMC (Redfish)".

TLS: BMCs ship legacy self-signed certs that the python:3.12-slim OpenSSL
(SECLEVEL=2) rejects with a handshake failure. Drop to CERT_NONE +
SECLEVEL=0 -- exactly the same context flax_reconcile.drivers uses for eAPI.

py3.11 note: bang-fiesta runs Python 3.11 which forbids backslashes inside
f-string expressions; this module uses + concatenation throughout.
"""
import base64
import http.client
import json
import logging
import ssl

log = logging.getLogger("flax-reconcile.bmc_reset")

REDFISH_CREDENTIALS_PATH = "/etc/flax/credentials-redfish.json"

# Unverified TLS for Redfish: BMCs ship legacy self-signed certs (same fix as
# the eAPI driver). CERT_NONE + SECLEVEL=0 so the 3.12-slim OpenSSL accepts them.
_UNVERIFIED_SSL = ssl.create_default_context()
_UNVERIFIED_SSL.check_hostname = False
_UNVERIFIED_SSL.verify_mode = ssl.CERT_NONE
_UNVERIFIED_SSL.set_ciphers("DEFAULT:@SECLEVEL=0")

_GET_TIMEOUT = 5
_POST_TIMEOUT = 10


def load_redfish_creds(path=None):
    """Load /etc/flax/credentials-redfish.json -> list of {bmcuser, bmcpass}.

    Missing/malformed/vault-encrypted file -> [] (the operator action then
    fails closed with a clear reason rather than crashing the cycle). A docker
    bind-mount of a missing host file creates an empty DIRECTORY, so an
    IsADirectoryError (an OSError subclass) is swallowed here too.
    """
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
    return [c for c in data if "bmcuser" in c and "bmcpass" in c]


def _basic_auth_header(user, password):
    token = base64.b64encode((user + ":" + password).encode()).decode()
    return "Basic " + token


def reset_bmc_via_redfish(bmc_ip, redfish_creds):
    """Force a Redfish Manager.Reset (ForceRestart) on bmc_ip.

    For each {bmcuser, bmcpass} cred: GET https://<bmc_ip>/redfish/v1/Managers
    with HTTP Basic auth; on HTTP 200 parse Members[0]["@odata.id"] and POST
    {"ResetType":"ForceRestart"} to <mgr>/Actions/Manager.Reset. AMI returns
    204; some BMCs return 200/202 -- all are accepted.

    Returns (ok: bool, detail: str). False when bmc_ip is empty, no cred
    authenticates, the Managers body has no members, or the reset POST is
    rejected. Does NOT poll for the BMC to come back (the cycle must not block
    3-5 min); a 2xx accept is treated as success.
    """
    if not bmc_ip:
        return False, "no bmc_ip"
    if not redfish_creds:
        return False, "no redfish credentials configured"

    last_detail = "no cred authenticated"
    for cred in redfish_creds:
        user = cred.get("bmcuser")
        password = cred.get("bmcpass")
        if not user or not password:
            continue
        headers = {"Authorization": _basic_auth_header(user, password),
                   "Connection": "close"}

        # 1. GET /redfish/v1/Managers -- verify auth + resolve the manager.
        try:
            conn = http.client.HTTPSConnection(
                bmc_ip, timeout=_GET_TIMEOUT, context=_UNVERIFIED_SSL)
            conn.request("GET", "/redfish/v1/Managers", headers=headers)
            r = conn.getresponse()
            status = r.status
            raw = r.read()
            try:
                conn.close()
            except Exception:
                pass
        except (http.client.HTTPException, ConnectionError, OSError) as e:
            last_detail = "GET /Managers transport error: " + str(e)
            continue

        if status != 200:
            last_detail = "GET /Managers HTTP " + str(status)
            continue

        try:
            members = json.loads(raw).get("Members", [])
        except ValueError as e:
            last_detail = "non-JSON /Managers body: " + str(e)
            continue
        if not members:
            last_detail = "no Manager members in /Managers"
            continue
        mgr = members[0].get("@odata.id")
        if not mgr:
            last_detail = "Manager member missing @odata.id"
            continue

        # 2. POST Manager.Reset ForceRestart.
        post_headers = dict(headers)
        post_headers["Content-Type"] = "application/json"
        body = json.dumps({"ResetType": "ForceRestart"}).encode()
        reset_path = mgr + "/Actions/Manager.Reset"
        try:
            conn = http.client.HTTPSConnection(
                bmc_ip, timeout=_POST_TIMEOUT, context=_UNVERIFIED_SSL)
            conn.request("POST", reset_path, body=body, headers=post_headers)
            r = conn.getresponse()
            status = r.status
            r.read()
            try:
                conn.close()
            except Exception:
                pass
        except (http.client.HTTPException, ConnectionError, OSError) as e:
            last_detail = "POST Manager.Reset transport error: " + str(e)
            continue

        if status in (200, 202, 204):
            return True, "Manager.Reset accepted (HTTP " + str(status) + ")"
        last_detail = "Manager.Reset HTTP " + str(status)

    return False, last_detail
