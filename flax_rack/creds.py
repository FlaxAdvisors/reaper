"""Runtime credentials, read from the vault-deployed /etc/flax files (never stored in the repo)."""
import json
from dataclasses import dataclass, field


class CredsError(Exception):
    pass


# credentials.json's flat switch-login keys per NOS (= flax_switch_sense make_driver's)
SWITCH_KEYS = {"sonic": ("sonicuser", "sonicpass"), "eos": ("eosuser", "eospass"),
               "cumulus": ("cumuser", "cumpass"), "ios": ("cisco_user", "cisco_pass")}


@dataclass
class Creds:
    # the switch logins present in credentials.json, by its flat keys (the dict
    # flax_switch_sense's make_driver takes): rack switches by geometry nos, the site
    # turtle (ios: cisco_user/cisco_pass)
    switch: dict
    bmc: list[tuple[str, str]]
    host: tuple = ()  # (user, pass) of the "flax" row; () when the file is absent
    redfish: list = field(default_factory=list)  # [(rfuser, rfpass)]; [] when absent
    obmc: tuple = ()   # Facebook-OpenBMC ssh (wedge BMC, BC IOM): obmcuser/obmcpass

    def has_switch(self, nos: str) -> bool:
        k = SWITCH_KEYS.get(nos)
        return bool(k and self.switch.get(k[0]) and k[1] in self.switch)


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
               redfish="/etc/flax/credentials-redfish.json", noses=("sonic",)) -> Creds:
    """noses: the rack switches' NOSes (geometry `nos`): each one's login is required
    (a site with no SONiC rack switch needs no sonic keys)."""
    try:
        with open(main) as f:
            m = json.load(f)
    except (OSError, ValueError) as e:
        raise CredsError(f"cannot read {main}: {e}") from e
    for nos in sorted(set(noses)):
        if nos not in SWITCH_KEYS:
            raise CredsError(f"unsupported switch nos {nos!r} (want one of "
                             f"{', '.join(sorted(SWITCH_KEYS))})")
        u, p = SWITCH_KEYS[nos]
        if not m.get(u) or not m.get(p):
            raise CredsError(f"add {u}/{p} to the vaulted credentials.json")
    switch = {k: m[k] for pair in SWITCH_KEYS.values() for k in pair if k in m}
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
    return Creds(switch, pairs, (hrow["user"], hrow["pass"]) if hrow else (), rf,
                 opt("obmcuser", "obmcpass"))
