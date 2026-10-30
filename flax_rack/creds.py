"""Runtime credentials, read from the vault-deployed /etc/flax files (never stored in the repo)."""
import json
from dataclasses import dataclass, field


class CredsError(Exception):
    pass


@dataclass
class Creds:
    sonic: tuple[str, str]
    bmc: list[tuple[str, str]]
    host: tuple = ()  # (user, pass) of the "flax" row; () when the file is absent
    redfish: list = field(default_factory=list)  # [(rfuser, rfpass)]; [] when absent
    cisco: tuple = ()  # the site turtle (IOS): cisco_user/cisco_pass; () when absent
    obmc: tuple = ()   # Facebook-OpenBMC ssh (wedge BMC, BC IOM): obmcuser/obmcpass


def _optional_rows(path):
    """Rows of an optional creds list; a missing/unreadable file is just no rows
    (the rung that needs it fails with "add <file>"; Discover/Close never need it)."""
    try:
        with open(path) as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def load_creds(main="/etc/flax/credentials.json", bmc="/etc/flax/credentials-bmc.json",
               host="/etc/flax/credentials-host.json",
               redfish="/etc/flax/credentials-redfish.json") -> Creds:
    try:
        with open(main) as f:
            m = json.load(f)
    except (OSError, ValueError) as e:
        raise CredsError(f"cannot read {main}: {e}") from e
    if not m.get("sonicuser") or not m.get("sonicpass"):
        raise CredsError("add sonicuser/sonicpass to the vaulted credentials.json")
    try:
        with open(bmc) as f:
            rows = json.load(f)
    except (OSError, ValueError) as e:
        raise CredsError(f"cannot read {bmc}: {e}") from e
    pairs = [(r["bmcuser"], r["bmcpass"]) for r in rows
             if isinstance(r, dict) and r.get("bmcuser") and "bmcpass" in r]
    hrow = next((r for r in _optional_rows(host)
                 if r.get("user") == "flax" and "pass" in r), None)
    rf = [(r["rfuser"], r["rfpass"]) for r in _optional_rows(redfish)
          if r.get("rfuser") and "rfpass" in r]
    opt = lambda u, p: (m[u], m[p]) if m.get(u) and p in m else ()
    return Creds((m["sonicuser"], m["sonicpass"]), pairs,
                 (hrow["user"], hrow["pass"]) if hrow else (), rf,
                 opt("cisco_user", "cisco_pass"), opt("obmcuser", "obmcpass"))
