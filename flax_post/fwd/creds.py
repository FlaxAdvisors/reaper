"""Load Redfish BMC credentials for the post firmware driver.

The post BMCs accept the same IPMI creds over Redfish, so the default is
/etc/flax/credentials-bmc.json (env FLAX_POST_FWD_CREDS) — the working
[{bmcuser, bmcpass}] list, NOT the empty credentials-redfish.json placeholder.
Parse mirrors flax_reconcile/bmc_reset.load_redfish_creds. Missing /
vault-encrypted / malformed / a docker bind-mount of a missing host file (an
empty DIRECTORY -> IsADirectoryError, an OSError subclass) all fail closed to [].
"""
import json
import os

REDFISH_CREDENTIALS_PATH = os.environ.get("FLAX_POST_FWD_CREDS", "/etc/flax/credentials-bmc.json")


def load_redfish_creds(path=None) -> list:
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
