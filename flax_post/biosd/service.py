"""Probe loop for the post BIOS firmware driver (twin of flax_post.fwd.service).

probe_once(deps) walks every post host device (dev with an ssh-reachable
host_ip), reads its DMI product via `dmidecode`, matches it against the BIOS
manifest, reads the current BIOS version via afulnx, classifies it against the
manifest target, and writes the row (report-only — no flashing here).

`deps` (real wiring in __main__._Deps, fakes in tests) supplies:
  hosts() -> [device dict]         (post 'host' rows with a host_ip)
  run(ip, script) -> (rc, output)  (ssh the script to the host)
  matcher                          (a biosd.manifest.BiosMatcher)
  set_row(port, **fields) -> dict  (store write + fw_bios mirror)
  bmc_phase(port) -> str | None    (this port's fw_bmc.phase, for the flash gate)
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import classify, config, driver

log = logging.getLogger("flax-post-biosd")

DMI_SCRIPT = "dmidecode -t system"

# How long a claimed port may sit at its pre-flash baseline version before the
# probe loop gives up on it and surfaces a fault (the flash session drops on
# self-reboot, so we can only tell success/stall apart by re-reading the
# version on later passes, not by the flash call's own rc).
FLASH_TIMEOUT_S = 900


class Registry:
    """Thread-safe set of ports with an in-flight BIOS flash.

    Shared between enforce_once (which claims a port for the duration of an
    autonomous flash) and the probe loop (which must not let a routine probe
    pass reclaim/re-flash a port already claimed).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._active = set()

    def claim(self, port) -> bool:
        """Mark port in-flight; return False if it was already claimed."""
        with self._lock:
            if port in self._active:
                return False
            self._active.add(port)
            return True

    def release(self, port) -> None:
        with self._lock:
            self._active.discard(port)

    def busy(self, port) -> bool:
        with self._lock:
            return port in self._active


def _probe_host(deps, registry, dev) -> dict | None:
    """Classify one host; returns the row written, the skipped dict when the
    port is claimed, or None when the probe raised."""
    port = dev.get("port")
    try:
        ip = dev["host_ip"]
        if registry is not None and registry.busy(port):
            _reprobe_inflight(deps, registry, port, ip)
            return {"port": port, "skipped": "flashing"}
        rc, dmi = deps.run(ip, DMI_SCRIPT)
        if rc != 0:
            return deps.set_row(port, phase="unreachable", current=None, target=None, fault_reason="")
        entry = deps.matcher.match(dmi)
        if entry is None:
            return deps.set_row(port, phase="unsupported", current=None, target=None, fault_reason="")
        rc, out = deps.run(ip, driver.check_script(entry))
        current = driver.parse_bios_version(out)
        phase = classify.classify(current, entry["target"])
        # `entry` rides along on the row so enforce_once has the flags/urls for
        # the flash without re-matching dmidecode output mid-enforce pass.
        return deps.set_row(port, phase=phase, current=current, target=entry["target"],
                            fault_reason="", entry=entry)
    except Exception:
        log.exception("biosd probe failed for %s", port)
        return None


def scan_hosts(deps) -> list:
    """The hosts a SCAN pass may ssh: every post host minus the powered-off
    ones (ruling 2026-09-12). An off host cannot answer, and writing
    `unreachable` over its last real classification is exactly the regression
    the tile's Firmware latch exists to hide; the on-demand probe_port path is
    deliberately not gated (the ladder asks only once the host is up). Deps
    without the hook (older fakes) scan everything."""
    off = set(deps.powered_off_ports()) if hasattr(deps, "powered_off_ports") else set()
    return [d for d in deps.hosts() if d.get("port") not in off]


def probe_once(deps, registry=None, workers=None):
    """One probe pass over every post host: classify current-vs-target into the store.

    Fanned out across a worker pool (each host is an independent SSH session) —
    `workers` defaults to config.MAX_PARALLEL; pass 1 (or a single host) to force
    the sequential path. Each host's work is isolated in its own try/except so
    one bad/raising host can't abort the rest of the pass.
    """
    hosts = [dev for dev in scan_hosts(deps) if dev.get("host_ip")]
    if not hosts:
        return

    n = config.MAX_PARALLEL if workers is None else workers
    n = max(1, min(n, len(hosts)))
    if n == 1:
        for dev in hosts:
            _probe_host(deps, registry, dev)
    else:
        with ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(lambda d: _probe_host(deps, registry, d), hosts))


