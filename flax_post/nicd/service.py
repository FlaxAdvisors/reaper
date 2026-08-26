"""Probe + enforce loops for the post NIC firmware agent (twin of
flax_post.biosd.service). probe_once reads every post host's Mellanox cards and
classifies them (report-only); enforce_once flashes (Task 6)."""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import classify, config, driver

log = logging.getLogger("flax-post-nicd")


class Registry:
    def __init__(self):
        self._lock = threading.Lock()
        self._active = set()

    def claim(self, port) -> bool:
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


def _classify_row(deps, dev):
    """Query one host, classify its cards, return the row fields to write."""
    ip = dev["host_ip"]
    rc, out = deps.run(ip, driver.QUERY_SCRIPT)
    if rc != 0:
        return dict(phase="unreachable", devices=[], fault_reason="")
    cards = driver.parse_cards(out)
    classified = [classify.classify_device(c, deps.matcher.match(c["psid"])) for c in cards]
    _chk, _upd, roll = classify.aggregate(classified)
    return dict(phase=roll, devices=classified, fault_reason="")


def probe_once(deps, registry=None, workers=None):
    hosts = [d for d in deps.hosts() if d.get("host_ip")]
    if not hosts:
        return

    def work(dev):
        port = dev.get("port")
        try:
            if registry is not None and registry.busy(port):
                return
            deps.set_row(port, **_classify_row(deps, dev))
        except Exception:
            log.exception("nicd probe failed for %s", port)

    n = config.MAX_PARALLEL if workers is None else workers
    n = max(1, min(n, len(hosts)))
    if n == 1:
        for dev in hosts:
            work(dev)
    else:
        with ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(work, hosts))


def wait_card_reset(deps, ip, pci, entry, deadline, clock) -> bool:
    """Poll a single card after mstfwreset until FW==target AND PSID==target,
    tolerating SSH drops. True on match, False at the deadline."""
    while clock() < deadline:
        rc, out = deps.run(ip, driver.card_query_script(pci))
        if rc == 0:
            cards = driver.parse_cards(out)
            if cards and cards[0].get("current") == entry["target_version"] \
                     and cards[0].get("psid") == entry["target_psid"]:
                return True
        # else: card still resetting / host briefly unreachable -> retry
    return False


def enforce_once(deps, registry, mode, allowlist, clock=time.time):
    """Flash every eligible, not-busy host's cards; sequential (safer than
    fanning out mstflint). Per-host isolation so one bad host can't abort."""
    for dev in deps.hosts():
        port = dev.get("port")
        try:
            ip = dev.get("host_ip")
            if registry.busy(port):
                continue
            row = deps.last_row(port) or {}
            devices = row.get("devices") or []
            ssh_ok = row.get("phase") not in ("unreachable", None)
            if not classify.flash_eligible(port, mode, allowlist, deps.bmc_phase(port),
                                           deps.bios_phase(port), ssh_ok, devices):
                continue
            if not registry.claim(port):
                continue
            try:
                _flash_node(deps, port, ip, devices, clock)
            finally:
                registry.release(port)
        except Exception:
            log.exception("nicd enforce failed for %s", port)


def _flash_node(deps, port, ip, devices, clock):
    deadline = clock() + config.FLASH_TIMEOUT
    deps.set_row(port, phase="flashing")
    needbmcreset = False
    for dev in devices:
        if dev.get("phase") != "needs_update" or dev.get("secure"):
            continue
        # dir/bin come from the manifest by the card's CURRENT psid:
        m = deps.matcher.match(dev["psid"])
        if m is None:
            continue
        rc, out = deps.run(ip, driver.flash_script(m, dev["pci"], config.SHARE_BASE))
        if not wait_card_reset(deps, ip, dev["pci"], m, deadline, clock):
            deps.set_row(port, phase="fault", fault_reason="card %s reset did not take" % dev["pci"])
            return
        # UEFI must be ON after the flash (else the card won't UEFI-BIOS-boot).
        # Only judge the knob when a card actually came back (an empty re-query
        # is a transient read, not a missing knob).
        rc, out = deps.run(ip, driver.card_query_script(dev["pci"]))
        cards = driver.parse_cards(out)
        if cards:
            uefi = cards[0].get("uefi")
            if uefi is False:                         # knob present but off -> enable it
                deps.run(ip, driver.uefi_script(dev["pci"]))
                if not wait_card_reset(deps, ip, dev["pci"], m, deadline, clock):
                    deps.set_row(port, phase="fault", fault_reason="uefi reset did not take on %s" % dev["pci"])
                    return
                needbmcreset = True
            elif uefi is None:                        # target FW exposes no UEFI knob
                deps.set_row(port, phase="fault",
                             fault_reason="no EXP_ROM_UEFI_x86_ENABLE on %s at target FW; cannot enable UEFI boot" % dev["pci"])
                return
    if needbmcreset or config.FORCE_BMC_RESET:
        # Reboot the BMC over REDFISH (Manager.Reset) -- out of context for
        # SSH/IPMI, so it dodges the `ipmitool mc reset cold` hang that bricks
        # these OpenBMCs. 2xx accept = success; the BMC reboots in the
        # background (we don't block-poll for it).
        ok, detail = deps.redfish_bmc_reset(port)
        log.info("bmc-reset %s: redfish Manager.Reset -> ok=%s %s", port, ok, detail)
        if not ok:
            deps.set_row(port, phase="fault", fault_reason="bmc reset rejected: %s" % detail)
            return
    # re-query all cards -> aggregate
    rc, out = deps.run(ip, driver.QUERY_SCRIPT)
    cards = [classify.classify_device(c, deps.matcher.match(c["psid"])) for c in driver.parse_cards(out)]
    _chk, _upd, roll = classify.aggregate(cards)
    deps.set_row(port, phase=roll, devices=cards, fault_reason="")
