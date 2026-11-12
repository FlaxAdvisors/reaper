"""Deploy records (spec §8.5): /etc/flax/deployed/<component>.json on every target
bang -- reaper commit, image, who, from where, when. status/report read them back
(probe_remote.deployed). Never a secret: these fields only."""
import json
import shlex
from datetime import datetime
from typing import Optional

from reapercli.lib.inventory import ssh_target
from reapercli.lib.remote import fanout

DEPLOYED_DIR = "/etc/flax/deployed"

# Runs on the bang under python3.12: JSON list on stdin, target dir in argv[1].
WRITER = """
import json, os, sys, tempfile
d = sys.argv[1]
for rec in json.load(sys.stdin):
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".rec-")
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, os.path.join(d, rec["component"] + ".json"))
"""


def make_record(component: str, commit: str, image: Optional[str], user: str, host: str,
                now: datetime, extra: Optional[dict] = None) -> dict:
    """extra: further non-secret fields (triage: the triage repo commit)."""
    return {"component": component, "commit": commit, "image": image, "user": user,
            "from": host, "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), **(extra or {})}


def write_records(sh, bangs: list, records: list, runner) -> dict:
    cmd = (f"sudo -n install -d -m 0755 {DEPLOYED_DIR} && "
           f"sudo -n python3.12 -c {shlex.quote(WRITER)} {DEPLOYED_DIR}")
    targets = {b.name: ssh_target(b, sh.on_site) for b in sh.bangs if b.name in bangs}
    return fanout(targets, cmd, json.dumps(records), 30, runner)
