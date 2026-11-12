"""On-host driver: bash the agent pipes over SSH.

check -> current BIOS version from `dmidecode -t bios` (the "Version" field --
platform-generic, and what dmidecode reports post-flash+reboot; afulnx's
"System ROM ID" is a board-FAMILY id like TPCH, NOT the version). flash ->
afulnx write (vendor flags, self-reboots); amifldrv_mod.ko is ISO-baked, afulnx
+ the vendor .zip are curl'd from the HTTP share and unzipped on-target."""
import re
import subprocess

AMIFLDRV = "/export/share/amifldrv/amifldrv_mod.ko"
BIOS_SCRIPT = "dmidecode -t bios"
_BIOS_VER = re.compile(r"^\s*Version:\s*(\S+)", re.MULTILINE)


def parse_bios_version(dmidecode_bios_out: str) -> str | None:
    """First `Version:` in `dmidecode -t bios` output = the BIOS version."""
    m = _BIOS_VER.search(dmidecode_bios_out or "")
    return m.group(1) if m else None


def _preamble(entry: dict) -> str:
    # FLASH-only preamble: load the kernel module (ok if already loaded); fetch
    # afulnx to a tmpdir. afulnx_url is REQUIRED -- a manifest entry missing it
    # must raise KeyError (surfaced by the caller as a fault) not curl "".
    return (
        "set -e\n"
        "T=$(mktemp -d)\n"
        f"insmod {AMIFLDRV} 2>/dev/null || true\n"
        f"curl -fsS {entry['afulnx_url']} -o $T/afulnx && chmod +x $T/afulnx\n"
    )


def check_script(entry: dict) -> str:
    # Version check is dmidecode-only (no afulnx/insmod needed); entry unused.
    return BIOS_SCRIPT + "\n"


def flash_script(entry: dict) -> str:
    # Pull the vendor ZIP to the DUT and unzip on-target (RAM overlay) -- the
    # share stays as-is (no server-side unzip). afulnx the in-zip .bin.
    # bin_url/bin_path/flags are REQUIRED -- a manifest entry missing any of
    # them must raise KeyError rather than silently build a garbage/no-op
    # afulnx command (e.g. `afulnx $T/fw/ ...` with an empty bin_path).
    flags = " ".join(entry["flags"])
    return (
        _preamble(entry)
        + f"curl -fsS {entry['bin_url']} -o $T/fw.zip\n"
        + f"unzip -o $T/fw.zip -d $T/fw\n"
        + f"$T/afulnx $T/fw/{entry['bin_path']} {flags}\n"
    )


# The live-ISO creds are non-root (flax/…) but afulnx/insmod/dmidecode need
# root; the ISO grants the SSH user passwordless sudo, so run the whole piped
# script under `sudo -n bash -s`. Works as-is if a root creds entry is ever
# added (sudo as root is a no-op). See the 2026-07-07 deploy notes.
REMOTE_SHELL = "sudo -n bash -s"


def run_over_ssh(user: str, pw: str, ip: str, script: str, timeout: int = 300) -> tuple:
    """Pipe `script` to `sudo -n bash -s` on the host (root via passwordless
    sudo). Returns (rc, combined_output). Uses sshpass; StrictHostKeyChecking
    off (ephemeral live-ISO hosts)."""
    # ServerAlive* bounds a mid-command connection drop (e.g. a firmware reset
    # that severs this very link) to ~10s instead of hanging on dead TCP.
    cmd = ["sshpass", "-p", pw, "ssh", "-o", "StrictHostKeyChecking=no",
           "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10",
           "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
           f"{user}@{ip}", REMOTE_SHELL]
    try:
        p = subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "ssh timeout"