def probe_port(deps, registry, port) -> dict | None:
    """On-demand probe of ONE port (spec §7). None when the port is not a post host."""
    dev = next((d for d in deps.hosts() if d.get("port") == port and d.get("host_ip")), None)
    if dev is None:
        return None
    if registry is not None and registry.busy(port):
        return {"port": port, "skipped": "flashing"}
    row = _probe_host(deps, registry, dev)
    return row if row is not None else {"port": port, "ok": False, "reason": "probe failed; see daemon log"}


def _reprobe_inflight(deps, registry, port, ip):
    """Re-read the version of a port claimed by enforce_once, without letting
    the normal classify path stomp its "flashing" phase. Only two outcomes end
    the claim: the version now matches target (success), or it's still at the
    pre-flash baseline past FLASH_TIMEOUT_S (stalled -> fault)."""
    row = deps.last_row(port) if hasattr(deps, "last_row") else None
    row = row or {}
    entry = row.get("entry")
    if entry is None:
        return
    rc, out = deps.run(ip, driver.check_script(entry))
    if rc != 0:
        return  # host still down/rebooting -- leave "flashing", try again next pass
    current = driver.parse_bios_version(out)
    target = entry.get("target")
    baseline = row.get("baseline")
    flashed_at = row.get("flashed_at") or 0
    if current == target:
        deps.set_row(port, phase="up_to_date", current=current, fault_reason="")
        registry.release(port)
    elif current == baseline and (time.time() - flashed_at) > FLASH_TIMEOUT_S:
        deps.set_row(port, phase="fault",
                     fault_reason="flash timed out after %ds" % FLASH_TIMEOUT_S)
        registry.release(port)
    # else: still mid-flash/reboot -- no write, claim held


def enforce_once(deps, registry, mode, allowlist):
    """One enforce pass: flash each allowlisted, flash-eligible, not-busy host.

    Deliberately SEQUENTIAL (unlike probe_once) — serialized flashing is safer
    than fanning out afulnx runs across many hosts at once, so this loop is
    left single-threaded on purpose.

    Each host's work is isolated in its own try/except (mirroring probe_once
    and flax_post.fwd's per-host isolation) so one bad/raising host — e.g. a
    malformed manifest entry raising KeyError out of driver.flash_script —
    can't abort the rest of the pass; it's logged, surfaced as a fault, and
    the registry claim (if taken) is released so the port isn't stranded
    "flashing" until FLASH_TIMEOUT_S."""
    for dev in deps.hosts():
        port = dev.get("port")
        try:
            ip = dev.get("host_ip")
            if registry.busy(port):
                continue
            row = deps.last_row(port) or {}
            entry = row.get("entry")
            if entry is None:
                continue
            # ssh_ok reflects whether the LAST probe pass actually reached this
            # host: a probe phase of unreachable/unknown means the SSH check
            # itself didn't succeed (dmidecode/afulnx didn't answer), so it
            # must not be treated as flash-eligible. needs_update implies the
            # host was reachable at probe time.
            ssh_ok = row.get("phase") not in ("unreachable", "unknown")
            eligible = classify.flash_eligible(
                port, mode, allowlist, deps.bmc_phase(port),
                ssh_ok=ssh_ok, phase=row.get("phase"))
            if not eligible:
                continue
            if not registry.claim(port):
                continue
            deps.set_row(port, phase="flashing", baseline=row.get("current"),
                         flashed_at=time.time())
            try:
                deps.run(ip, driver.flash_script(entry))
            except Exception as e:
                log.exception("biosd flash failed for %s", port)
                try:
                    deps.set_row(port, phase="fault", fault_reason=str(e))
                except Exception:
                    pass
                registry.release(port)
        except Exception:
            log.exception("biosd enforce failed for %s", port)
