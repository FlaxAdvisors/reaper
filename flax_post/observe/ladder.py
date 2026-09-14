# flax_post/observe/ladder.py
"""The per-slot readiness ladder: a PURE rung machine (spec 2026-09-11
post-slot-ladder §4/§5). No I/O here. The worker (observe/worker.py) gathers
the one piece of evidence `evidence_needed` asks for, calls `advance`, then
performs the returned actions and persists the returned slice.

Fault-and-stop: a budget expiry writes ladder.fault and the machine emits no
further actions until the slice is reset — by a human off-to-on (the IPMI
lane writes human_reset) or by the hold-file gesture (touch, then remove).
"""
import copy
import os

from ..blades import BOOT_MARKER_RUNGS, RUNGS, bmc_ready_budget_s, bmc_ready_retry_budgets, ladder_budget_s

# Budgets live in blades.LADDER_BUDGET_S (the tile shows them); see budget_s.
POWER_COOLDOWN_S = int(os.environ.get("FLAX_POST_LADDER_POWER_COOLDOWN_S", "900"))
_MARK_KEY = {"tftp-seen": "tftp", "ipxe-seen": "ipxe", "live-iso-seen": "iso"}
_EVIDENCE = {"tftp-seen": "tftp", "ipxe-seen": "ipxe", "live-iso-seen": "iso",
             "host-pinged": "ping", "host-ssh": "ssh", "bmc-ready": "bmcdata",
             "agent-reachable": "agent", "qualify": "agent"}
# After a FAIL verdict the battery keeps running on the node and the worker
# keeps collecting; the agent being unreachable this long (node or BMC
# rebooted) ends the collection.
POST_VERDICT_AGENT_GRACE_S = int(os.environ.get("FLAX_POST_LADDER_POST_VERDICT_GRACE_S", "90"))
_NEXT = {r: RUNGS[i + 1] for i, r in enumerate(RUNGS[:-1])}
RUNG_INDEX = {r: i for i, r in enumerate(RUNGS)}


def budget_s(rung):
    """Seconds allowed on `rung`, None for rungs that never time out
    (blades.ladder_budget_s, env override FLAX_POST_LADDER_<RUNG>_S)."""
    return ladder_budget_s(rung)


def new_ladder(now) -> dict:
    return {"rung": "bmc-pinged", "since": now, "born_at": now, "power_on_at": None,
            "power_on_pending": False, "marks": {}, "fault": None, "probe_calls": {}}


def human_reset(now) -> dict:
    """A human powered the blade on: restart as if first seen, past power-on."""
    lad = new_ladder(now)
    lad.update(rung="tftp-seen", power_on_at=now, human_power_on=True)
    return lad


def occupant_reset(born_at, now) -> dict:
    """A different blade answers in the slot (ipmi.occupant_change): start as if
    first seen, born when the previous blade last answered there, so the boot
    proof can credit a PXE boot that ran before the new BMC answered (et10b4
    2026-09-14). A missing or future birth is `now`."""
    lad = new_ladder(now)
    lad["born_at"] = born_at if (born_at is not None and born_at <= now) else now
    return lad


def fault(ladder, rung, reason, now) -> dict:
    lad = copy.deepcopy(ladder)
    lad["fault"] = {"rung": rung, "reason": reason, "at": now}
    lad["power_on_pending"] = False
    return lad


def evidence_needed(ladder):
    if not ladder or ladder.get("fault"):
        return None
    if ladder.get("rung") == "host-ssh" and _needs_boot_proof(ladder):
        return "ssh-boot"
    return _EVIDENCE.get(ladder.get("rung"))


# Boot markers of one PXE boot sit 67-74 s apart (tftp -> iPXE -> ISO, every
# boot on bang-gouda 2026-09-14); anything wider is a different or broken boot.
BOOT_PROOF_BOUND_S = 120


def _needs_boot_proof(lad) -> bool:
    """Already on at first sight, with a birth time to bound the log search.
    Ladders written before born_at existed keep plain ssh (markers unknown)."""
    return bool((lad.get("marks") or {}).get("skipped")) and lad.get("born_at") is not None


def _prove_boot(born_at, times, kernel_boot) -> "dict | None":
    """{tftp, ipxe, iso, kernel} when the running kernel came from a PXE boot
    logged after born_at, else None. Newest iPXE since birth; its tftp within
    BOOT_PROOF_BOUND_S before; its ISO within BOOT_PROOF_BOUND_S after; and the
    kernel booted between that iPXE and ISO fetch (an older boot's pair cannot
    bracket a kernel that booted later)."""
    if kernel_boot is None:
        return None
    ipxes = [t for t in times.get("ipxe") or [] if t >= born_at]
    if not ipxes:
        return None
    ipxe = max(ipxes)
    tftps = [t for t in times.get("tftp") or [] if born_at <= t <= ipxe and ipxe - t <= BOOT_PROOF_BOUND_S]
    isos = [t for t in times.get("iso") or [] if ipxe <= t <= ipxe + BOOT_PROOF_BOUND_S]
    if not tftps or not isos:
        return None
    iso = min(isos)
    if not ipxe <= kernel_boot <= iso:
        return None
    return {"tftp": max(tftps), "ipxe": ipxe, "iso": iso, "kernel": kernel_boot}


