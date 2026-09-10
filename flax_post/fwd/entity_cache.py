# flax_post/fwd/entity_cache.py
"""Drop EntityManager's cached configuration on a BMC before flashing it.

Port of the triage bmcfw pattern (services/api/bmcfw/worker.py's
clear_entity_cache dep + the bmc-clear-entity-cache bin it shells out to) --
NOT a call into that bin, which lives only on triage's host and needs
/opt/flax/bin + a bmc-root-shell credential neither post agent mounts today.
Post's fwd container already carries everything this needs instead:
openssh-client + sshpass are baked into the flax-control image (used today by
flax_switch_sense/flax_reconcile/flax_observe), and credentials-bmc.json --
already bind-mounted into flax-post-fwd -- carries a `root` entry alongside
the USERID/admin/oper ones (apply_bmc_scripts' bmc_root_password derives from
that same "root" entry). No new mount, no new credential, no image change.

WHY THIS EXISTS (verbatim rationale, unchanged from the triage bin):
/var/configuration/system.json caches the entity-manager configuration and
SURVIVES A FIRMWARE FLASH. EntityManager loads it at boot, builds its model
from it, then writes that model back -- so a cache describing the previous
build perpetuates itself across an update, and a correctly flashed unit can
still report stale sensors and SDR entries. Deleting the file forces a
rebuild from the actual hardware on the next start.

Deliberately no `systemctl restart` here -- this runs BEFORE the flash, and
the flash's own activation reboot restarts EntityManager for free.

Best-effort by construction: a cache-clear failure must NEVER block a
firmware update (the update is what matters; refusing to flash over a cache
file would leave a node on old firmware to avoid stale sensors -- the wrong
trade). Callers record the outcome, they never raise past this into the
flash gate.
"""
import json
import os
import subprocess

BMC_CREDS_PATH = os.environ.get("FLAX_POST_FWD_CREDS", "/etc/flax/credentials-bmc.json")
SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=10"]
SSH_TIMEOUT_SECS = 20
REMOTE_CMD = "rm -f /var/configuration/system.json && echo CLEARED"


def _load_root_cred(path=None):
    """The credentials-bmc.json entry with bmcuser=='root' -> (user, password),
    or None if the file is missing/vault-encrypted/malformed or carries no root
    entry. Mirrors roles/apply_bmc_scripts/tasks/main.yml's own selection
    (selectattr('bmcuser','equalto','root')|first) so this reads the exact same
    already-deployed credential apply_bmc_scripts renders bmc_root_password
    from -- no new secret, no new file."""
    if path is None:
        path = BMC_CREDS_PATH
    try:
        with open(path) as f:
            first = f.readline()
            if first.startswith("$ANSIBLE_VAULT"):
                return None
            data = json.loads(first + f.read())
    except (OSError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    for c in data:
        if isinstance(c, dict) and c.get("bmcuser") == "root" and c.get("bmcpass"):
            return (c["bmcuser"], c["bmcpass"])
    return None


def _ssh_run(ip, user, password, remote_cmd, timeout=SSH_TIMEOUT_SECS):
    """One `sshpass -e ssh -n -T ... user@ip remote_cmd` -> stdout text.

    Password goes through the SSHPASS env var (sshpass -e), never argv --
    RULE ZERO: a literal -p PASSWORD would land in this process's own argv/ps
    and in any CalledProcessError string. -n prevents ssh from reading our
    stdin; -T disables pty allocation (no remote shell prompt to strip)."""
    env = dict(os.environ)
    env["SSHPASS"] = password
    cmd = (["sshpass", "-e", "ssh", "-n", "-T"] + SSH_OPTS
           + ["%s@%s" % (user, ip), remote_cmd])
    r = subprocess.run(cmd, timeout=timeout, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, env=env)
    if r.returncode != 0:
        raise RuntimeError("ssh exited %d" % r.returncode)
    return r.stdout.decode("utf-8", errors="replace")


def clear_entity_cache(ip, creds_path=None):
    """Best-effort: (cleared: bool, reason: str). reason is "" on success, else
    a short machine-stable code -- never a raw exception string (which could
    embed transient text unsuitable for a stored/compared field) and never the
    password. NEVER raises -- every failure mode (no root cred, ssh timeout,
    ssh_unreachable, unexpected output) maps to (False, <reason>)."""
    cred = _load_root_cred(creds_path)
    if cred is None:
        return False, "no root credential in credentials-bmc.json"
    user, password = cred
    try:
        out = _ssh_run(ip, user, password, REMOTE_CMD)
    except subprocess.TimeoutExpired:
        return False, "ssh_timeout"
    except Exception:
        return False, "ssh_unreachable"
    if "CLEARED" in out:
        return True, ""
    return False, "rm_failed"
