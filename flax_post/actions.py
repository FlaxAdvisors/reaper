# flax_post/actions.py
"""Operator power + chassis-identify actions: pure, unit-testable action logic
for the two write endpoints (app.py POST /api/v1/power, /api/v1/identify).

Shells out to the ghost helpers `powertriage`/`identtriage` (self-loading creds,
resolved on PATH) via an injectable runner seam — mirrors the subprocess-seam
pattern in flax_post/observe/ipmi.py's _default_ipmi_runner. Tests replace
RUNNER (or pass runner=) so no real subprocess/network call ever happens here.
"""
import subprocess

# Firmware phases (post_state fw_bmc/fw_bios/fw_nic 'phase') during which a
# power-OFF must be blocked -- yanking power mid-flash can brick the board.
FW_ACTIVE = frozenset({"checking", "flashing", "monitoring", "activating"})

POWER_ACTIONS = {"on", "off", "cycle", "status"}
IDENT_MODES = {"on", "off", "force"}


def flash_active(record: dict) -> bool:
    """True if any of record['fw']['bmc'|'bios'|'nic'] is mid-flash (phase in
    FW_ACTIVE). Null-safe: missing 'fw', a missing slice, or a missing/None
    'phase' are all treated as not active."""
    fw = record.get("fw") or {}
    for slice_ in ("bmc", "bios", "nic"):
        phase = (fw.get(slice_) or {}).get("phase")
        if phase in FW_ACTIVE:
            return True
    return False


def _default_runner(argv: list, timeout: int) -> tuple:
    """Subprocess seam: run argv, return (returncode, combined stdout+stderr).
    Timeout -> (124, "timeout"); helper not on PATH -> (127, "<helper> not found").
    Mirrors observe/ipmi.py's _default_ipmi_runner subprocess-seam pattern."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except FileNotFoundError:
        return 127, f"{argv[0]} not found"


RUNNER = _default_runner


def run_power(bmc_ip: str, action: str, *, blocked: bool, runner=None) -> dict:
    """Invoke `powertriage <bmc_ip> <action>` unless blocked (caller passes
    blocked=action=='off' and flash_active(record) to withhold power-off during
    an in-flight firmware flash without ever shelling out)."""
    if action not in POWER_ACTIONS:
        raise ValueError(f"invalid power action: {action!r}")
    if blocked:
        return {"ok": False, "blocked": True, "reason": "firmware flash in progress"}
    run = runner or RUNNER
    rc, output = run(["powertriage", bmc_ip, action], timeout=120)
    return {"ok": rc == 0, "action": action, "ip": bmc_ip, "output": output}


def run_identify(bmc_ip: str, mode: str, *, runner=None) -> dict:
    """Invoke `identtriage <bmc_ip> <mode>` (chassis-identify LED)."""
    if mode not in IDENT_MODES:
        raise ValueError(f"invalid identify mode: {mode!r}")
    run = runner or RUNNER
    rc, output = run(["identtriage", bmc_ip, mode], timeout=60)
    return {"ok": rc == 0, "mode": mode, "ip": bmc_ip, "output": output}


def bmc_ip_for_port(port: str, slots: list) -> "str | None":
    """The bmc_ip of the slot record whose 'port' matches, or None if the port
    is unknown or the matching slot has no bmc_ip."""
    for slot in slots:
        if slot.get("port") == port:
            return slot.get("bmc_ip")
    return None