IDLE_INTERVAL_S = 15


def interval_s(ladder) -> int:
    """Worker cadence for a step that did something: 2s while a rung is in
    flight, 10s while polling the battery, 15s when idle, faulted or done.

    A step that neither acted nor changed the slice is idle whatever its rung
    (a slot parked at power-on because it is off/latched/held/not allowlisted)
    and the worker uses IDLE_INTERVAL_S directly — see worker.iterate_once."""
    if not ladder or ladder.get("fault"):
        return IDLE_INTERVAL_S
    rung = ladder.get("rung")
    if rung == "qualify":
        return 10
    if rung in ("done", "bmc-pinged"):
        return IDLE_INTERVAL_S
    return 2


def _pass(lad, next_rung, now):
    lad["rung"] = next_rung
    lad["since"] = now


def _over_budget(lad, rung, since, now):
    b = budget_s(rung)
    return b is not None and (now - since) > b


def advance(ladder, snap, evidence, now):
    """One step. Returns (new_ladder, actions). Never mutates its inputs."""
    lad = copy.deepcopy(ladder) if ladder else new_ladder(now)
    ev = evidence or {}
    acts = []

    if lad.get("fault"):
        if snap.get("hold"):
            lad["hold_seen"] = True
        elif lad.get("hold_seen"):
            return new_ladder(now), ["unclaim"]
        return lad, []

    if lad.pop("human_power_on", None):
        acts += ["sol-mark:operator", "claim"]

    rung = lad["rung"]

    if rung == "bmc-pinged":
        if snap.get("bmc_pinged"):
            _pass(lad, "power-on", now)
            acts.append("probe-fwd")
        return lad, acts

    if rung == "power-on":
        power = snap.get("power_on")
        if lad.get("power_on_pending"):
            if power == "on":
                lad["power_on_pending"] = False
                _pass(lad, "tftp-seen", now)
            elif power == "off" and _over_budget(lad, "power-on", lad["power_on_at"], now):
                lad = fault(lad, "power-on", "not on after %ds" % budget_s("power-on"), now)
                acts.append("unclaim")
            elif power is None and _over_budget(lad, "power-unreadable", lad["power_on_at"], now):
                # AMI/onetree BMCs drop off the network for >60 s right after
                # a chassis power-on (et5b4, et6b3 2026-09-12: the chassis WAS
                # on). Only a definite `off` spends the 60 s budget; an
                # unreadable BMC gets the longer cap.
                lad = fault(lad, "power-on", "power unreadable %ds after power-on"
                            % budget_s("power-unreadable"), now)
                acts.append("unclaim")
            return lad, acts
        if power == "on":
            # Already on when first seen (rack in flight, observe restart): the
            # boot markers for this boot are unknowable, so skip them.
            lad["marks"]["skipped"] = "power already on"
            lad["power_on_at"] = lad.get("power_on_at") or now
            # A latched blade that happens to be on is finished, not booting:
            # walking on would ssh the host, fire the firmware probes and then
            # fault at agent-reachable with no agent left to reach.
            _pass(lad, "done" if snap.get("verdict") is not None else "host-pinged", now)
            return lad, acts
        if power != "off":
            return lad, acts
        if snap.get("verdict") is not None or not snap.get("allowed") \
                or snap.get("hold") or snap.get("fw_flashing"):
            return lad, acts
        last_attempt = lad.get("last_power_attempt")
        if last_attempt is not None and now - last_attempt < POWER_COOLDOWN_S:
            return lad, acts
        lad.update(power_on_pending=True, power_on_at=now, last_power_attempt=now)
        return lad, acts + ["sol-mark:power-on", "claim", "power-on"]

    if rung in BOOT_MARKER_RUNGS:
        key = _MARK_KEY[rung]
        ts = ev.get(key)
        if ts:
            lad["marks"][key] = ts
            _pass(lad, _NEXT[rung], now)
            return lad, acts
        since = lad["power_on_at"] if rung == "tftp-seen" else lad["since"]
        if _over_budget(lad, rung, since, now):
            lad = fault(lad, rung, "no %s after %ds" % (key, budget_s(rung)), now)
            acts.append("unclaim")
        return lad, acts

    if rung == "host-leased":
        if snap.get("host_leased"):
            _pass(lad, "live-iso-seen", now)
        elif _over_budget(lad, rung, lad["since"], now):
            lad = fault(lad, rung, "no host lease after %ds" % budget_s(rung), now)
            acts.append("unclaim")
        return lad, acts

    if rung == "host-pinged":
        if ev.get("ping"):
            lad["marks"]["ping"] = now
            _pass(lad, "host-ssh", now)
        elif _over_budget(lad, rung, lad["since"], now):
            lad = fault(lad, rung, "no ping after %ds" % budget_s(rung), now)
            acts.append("unclaim")
        return lad, acts

    if rung == "host-ssh":
        boot = ev.get("ssh-boot")
        boot = boot if isinstance(boot, dict) else None
        if ev.get("ssh") or (boot and boot.get("ok")):
            if boot and lad.get("born_at") is not None:
                proof = _prove_boot(lad["born_at"], boot, boot.get("kernel_boot"))
                if proof:
                    lad["marks"].update(proof)
            lad["marks"]["ssh"] = now
            _pass(lad, "bmc-ready", now)
            acts += ["unclaim", "probe-biosd", "probe-nicd"]
        elif _over_budget(lad, rung, lad["since"], now):
            lad = fault(lad, rung, "no ssh after %ds" % budget_s(rung), now)
            acts.append("unclaim")
        return lad, acts

    if rung == "bmc-ready":
        # Ruling 2026-09-12: no agent and no BMC-side probe until the BMC has
        # proven it answers a real data read (ping alone lies for ~a minute
        # after the chassis power-on). The evidence is the FRU identity the
        # population rules depend on; it is kept on the slice for the modal.
        data = ev.get("bmcdata")
        if data:
            lad["marks"]["bmcready"] = now
            lad["bmc_fru"] = dict(data) if isinstance(data, dict) else {}
            _pass(lad, "fw-gates", now)
            acts.append("probe-fwd")            # refresh the BMC firmware row now that it answers
            return lad, acts
        # Ruling 2026-09-14: an expired wait restarts with the next retry budget
        # (et25b2 faulted at 600 s and answered minutes later); only the last
        # expiry faults.
        attempts = list(lad.get("bmc_ready_attempts") or [])
        if now - lad["since"] > bmc_ready_budget_s(len(attempts)):
            waited = now - lad["since"]
            if len(attempts) < len(bmc_ready_retry_budgets()):
                attempts.append({"at": now, "waited_s": round(waited, 1)})
                lad["bmc_ready_attempts"] = attempts
                lad["since"] = now
            else:
                total = sum(a.get("waited_s") or 0 for a in attempts) + waited
                lad = fault(lad, rung, "BMC not answering data reads after %d attempts (%ds)"
                            % (len(attempts) + 1, total), now)
        return lad, acts

    if rung == "fw-gates":
        if snap.get("fw_gates"):
            # The gate passing IS the firmware stage completing: freeze the
            # record's Firmware step map here (the worker puts it in the
            # snapshot) so a later daemon write — fwd's `unreachable` during
            # the post-power-on BMC blackout (et28b4, et6b4 2026-09-12) —
            # cannot regress what the verdict snapshot later latches.
            if snap.get("fw_steps"):
                lad["fw_steps"] = dict(snap["fw_steps"])
            _pass(lad, "agent-reachable", now)
            acts.append("poll-agent")
        elif _over_budget(lad, rung, lad["since"], now):
            lad = fault(lad, rung, "firmware gate not passed after %ds" % budget_s(rung), now)
        return lad, acts

    if rung == "agent-reachable":
        if ev.get("agent"):
            _pass(lad, "qualify", now)
            return lad, acts + ["poll-agent"]
        # A slot outside the allowlist is observed only: nobody launches its
        # agent, so the 180 s budget has nothing to measure (et28b3 faulted
        # this way on the first deploy, 2026-09-11). Keep polling, never fault.
        if not snap.get("allowed"):
            # Keep the rung's clock at "now": when the allowlist later opens,
            # the 180 s budget must start from that moment, not from when the
            # rung was first entered (six blades faulted in the same step their
            # agent was launched, 2026-09-12).
            lad["since"] = now
            return lad, acts + ["poll-agent"]
        # launch_at is never cleared: a row that reaches this rung without a
        # fresh launch (the non-allowlisted slot, or a re-run) carries the
        # launch of a previous boot. Only a launch at or after this rung
        # started moves the clock; otherwise the rung's own `since` governs.
        launch_at = snap.get("launch_at")
        since = launch_at if (launch_at is not None and launch_at >= lad["since"]) else lad["since"]
        if _over_budget(lad, rung, since, now):
            lad = fault(lad, rung, "agent not reachable %ds after launch" % budget_s(rung), now)
            return lad, acts
        return lad, acts + ["poll-agent"]

    if rung == "qualify":
        # The worker polls the agent as this rung's evidence every step.
        verdict = snap.get("verdict")
        if verdict is None:
            return lad, acts
        if verdict == "pass" or snap.get("battery_done"):
            _pass(lad, "done", now)
            return lad, acts
        # fail with the battery still running (ruling 2026-09-12): keep
        # collecting until it is terminal, or the agent is gone for good
        if ev.get("agent"):
            lad.pop("agent_lost_at", None)
            return lad, acts
        lost = lad.get("agent_lost_at")
        if lost is None:
            lad["agent_lost_at"] = now
        elif now - lost > POST_VERDICT_AGENT_GRACE_S:
            lad["collection"] = "cut after the verdict: agent unreachable for %ds (node or BMC reboot)" % POST_VERDICT_AGENT_GRACE_S
            _pass(lad, "done", now)
        return lad, acts

    return lad, acts          # done
