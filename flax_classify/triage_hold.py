"""Triage reservation hold (spec 2026-09-27 §3, piece A) -- triage lane only.

The triage sweep is vacancy-GC: a desired_reservations row whose mac is not a
target this cycle is deleted, and the materializer then deletes the kea row
AND releases the live lease. A BMC reboot drops its mac out of the targets
within seconds (link blip -> no access_vid; empty FDB -> observe bmc_mac None)
although the blade never left the slot, so the returning BMC got a pool IP
(reservation-loss report 2026-09-27 §4).

A row is HELD (kept, not rewritten) while all of:
  * it is owner_role="triage" and its mac is not a target this cycle;
  * observe's row for the same (switch, port) still has chassis_sn latched;
  * observe either names this mac as the port's bmc_mac/nic_mac, or names no
    mac at all (R1: empty FDB / hold expired) -- a port whose observe row
    names OTHER macs has a new identity and is not held;
  * the mac was a real target less than hold_secs ago.
"Last real target" is in-process memory. A fresh process seeds every existing
triage row as seen now, so a restart never sweeps on its first cycle.
"""
import time

DEFAULT_HOLD_SECS = 900.0


def _norm(mac):
    return mac.strip().lower() if mac else None


class TriageHold:
    def __init__(self, hold_secs: float = DEFAULT_HOLD_SECS, clock=time.monotonic):
        self.hold_secs = float(hold_secs)
        self._clock = clock
        self._last_target: dict = {}
        self._seeded = False

    def held_macs(self, *, desired_rows, observe_rows, target_macs) -> set:
        now = self._clock()
        mine = [r for r in desired_rows if r.get("owner_role") == "triage"]
        if not self._seeded:
            for r in mine:
                self._last_target[_norm(r["mac"])] = now
            self._seeded = True
        targets = {_norm(m) for m in target_macs}
        for m in targets:
            self._last_target[m] = now

        obs = {(o["switch"], o["port"]): (o.get("resolved") or {})
               for o in observe_rows}
        held = set()
        live_rows = set()
        for r in mine:
            mac = _norm(r["mac"])
            live_rows.add(mac)
            if mac in targets:
                continue
            res = obs.get((r.get("switch"), r.get("port")))
            if not res or not res.get("chassis_sn"):
                continue
            named = {_norm(res.get("bmc_mac")), _norm(res.get("nic_mac"))} - {None}
            if named and mac not in named:
                continue
            seen = self._last_target.get(mac)
            if seen is None or now - seen >= self.hold_secs:
                continue
            held.add(mac)
        for mac in list(self._last_target):
            if mac not in live_rows and mac not in targets:
                del self._last_target[mac]
        return held
