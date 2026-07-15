"""Load host SSH creds from /etc/flax/credentials-host.json ([{user,pass},...]).
Prefer the 'root' entry (no sudo encumbrance); else the first."""
import json
import os

DEFAULT_PATH = os.path.join(os.environ.get("FLAX_CONFIG_DIR", "/etc/flax"),
                            "credentials-host.json")


def load_host_creds(path: str | None = None) -> tuple:
    with open(path or DEFAULT_PATH) as f:
        entries = json.load(f)
    for e in entries:
        if e.get("user") == "root":
            return e["user"], e["pass"]
    return entries[0]["user"], entries[0]["pass"]
