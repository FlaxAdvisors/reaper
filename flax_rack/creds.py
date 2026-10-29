"""Runtime credentials, read from the vault-deployed /etc/flax files (never stored in the repo)."""
import json
from dataclasses import dataclass


class CredsError(Exception):
    pass


@dataclass
class Creds:
    sonic: tuple[str, str]
    bmc: list[tuple[str, str]]


def load_creds(main="/etc/flax/credentials.json", bmc="/etc/flax/credentials-bmc.json") -> Creds:
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
    return Creds((m["sonicuser"], m["sonicpass"]), pairs)
