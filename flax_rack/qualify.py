"""Qualify stage for a rack order (spec section 7), run in lockstep phases.

The integrated rack is kept in step: five rack-wide phases, each started on every
still-eligible device at once, with a barrier (wait for all) before the next:

  boot       pxe-live (identity checked)                       BOOT_DEADLINE_S
  inventory  agent: sdr-pre..inventory; then population-check,  INVENTORY_DEADLINE_S
             smbios-serial, tpm-state
  storage    agent: fio                                        120 s + 120 s x disks
  burn       agent: mem-pre, cpu-mem-stress .. sel-post        FLAX_STRESS_SECONDS + 900 s
  network    rack: lldp-<wedge>; per device ha-path, fabric-addr, iperf-mesh

Each agent phase relaunches the post.tgz agent with that phase's FLAX_QUAL_STAGES
(agent.launch stops an agent running another stage list and starts it fresh), all
devices in one launch wave, then polls them in parallel; each battery stage is
recorded as its own rung as soon as its device finishes (artifacts under
artifacts/<pos>/<agent run>/<stage>/), so a slow device delays only the barrier.
agent-reachable (the launch) and agent-stages are recorded once per agent phase
(detail.phase).
The storage deadline counts the disks fio will read in the inventory's lsblk
capture (fio_disks; 8 when there is none). On a deadline the agent is stopped, so
nothing still runs when the next phase starts; a node whose stop failed is named.

Step names are loose-node Post's (flax_post.blades PHASE_STEPS["Qualify"]) where the
meaning is the same, and the judges are Post's own code: population-check is
flax_post.qualify_rules.population_check (profile + DIMM blocklist + no blank FRU 0
ship serial), smbios-serial / tpm-state are flax_post.shipchecks on the inventory's
ipmitool-fru / dmidecode / tpmstate captures (evidence stored as their `check`
artifact), and a device's verdict is qualify_rules.qualify_verdict fed the battery,
the population and the ship checks. Post's iperf (to the bang) is this rack's
ha-path, fabric-addr, iperf-mesh; pxe-live is rackpost's own. (Firmware versions are
the Firmware stage's; the old Qualify `fw-versions` read is gone -- its order.db rows
stay, harmless, and are never shown.)
`console` is the node's SOL captured (flax_rungs.sol) from just before its live boot
until it leaves Qualify, saved as artifacts/<pos>/q<run>/console/sol.txt.

A device failing a phase (a `fail` or `unmeasured` on any of that phase's rungs;
population-check and the ship checks never gate -- their fail still fails the device in
qualify_passed) gets `skip` ("failed in phase <p>", or "not measured in phase <p>" when
its target stopped answering) on every rung of the later phases; the rest of the rack
carries on. `unmeasured` (flax_rungs.result) = the target did not answer, nothing was
judged: pxe-live with no IPMI answer, agent-stages when the agent never answered,
population-check with no FRU 0 from the BMC, ha-path with no wedge's LLDP readable,
fabric-addr with the host gone from ssh, iperf-mesh when its server is not ready. It
never passes: qualify_passed names it "not measured: <rung>". Identity first: a device is
only booted when its latest Discover bmc-reserved, bmc-leased, bmc-pinged and
host-reserved passed and order.db holds its host MAC (else pxe-live fails "discover
first"); pxe-live then requires the live image's BOOTIF MAC to be that host MAC.
No reboot when Discover's host-ssh proved this boot of the live image and a re-read
shows the same boot_id and BOOTIF (pxe-live passes "reboot skipped"); else it boots.
population-check's FRU 0 is the out-of-band `ipmitool fru print 0` (identity.tp_fru,
Post's IPMI lane; family-aware via fru.read_baseboard), read at the inventory phase:
a failed out-of-band read fails the check -- never backfilled from the in-band capture.

`positions` re-runs every phase for those devices only; the network phase adds this
rack's iperf server (geometry.iperf_server; its fabric addresses do not survive a
reboot) when it passed every earlier phase. If the server is not ready every client
fails "server <pos> not ready". The current phase and barrier count are kept in
order.db (qualify_progress) for the UI and cleared when the stage ends.

The rack's Cancel flag (stages.cancelled) is checked before each device's boot and at
every phase barrier: the devices still in are out with `skip` "cancelled" (a boot or
agent poll already running finishes; the network phase does not start).

flax_rungs functions never touch order.db; this module records them. ctx.rungs
(a namespace like real_rungs()) replaces them in tests and in RACKPOST_FAKE.
"""
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from types import SimpleNamespace

from flax_post import population, qualify_rules, shipchecks
from flax_post.observe import family_map_live, fru
from flax_rungs import agent, fabric, ha, hostssh, identity, iperf, liveboot, sol, sonic
from flax_rungs.result import UNMEASURED, RungResult, not_passed
from flax_rungs.sonic import LldpEntry

from .addressing import plan_addresses
from .discover_ladder import DISCOVER_STEPS
from .stages import _rung, cancelled, creds
from .version import __version__

STAGE = "qualify"
AGENT_STAGES = tuple(agent.ORDER_STAGES.split(","))
PHASES = ("boot", "inventory", "storage", "burn", "network")
PHASE_STAGES = {    # the agent's FLAX_QUAL_STAGES per phase; together = ORDER_STAGES
    "inventory": ("sdr-pre", "sel-pre", "sel-clear", "tooling", "inventory"),
    "storage": ("fio",),
    "burn": ("mem-pre", "cpu-mem-stress", "mem-post", "sdr-post", "sel-post"),
}
NET_RUNGS = ("ha-path", "fabric-addr", "iperf-mesh")
# judged by the engine from the inventory capture (Post's order: after fio); never gate
ENGINE_CHECKS = ("population-check", *shipchecks.SHIP_CHECKS)
NON_GATING = (*ENGINE_CHECKS, "console")
# order.db rows written before Post's names were adopted: shown under the new name
OLD_RUNG_NAMES = {"pop": "population-check", "agent-launch": "agent-reachable"}
MAX_PARALLEL = 8            # concurrent live boots (BMC power actions) in the boot phase
BOOT_DEADLINE_S = 900
INVENTORY_DEADLINE_S = 1200
STORAGE_BASE_S, STORAGE_PER_DISK_S = 120, 120
STORAGE_UNKNOWN_DISKS = 8   # no lsblk capture: assume this many
BURN_SLACK_S = 900          # over FLAX_STRESS_SECONDS: mem-pre/post, sdr/sel reads
_SEG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")
_FIO_DEV = re.compile(r"^(nvme\d+n\d+|sd[a-z]+)$")    # = qual_battery._fio's device rule


def real_rungs():
    return SimpleNamespace(
        boot_live=liveboot.boot_live, launch=agent.launch, poll=agent.poll,
        stop_agent=agent.stop,
        read_lldp=sonic.read_lldp, apply_fabric=fabric.apply, mesh=iperf.mesh,
        host_run=hostssh.run, sol_start=sol.start, sol_stop=sol.stop,
        tp_fru=identity.tp_fru)


def pop_default(g, d) -> str:
    """The BOM default: geometry `pop` if set, else <customer>-<kind> (f4-compute-tp)."""
    return d.pop or f"{g.customer.lower()}-{d.kind}"


def device_rungs(d) -> list[str]:
    """The Qualify rungs a device must pass, in Post's step order ([] = no host)."""
    if not d.endpoints.get("hosts"):
        return []
    out = ["pxe-live", "agent-reachable", "agent-stages",
           *PHASE_STAGES["inventory"], *PHASE_STAGES["storage"], *ENGINE_CHECKS,
           *PHASE_STAGES["burn"]]
    if any(n.startswith("pcie") for n in d.links):
        out += ["ha-path", "fabric-addr"]
    return out + ["iperf-mesh", "console"]


def _stage_ok(run) -> bool:
    """A battery stage counts as passed on `pass`, or on a skip the agent itself
    reported (fio's "no physical storage media": its detail names the stage) -- never
    on rackpost's own "failed in phase" skip."""
    run = run or {}
    return run.get("verdict") == "pass" or (
        run.get("verdict") == "skip" and bool((run.get("detail") or {}).get("stage")))


def device_verdict(lr, d) -> str | None:
    """qualify_rules.qualify_verdict fed what Post feeds it: the battery's verdict, the
    population-check result and the ship checks, from this device's latest runs."""
    run = lambda r: lr.get((d.pos, r)) or {}
    stages = [run(n) for n in AGENT_STAGES]
    node = ("fail" if any(r.get("verdict") == "fail" for r in stages)
            else "pass" if all(_stage_ok(r) for r in stages) else None)
    pop = {"verdict": (run("population-check").get("detail") or {}).get("verdict")}
    checks = {n: {"status": run(n).get("verdict") if run(n).get("status") == "done" else None}
              for n in shipchecks.SHIP_CHECKS}
    return qualify_rules.qualify_verdict({"verdict": node}, pop, checks)


def qualify_passed(db, g) -> tuple[bool, list[str]]:
    """(rack passed?, positions not passed). A host device passes when Post's verdict
    (device_verdict) is pass and every rackpost rung (pxe-live, agent plumbing, the
    network rungs, console) last finished `pass`; an IOM-only device (no host) counts
    via its Discover ladder (bmc-pinged and serial passed, no step failed). An
    `unmeasured` rung is never a pass (qualify_why names it)."""
    why = qualify_why(db.latest_runs(), g)
    bad = [d.pos for d in g.devices if d.pos in why]
    return not bad, bad


def qualify_why(lr, g) -> dict[str, str]:
    """pos -> why it has not passed Qualify (absent = passed): its first rung (in step
    order) that is not passed, as result.not_passed words it ("not measured: <rung>"
    for an unmeasured one); Post's verdict when every rung looks passed but it is not."""
    out = {}
    for d in g.devices:
        need = device_rungs(d)
        run = lambda r: lr.get((d.pos, r)) or {}    # noqa: E731
        if not need:
            bad = [s for s in DISCOVER_STEPS
                   if run(s).get("verdict") not in ("pass", "skip")
                   or (s in ("bmc-pinged", "serial") and run(s).get("verdict") != "pass")]
        else:
            rest = [r for r in need if r not in AGENT_STAGES and r not in ENGINE_CHECKS]
            if device_verdict(lr, d) == "pass" and all(run(r).get("verdict") == "pass"
                                                       for r in rest):
                continue
            bad = [r for r in need if not (_stage_ok(run(r)) if r in AGENT_STAGES
                                           else run(r).get("verdict") == "pass")]
            if not bad:
                out[d.pos] = f"verdict {device_verdict(lr, d) or 'none'}"
                continue
        if bad:
            out[d.pos] = not_passed(bad[0], run(bad[0]).get("verdict"))
    return out


def stress_seconds() -> int:
    """The agent's cpu-mem-stress duration (qual_battery reads the same variable; the
    rendered flax-qual.env does not set it, so both default to 600)."""
    try:
        return int(os.environ.get("FLAX_STRESS_SECONDS") or 600)
    except ValueError:
        return 600


def fio_disks(lsblk: str | None) -> int | None:
    """How many disks the storage phase's fio reads, from the inventory's plain `lsblk`
    capture: a top-level nvme*/sd* disk with no mountpoint of its own -- the rule
    qual_battery._fio applies to `lsblk -ndo NAME,MOUNTPOINT` (child rows carry tree
    glyphs, so they never match). None = no usable capture."""
    lines = (lsblk or "").splitlines()
    if not lines or not lines[0].lstrip().startswith("NAME"):
        return None
    n = 0
    for line in lines[1:]:
        f = line.split()        # NAME MAJ:MIN RM SIZE RO TYPE [MOUNTPOINTS]
        if f and _FIO_DEV.match(f[0]) and len(f) < 7:
            n += 1
    return n


def storage_deadline(lsblk: str | None) -> tuple[int, dict]:
    n = fio_disks(lsblk)
    det = {"disks": n, "disks_from": "lsblk"}
    if n is None:
        n = STORAGE_UNKNOWN_DISKS
        det = {"disks": n, "disks_from": f"unknown (no lsblk capture; assumed {n})"}
    return STORAGE_BASE_S + STORAGE_PER_DISK_S * n, det


def _safe(name) -> bool:
    return isinstance(name, str) and bool(_SEG.fullmatch(name)) and ".." not in name


def _record(ctx, pos, rung, res: RungResult, stage=STAGE, run_seg=None,
            seg_prefix="q") -> RungResult:
    """One finished run row for a result obtained elsewhere; its artifacts (text) are
    written under artifacts/<pos>/<run_seg or seg_prefix+row id>/<rung>/ and the row
    keeps their paths."""
    rid = ctx.db.start_run(pos, stage, rung)
    try:
        arts = res.artifacts or {}
        detail = dict(res.detail)
        rel = {}
        if arts:
            keep = {n.removesuffix(".txt"): t for n, t in arts.items()
                    if _safe(n.removesuffix(".txt")) and isinstance(t, str)}
            if len(keep) < len(arts):       # an agent-chosen name never picks a path
                detail["dropped_artifacts"] = sorted(n for n in arts
                                                     if n.removesuffix(".txt") not in keep)
            seg = run_seg if _safe(run_seg) else f"{seg_prefix}{rid}"
            rel = dict(zip(keep, agent.save_artifacts(ctx.dir, pos, seg, rung, keep)))
        r = RungResult(res.verdict, detail, rel)
    except Exception as e:                  # noqa: BLE001 -- a row never stays 'running'
        r = RungResult("fail", {"error": f"{type(e).__name__}: {e}"})
    ctx.db.finish_run(rid, r)
    return r


def _skip(ctx, pos, rungs, why, stage=STAGE):
    for rung in rungs:
        _record(ctx, pos, rung, RungResult("skip", {"reason": why}), stage)


def _artifact(ctx, lr, pos, rung, name) -> str | None:
    rel = ((lr.get((pos, rung)) or {}).get("artifacts") or {}).get(name)
    if not rel:
        return None
    try:
        return (ctx.dir / rel).read_text()
    except OSError:
        return None


# ---------------------------------------------------------------- parsers
_LINK_HDR = re.compile(r"^\d+:\s+([^:@\s]+)")


def parse_ip_link(text: str) -> dict[str, str]:
    """ifname -> MAC from `ip -d link` (link/ether on the next line) or `ip -o link`."""
    out, cur = {}, None
    for line in text.splitlines():
        m = _LINK_HDR.match(line)
        if m:
            cur = m.group(1)
        e = re.search(r"link/ether\s+((?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})", line)
        if e and cur and cur not in out:
            out[cur] = e.group(1).lower()
    return out


def iface_of(text: str, ip: str) -> str | None:
    """The interface holding `ip` in `ip -d address` / `ip -o -4 addr show` output."""
    cur = None
    for line in text.splitlines():
        m = _LINK_HDR.match(line)
        if m:
            cur = m.group(1)
        if re.search(rf"\binet {re.escape(ip)}/", line):
            return cur
    return None


# ---------------------------------------------------------------- per-device rungs
def _pop_label(rule: str) -> str:
    """stepinfo's labels for a failed population rule."""
    return ("BLOCKED" if rule.startswith("blocked DIMM")
            else "NO SHIP" if rule.startswith("no ship serial") else "missing")


def baseboard_of(fru_text: str | None) -> dict | None:
    """Post's FRU 0 reading (fru.read_baseboard, deployed family map) of an
    `ipmitool fru` text; None without one."""
    if not fru_text or not fru_text.strip():
        return None
    return fru.read_baseboard(fru_text, family_map_live.current())


def oob_fru0(R, cr, bmc_ip, *, runner=None) -> tuple[str | None, str | None]:
    """(FRU 0 text, None) from the out-of-band read (identity.tp_fru: blank serial or
    'not present' still return the text), or (None, why) when the BMC gave none."""
    if not bmc_ip:
        return None, "no BMC endpoint"
    r = R.tp_fru(bmc_ip, cr.bmc, runner=runner)
    text = (r.artifacts or {}).get("fru0.txt")
    return (text, None) if text else (None, r.detail.get("error") or "FRU 0 not read")


def population_rung(ctx, d, arts: dict, fru0=(None, "not read")) -> RungResult:
    """population-check: Post's qualify_rules.population_check on the inventory's macinv
    (count form), dimmsum and the out-of-band FRU 0 (`fru0` = oob_fru0()). Rackpost never
    passes on nothing (spec A4.6): a missing or empty profile, no macinv, or a FRU 0 the
    BMC reports not present is a fail before the judge runs; no FRU 0 text at all (the
    BMC gave no IPMI answer) is `unmeasured` -- nothing judged, never a pass, and never
    backfilled from the in-band capture (Post's no-backfill rule). The FRU 0 text read
    is the check's `fru0` artifact."""
    ovr = next((x["pop_override"] for x in ctx.db.devices() if x["pos"] == d.pos), None)
    p = ovr or pop_default(ctx.g, d)
    path = os.path.join(population.PROFILE_DIR, p)
    head = {"profile": p, "verdict": "grey"}
    if "/" in p or not os.path.isfile(path):
        return RungResult("fail", {**head, "error": f"no profile {p}"})
    rules = population.load_profile(p)
    if not rules:
        return RungResult("fail", {**head, "error": f"profile {p} has no rules"})
    macinv = arts.get("macinv")
    if not macinv or not macinv.strip():
        return RungResult("fail", {**head, "error": "no macinv from the inventory stage"})
    fru_text, fru_err = fru0
    fart = {"fru0": fru_text} if fru_text else {}
    bb = baseboard_of(fru_text)
    if bb is None or bb["state"] == "absent":
        why = (f"FRU 0 not read out of band: {fru_err}" if bb is None
               else f"FRU 0 not present: {bb.get('reason') or 'no FRU 0'}")
        return RungResult("fail" if bb else UNMEASURED,
                          {**head, "error": why, "headline": why, "reason": why,
                           "rows": [["profile", p], ["FRU 0", why]],
                           "notes": ["FRU 0 comes from the BMC (`ipmitool fru print 0`, "
                                     "Post's IPMI lane); it is never taken from the "
                                     "in-band capture"]}, fart)
    pop = qualify_rules.population_check(macinv, p, arts.get("dimmsum"), bb)
    ev = population.evaluate(rules, macinv)
    failed = pop.get("failed_rules") or []
    profile_rules = {r["rule"] for r in ev["results"]}
    detail = {**pop, "results": ev["results"],
              "extra": [r for r in failed if r not in profile_rules],
              "rows": [["profile", p], ["verdict", pop["verdict"]]]
              + [[_pop_label(r), r] for r in failed]
              + [[f"FRU 0 serial ({bb.get('serial_field')})", bb.get("serial") or "empty"]],
              "notes": ["rules are matched against this run's inventory digest (macinv count "
                        "form); the POP button shows the full rule list",
                        "FRU 0 read out of band from the BMC (`ipmitool fru print 0`)"]}
    if pop.get("blocked_dimms"):
        detail["notes"].insert(0, f"a blocked part is installed: {len(pop['blocked_dimms'])} "
                                  "DIMM(s) match a known-bad SPD identity; the node must not ship")
    if pop.get("no_ship_serial"):
        detail["notes"].insert(0, "no ship serial in FRU 0: the node must not ship")
    if pop["verdict"] == "green":
        detail["headline"] = f"population green: every {p} rule found"
        return RungResult("pass", detail, fart)
    miss = sum(1 for r in ev["results"] if not r["ok"])
    why = ([f"{miss} of {len(ev['results'])} {p} rule(s) not found"] if miss else []) + \
        ([f"{len(pop['blocked_dimms'])} blocked DIMM(s)"] if pop.get("blocked_dimms") else []) + \
        (["no ship serial in FRU 0"] if pop.get("no_ship_serial") else [])
    detail["error"] = detail["headline"] = "; ".join(why) or "population red"
    return RungResult("fail", detail, fart)


def ship_check_rung(name: str, arts: dict) -> RungResult:
    """smbios-serial / tpm-state: flax_post.shipchecks on the inventory's captures, its
    evidence_text stored as the rung's `check` artifact (Post's record of the state it
    saw). A missing capture fails, never passes or skips."""
    if name == "smbios-serial":
        c = shipchecks.smbios_serial(arts.get("ipmitool-fru"), arts.get("dmidecode"))
        rows = [["FRU 0 serial", ", ".join(c["fru0_serials"]) or "none"]] + \
            [[f"SMBIOS {k}", v or "none"] for k, v in c["dmi_serials"].items()]
    else:
        c = shipchecks.tpm_state(arts.get("tpmstate"))
        dev = c["device"]
        rows = [["TPM device", "present" if dev else "absent" if dev is False else "unknown"],
                ["version", c["version"] or "unknown"], ["state line", c["raw"] or "none"]]
    c["checker"] = "rackpost " + __version__
    detail = {**c, "headline": c["reason"] or f"{name} ok",
              "rows": rows + ([["reason", c["reason"]]] if c["reason"] else [])
              + [["checked by", c["checker"]]], "notes": []}
    if c["status"] != "pass":
        detail["error"] = c["reason"]
    return RungResult(c["status"], detail, {"check": shipchecks.evidence_text(name, c)})


GATE_STEPS = ("bmc-reserved", "bmc-leased", "bmc-pinged", "host-reserved")


def discover_gate(lr, dev_row) -> str | None:
    """Why this device may not be booted yet (None = Discover proved it): its latest
    GATE_STEPS (the planned BMC IP is reserved for AND leased by the discovered BMC MAC,
    and answers; the host reservation exists) must be pass and order.db must hold its
    host MAC. A row from before Plan 2b (reserve/bmc-reach) does not count."""
    v = {r: (lr.get((dev_row["pos"], r)) or {}).get("verdict") for r in GATE_STEPS}
    bad = [not_passed(r, v[r]) for r in v if v[r] != "pass"]
    if not bad and not dev_row.get("host_mac"):
        bad.append("no host MAC matched")
    return ("discover first: " + ", ".join(bad)) if bad else None



def reboot_skip(ctx, R, cr, lr, x) -> tuple[RungResult | None, str]:
    """(pxe-live pass, why) when Discover's host-ssh proved this boot of the live image
    and a re-read shows the same boot_id with BOOTIF = the discovered host MAC: no
    reboot. Else (None, why it boots)."""
    hs = lr.get((x.pos, "host-ssh")) or {}
    bid = (hs.get("detail") or {}).get("boot_id")
    if hs.get("verdict") != "pass" or not bid:
        return None, "Discover's host-ssh did not prove a live boot"
    if not cr.host:
        return None, "no host credentials to re-read the boot"
    host_ip = x.ips.get("host")
    rc, out, _err = R.host_run(host_ip, *cr.host, liveboot._PROBE, timeout=20,
                               runner=ctx.runner)
    if rc != 0:
        return None, f"live image not answering ssh (rc={rc})"
    now_bid, up, cmdline = liveboot.parse_probe(out)
    got, want = liveboot.bootif_mac(cmdline), liveboot._hex(x.row.get("host_mac"))
    if now_bid != bid:
        return None, f"boot_id changed since Discover ({bid} -> {now_bid or 'unreadable'})"
    if "pxeaddr=" not in cmdline or not got or got != want:
        return None, "not the discovered host's live image any more"
    mac = liveboot._colon(got)
    return RungResult("pass", {
        "reboot": "skipped", "boot_id": bid, "uptime_s": int(up) if up is not None else None,
        "bootif": mac, "cmdline_ok": True, "override": None, "power_action": None,
        "headline": "reboot skipped: still the live boot Discover proved",
        "rows": [["boot_id", bid], ["BOOTIF", mac], ["uptime", f"{int(up)} s" if up else "?"],
                 ["Discover host-ssh", hs.get("finished_at")]],
        "notes": ["Discover's host-ssh proved this boot of the live image and the boot_id "
                  "read again now is unchanged: no power cycle"]}), ""


# ---------------------------------------------------------------- phase plumbing
@dataclass
class _Dev:
    """One target device's walk through the phases."""
    d: object
    ips: dict
    row: dict
    failed: str | None = None       # the phase it failed in (later phases skip it)
    unmeasured: bool = False        # ... because a rung there was not measured (no fail)
    hard: bool = False              # a rung of that phase failed outright
    busy: bool = False              # an agent stop failed: its battery may still run
    lsblk: str | None = None        # the inventory's lsblk capture (storage deadline)
    sol: object = None              # the running SOL capture (console rung)
    sol_rid: int | None = None      # its order.db run row, 'running' until stopped
    sol_done: bool = False          # console recorded for this Qualify

    @property
    def pos(self) -> str:
        return self.d.pos


class _Progress:
    """The current phase and its barrier, kept in order.db for the UI (best effort).
    Deploy passes its own phases and order.db setter."""

    def __init__(self, ctx, phases=PHASES, save=None):
        self.ctx, self.mu, self.cur = ctx, threading.Lock(), None
        self.phases = phases
        self.save = save or ctx.db.set_qualify_progress

    def start(self, phase, poss):
        with self.mu:
            self.cur = {"phase": phase, "n": self.phases.index(phase) + 1, "of": len(self.phases),
                        "devices": len(poss), "done": 0, "waiting_on": sorted(poss)}
            self._save()

    def done(self, pos):
        with self.mu:
            if self.cur and pos in self.cur["waiting_on"]:
                self.cur["waiting_on"].remove(pos)
                self.cur["done"] += 1
                self._save()

    def clear(self):
        with self.mu:
            self.cur = None
            self._save()

    def _save(self):
        try:
            self.save(self.cur)
        except Exception:                   # noqa: BLE001 -- a view, never the job
            pass


def _phase_rungs(phase, d) -> list[tuple[str, str]]:
    """(rung, stage) pairs `phase` records for device d: what a device that failed an
    earlier phase gets `skip` on. agent-reachable/agent-stages belong to the first agent
    phase; a later agent phase re-records them only for a device that runs it."""
    if phase == "boot":
        return [("pxe-live", STAGE)]
    if phase == "inventory":
        return [(r, STAGE) for r in ("agent-reachable", "agent-stages",
                                     *PHASE_STAGES["inventory"], *ENGINE_CHECKS)]
    if phase in PHASE_STAGES:
        return [(r, STAGE) for r in PHASE_STAGES[phase]]
    return [(r, STAGE) for r in NET_RUNGS if r in device_rungs(d)]


def _why_out(x) -> str:
    if x.busy:
        return "qual agent still running (stop failed)"
    if x.failed == "cancelled":
        return "cancelled"
    return f"{'not measured' if x.unmeasured else 'failed'} in phase {x.failed}"


def _cancel_out(ctx, xs) -> bool:
    """Cancel pressed: every device still in is out ("cancelled")."""
    if not cancelled(ctx):
        return False
    for x in xs:
        x.failed = x.failed or "cancelled"
    return True


def _skip_phase(ctx, x, phase):
    why = _why_out(x)
    for rung, stage in _phase_rungs(phase, x.d):
        _record(ctx, x.pos, rung, RungResult("skip", {"reason": why}), stage)


def _wave(ctx, phase, xs, fn, workers, prog=None):
    """fn(x) for every x at once (at most `workers` at a time); returns when all are
    done. A device whose step raised is out of the phase; the rack carries on."""
    if not xs:
        return

    def one(x):
        try:
            fn(x)
        except Exception as e:              # noqa: BLE001 -- recording itself failed
            x.failed = x.failed or phase
            ctx.db.add_event("error", f"qualify {x.pos} {phase}: {type(e).__name__}: {e}")
        finally:
            if prog is not None:
                prog.done(x.pos)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(xs))),
                            thread_name_prefix=f"qualify-{ctx.g.url_prefix}-{phase}") as ex:
        list(ex.map(one, xs))


def _out(x, phase, r: RungResult):
    """x is out of the phase on r (any verdict but pass); unmeasured says why."""
    x.failed = x.failed or phase
    if x.failed != phase:
        return
    x.hard = x.hard or r.verdict != UNMEASURED
    x.unmeasured = not x.hard


def _phase_end(ctx, phase, xs):
    ran = [x for x in xs if x.failed is None or x.failed == phase]
    bad = [x.pos for x in ran if x.failed == phase and not x.unmeasured]
    unm = [x.pos for x in ran if x.failed == phase and x.unmeasured]
    ctx.db.add_event("qualify", f"phase {phase}: {len(ran) - len(bad) - len(unm)} passed"
                                + (f", failed: {', '.join(bad)}" if bad else "")
                                + (f", not measured: {', '.join(unm)}" if unm else ""))


def _with(r: RungResult, **extra) -> RungResult:
    return RungResult(r.verdict, {**r.detail, **extra}, r.artifacts)


# ---------------------------------------------------------------- boot phase
def _boot(ctx, R, cr, lr, x):
    pos, d = x.pos, x.d
    if _cancel_out(ctx, [x]):               # not booted yet: never start a power action
        _record(ctx, pos, "pxe-live", RungResult("skip", {"reason": "cancelled"}))
        return
    why = discover_gate(lr, x.row)
    host_ip, bmc_ip = x.ips.get("host"), x.ips.get("bmc")
    if why:                                 # never power-cycle an unproven BMC IP
        r = _record(ctx, pos, "pxe-live", RungResult("fail", {"error": why}))
    elif d.endpoints["hosts"] > 1:
        r = _record(ctx, pos, "pxe-live", RungResult("fail", {
            "error": f"{d.endpoints['hosts']}-host devices are not qualified yet "
                     "(no per-card live boot)"}))
    elif not bmc_ip:
        r = _record(ctx, pos, "pxe-live", RungResult("fail", {"error": "no BMC to boot it with"}))
    else:
        _console_start(ctx, R, cr, x)       # capture the boot itself
        try:
            skip, why = reboot_skip(ctx, R, cr, lr, x)
        except Exception as e:              # noqa: BLE001 -- unproven: boot as before
            skip, why = None, f"re-read failed: {type(e).__name__}: {e}"
        if skip is not None:
            r = _record(ctx, pos, "pxe-live", skip)
        else:
            r = _rung(ctx, pos, "pxe-live", lambda: _with(
                R.boot_live(bmc_ip, host_ip, cr, expect_mac=x.row["host_mac"],
                            deadline_s=BOOT_DEADLINE_S, runner=ctx.runner), rebooted_because=why),
                STAGE)
    if r.verdict != "pass":
        _out(x, "boot", r)


# ---------------------------------------------------------------- console (SOL)
def _sol_rel(pos, rid) -> str:
    return f"artifacts/{pos}/q{rid}/console/sol.txt"


def _console_start(ctx, R, cr, x):
    """Open the console row ('running' for the UI) and start the SOL capture into the
    order dir. A capture that cannot start is recorded failed at once; it never stops
    the boot (console does not gate, as in Post)."""
    if not _safe(x.pos):
        return
    x.sol_rid = ctx.db.start_run(x.pos, STAGE, "console")
    try:
        x.sol = R.sol_start(x.ips["bmc"], cr.bmc, ctx.dir / _sol_rel(x.pos, x.sol_rid),
                            runner=ctx.runner)
    except Exception as e:                  # noqa: BLE001
        x.sol = RungResult("fail", {"error": f"sol start: {type(e).__name__}: {e}"})


def _console_stop(ctx, R, x):
    """Stop the capture (sol deactivate, always) and finish the console row: pass when
    the stored capture holds bytes."""
    if x.sol_rid is None:
        return
    rid, cap = x.sol_rid, x.sol
    x.sol_rid, x.sol, x.sol_done = None, None, True
    try:
        res = cap if isinstance(cap, RungResult) else R.sol_stop(cap)
    except Exception as e:                  # noqa: BLE001 -- the row never stays running
        res = RungResult("fail", {"error": f"sol stop: {type(e).__name__}: {e}"})
    rel = _sol_rel(x.pos, rid)
    try:
        n = (ctx.dir / rel).stat().st_size
    except OSError:
        n = 0
    det = dict(res.detail)
    det.update(headline=(f"SOL captured: {n} bytes" if res.verdict == "pass"
                         else det.get("error") or "no sol capture"),
               rows=[["capture", "sol.txt" if n else "none"], ["bytes", str(n)],
                     ["cipher", str(det.get("cipher") or "")],
                     ["session drops", str(det.get("drops") or 0)]],
               notes=["SOL from just before the live boot until the node leaves Qualify; "
                      "the session is deactivated at the end"])
    ctx.db.finish_run(rid, RungResult(res.verdict, det, {"sol": rel} if n else {}))


def _console_end(ctx, R, xs):
    """Every device's console is recorded once: stopped if running, else skipped (it
    never booted) with the reason it is out."""
    for x in xs:
        try:
            if x.sol_rid is not None:
                _console_stop(ctx, R, x)
            elif not x.sol_done:
                x.sol_done = True
                _record(ctx, x.pos, "console", RungResult("skip", {
                    "reason": _why_out(x) if x.failed else "no sol capture started"}))
        except Exception as e:              # noqa: BLE001
            ctx.db.add_event("error", f"qualify {x.pos} console: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- agent phases
def _deadline(phase, x) -> tuple[int, dict]:
    if phase == "storage":
        dl, det = storage_deadline(x.lsblk)
        return dl, {"deadline_by": f"{STORAGE_BASE_S} + {STORAGE_PER_DISK_S} x disks", **det}
    if phase == "burn":
        s = stress_seconds()
        return s + BURN_SLACK_S, {"deadline_by": f"FLAX_STRESS_SECONDS {s} + {BURN_SLACK_S}"}
    return INVENTORY_DEADLINE_S, {"deadline_by": "inventory phase"}


def _agent_phase(ctx, R, cr, prog, phase, xs):
    """Launch the agent with this phase's stages on every device in one wave, then poll
    them all; each device's results are recorded the moment it finishes."""
    prog.start(phase, [x.pos for x in xs])
    if not xs:
        return
    want = PHASE_STAGES[phase]
    stages = ",".join(want)
    user, pw = cr.host
    started: dict[str, float] = {}
    mu = threading.Lock()

    def launch(x):
        r = _rung(ctx, x.pos, "agent-reachable", lambda: _with(
            R.launch(x.ips["host"], user, pw, stages=stages, runner=ctx.runner), phase=phase),
            STAGE)
        if r.verdict == "pass":
            with mu:
                started[x.pos] = time.monotonic()
            return
        x.failed = phase
        _skip(ctx, x.pos, ["agent-stages", *want]
              + (list(ENGINE_CHECKS) if phase == "inventory" else []), "agent-reachable failed")
        prog.done(x.pos)

    _wave(ctx, phase, xs, launch, len(xs))
    for x in xs:
        if x.failed == phase:
            prog.done(x.pos)                # a launch that raised
    if phase == "burn" and started:
        order = sorted(started, key=started.get)
        spread = started[order[-1]] - started[order[0]]
        ctx.db.add_event("qualify", f"burn: stress launched on {len(order)} node(s) within "
                                    f"{spread:.1f} s ({order[0]} first, {order[-1]} last)")
    _wave(ctx, phase, [x for x in xs if x.pos in started],
          lambda x: _poll(ctx, R, cr, phase, x), len(xs), prog)


def _poll(ctx, R, cr, phase, x):
    pos, host_ip = x.pos, x.ips["host"]
    want = PHASE_STAGES[phase]
    user, pw = cr.host
    dl, by = _deadline(phase, x)
    got: dict[str, RungResult] = {}

    def poll():
        sized = {"phase": phase, "deadline_s": dl, **by}
        got.update(R.poll(host_ip, deadline_s=dl))
        if any(r.detail.get("error") == "timeout" for r in got.values()):
            # the agent may still be running fio/stress: stop it before the next phase
            st = R.stop_agent(host_ip, user, pw, runner=ctx.runner)
            sized["agent_stop"] = ("stopped" if st.verdict == "pass"
                                   else st.detail.get("error") or "stop failed")
            x.busy = st.verdict != "pass"
        if "agent" in got:      # unmeasured when the agent never answered at all
            a = got.pop("agent")
            return RungResult(a.verdict, {**a.detail, **sized})
        names = list(got)
        run_id = next((r.detail.get("run_id") for r in got.values() if r.detail.get("run_id")),
                      None)
        detail = {"run_id": run_id, "stages": names, **sized}
        if "agent_stop" in sized:
            detail["error"] = (f"{phase} phase timed out after {dl}s; agent "
                               + ("stopped" if sized["agent_stop"] == "stopped"
                                  else f"NOT stopped: {sized['agent_stop']}"))
            return RungResult("fail", detail)
        if names != list(want):
            missing = [n for n in want if n not in got]
            extra = [n for n in names if n not in want]
            detail["error"] = (f"agent stages differ from the {phase} list:"
                               + (f" missing {','.join(missing)}" if missing else "")
                               + (f" extra {','.join(extra)}" if extra else "")
                               + ("" if missing or extra else " (order differs)"))
            return RungResult("fail", detail)
        return RungResult("pass", detail)

    ar = _rung(ctx, pos, "agent-stages", poll, STAGE)
    if ar.verdict != "pass":
        _out(x, phase, ar)
    run_seg = (ar.detail or {}).get("run_id")
    word = "not measured" if ar.verdict == UNMEASURED else "failed"
    for name in want:
        if name in got:
            r = _record(ctx, pos, name, got[name], STAGE, run_seg)
        elif got:
            r = _record(ctx, pos, name, RungResult("fail", {"error": f"agent did not report {name}"}))
        else:
            r = _record(ctx, pos, name, RungResult("skip", {"reason": f"agent-stages {word}"}))
        if r.verdict in ("fail", UNMEASURED):
            _out(x, phase, r)
    if phase == "inventory":
        inv = got.get("inventory")
        arts = inv.artifacts if inv else {}
        x.lsblk = arts.get("lsblk")
        # POP and the ship checks never gate either (a missing profile must not keep the
        # iperf server out of burn/network); their fail still fails the device in
        # qualify_passed. The check evidence goes beside the battery's artifacts.
        try:
            res = population_rung(ctx, x.d, arts,
                                  oob_fru0(R, cr, x.ips.get("bmc"), runner=ctx.runner))
        except Exception as e:              # noqa: BLE001 -- a judge never sinks the phase
            res = RungResult("fail", {"error": f"{type(e).__name__}: {e}"})
        _record(ctx, pos, "population-check", res, STAGE, run_seg)
        for name in shipchecks.SHIP_CHECKS:
            try:
                res = ship_check_rung(name, arts)
            except Exception as e:          # noqa: BLE001 -- a judge never sinks the phase
                res = RungResult("fail", {"error": f"{type(e).__name__}: {e}"})
            _record(ctx, pos, name, res, STAGE, run_seg)


# ---------------------------------------------------------------- network phase
def _host_lookup(ctx, R, lr, pos, host_ip, user, pw, art, cmd, parse):
    """Parse the saved inventory artifact `art`; over ssh `cmd` when it is missing."""
    text = _artifact(ctx, lr, pos, "inventory", art)
    res = parse(text) if text else None
    if not res:
        rc, out, _ = R.host_run(host_ip, user, pw, cmd, runner=ctx.runner)
        res = parse(out) if rc == 0 else None   # stdout only; stderr is ssh noise
    return res


def server_not_ready(lr, d) -> str | None:
    """Why an iperf server outside this run's positions may not serve (None = it may):
    every earlier-phase rung but the non-gating ones (population-check, the ship checks,
    console) must have passed last time, and its agent must not have been left running."""
    stop = ((lr.get((d.pos, "agent-stages")) or {}).get("detail") or {}).get("agent_stop")
    if stop not in (None, "stopped"):
        return "its qual agent is still running (stop failed)"
    for r in device_rungs(d):
        if r in (*NET_RUNGS, *NON_GATING):
            continue
        run = lr.get((d.pos, r))
        if not (_stage_ok(run) if r in AGENT_STAGES else (run or {}).get("verdict") == "pass"):
            return ("not measured: its " + r if (run or {}).get("verdict") == UNMEASURED
                    else f"its {r} did not pass")
    return None


def _network(ctx, R, prog, scope, ips, cr, not_ready):
    """not_ready: pos -> (skip reason, server reason) for devices out of this phase."""
    g = ctx.g
    lr = ctx.db.latest_runs()
    srv = g.iperf_server
    srv_dev = next((d for d in g.devices if d.pos == srv), None)
    up = [d for d in scope if d.pos not in not_ready]
    for d in scope:
        if d.pos in not_ready:
            _skip(ctx, d.pos, [r for r in NET_RUNGS if r in device_rungs(d)], not_ready[d.pos][0])
    prog.start("network", [d.pos for d in up])
    user, pw = cr.host if cr.host else ("", "")

    # -- wedge LLDP -> HA path -> fabric addressing
    fab_ifaces: dict[str, dict] = {}
    cabled = [d for d in up if "ha-path" in device_rungs(d)]
    if cabled:
        lldp, unread, unanswered = {}, [], set()
        for s in (s for s in g.switches if s.role == "tor"):
            if not s.mgmt_ip:
                res = RungResult("fail", {"error": f"switch {s.id} has no mgmt_ip in geometry.json"})
            else:
                try:
                    res = R.read_lldp(s.mgmt_ip, *cr.sonic, runner=ctx.runner)
                except Exception as e:      # noqa: BLE001
                    res = RungResult("fail", {"error": f"{type(e).__name__}: {e}"})
            # the table text goes to artifacts/_rack/q<run>/lldp-<id>/; the row keeps its path
            r = _record(ctx, "_rack", f"lldp-{s.id}", res)
            if r.verdict == "pass":
                lldp[s.id] = [LldpEntry(**e) for e in r.detail.get("lldp", [])]
            else:
                unread.append(s.id)
                if r.verdict == UNMEASURED:
                    unanswered.add(s.id)
        macs = {}
        for d in cabled:
            macs[d.pos] = _host_lookup(ctx, R, lr, d.pos, ips[d.pos]["host"], user, pw,
                                       "ip-d-link", "ip -o link\n", parse_ip_link) or {}
        checked = ha.check(g, lldp, macs)
        for d in cabled:
            res = checked.get(d.pos) or RungResult("fail", {"error": "no HA result"})
            if unread and not lldp and all(x in unanswered for x in unread):
                # no wedge answered: nothing was judged (one that answered still judges)
                why = f"LLDP not read: {', '.join(unread)} did not answer ssh"
                res = RungResult(UNMEASURED, {**res.detail, "error": why, "headline": why,
                                              "reason": why})
            elif unread:
                det = dict(res.detail)
                det["error"] = (f"LLDP unreadable on {', '.join(unread)}"
                                + (f"; {det['error']}" if det.get("error") else ""))
                res = RungResult("fail", det)
            _record(ctx, d.pos, "ha-path", res)
            octet = int(ips[d.pos]["host"].rsplit(".", 1)[1])
            links = res.detail.get("links") or []

            def fab():
                assigns = fabric.assignments(g, d, links, octet)
                return R.apply_fabric(ips[d.pos]["host"], user, pw, assigns, runner=ctx.runner)

            if _rung(ctx, d.pos, "fabric-addr", fab, STAGE).verdict == "pass":
                fab_ifaces[d.pos] = {
                    l["seen"].split()[0]: (l["ifname"], fabric.fabric_addr(
                        g, d, l["seen"].split()[0], octet).split("/")[0])
                    for l in links if l.get("seen") and l.get("ifname")}

    # -- iperf mesh (this rack's server only; never the bang)
    meshed = [d for d in up if "iperf-mesh" in device_rungs(d)]
    if not meshed:
        return

    def node(d):
        hip = ips[d.pos]["host"]
        ifaces = dict(fab_ifaces.get(d.pos, {}))
        mif = _host_lookup(ctx, R, lr, d.pos, hip, user, pw, "ip-d-address",
                           "ip -o -4 addr show\n", lambda t: iface_of(t, hip))
        if mif:
            ifaces["mgmt"] = (mif, hip)
        return {"pos": d.pos, "host_ip": hip, "ifaces": ifaces}

    why, config = None, False       # config: a geometry defect (fail), else unmeasured
    if not srv:
        why, config = "no iperf_server in geometry.json", True
    elif srv_dev is None or not srv_dev.endpoints.get("hosts"):
        why, config = f"server {srv} not ready: not a host-bearing device of this rack", True
    elif srv in not_ready:
        why = f"server {srv} not ready: {not_ready[srv][1]}"
    server = None
    if why is None:
        server = node(srv_dev)
        if "mgmt" not in server["ifaces"]:
            why = f"server {srv} not ready: no interface holds {server['host_ip']}"
    rids = {d.pos: ctx.db.start_run(d.pos, STAGE, "iperf-mesh") for d in meshed}
    if why is not None and config:
        out = {p: RungResult("fail", {"error": why}) for p in rids}
    elif why is not None:   # the server is not ready: nothing judged for any client
        out = {p: RungResult(UNMEASURED, {"error": why, "headline": why, "reason": why})
               for p in rids}
    else:
        try:
            clients = [node(d) for d in meshed if d.pos != srv]
            out = R.mesh(server, clients, user=user, pw=pw, runner=ctx.runner)
        except Exception as e:              # noqa: BLE001
            out = {p: RungResult("fail", {"error": f"{type(e).__name__}: {e}"}) for p in rids}
    for p, rid in rids.items():
        ctx.db.finish_run(rid, out.get(p) or RungResult("fail", {"error": "no iperf result"}))
        prog.done(p)


# ---------------------------------------------------------------- stage
def run_qualify(ctx, *, positions: list[str] | None = None) -> None:
    if ctx.db.closed_at():
        raise RuntimeError("order is closed")
    g = ctx.g
    hosts = [d for d in g.devices if d.endpoints.get("hosts")]
    if positions:
        unknown = sorted(set(positions) - {d.pos for d in hosts})
        if unknown:
            raise ValueError(f"not a host-bearing position: {', '.join(unknown)}")
        targets = [d for d in hosts if d.pos in positions]
    else:
        targets = hosts
    cr = creds(ctx)
    R = ctx.rungs or real_rungs()
    ips: dict[str, dict] = {}
    for a in plan_addresses(g):               # the one address formula (host octet included)
        ips.setdefault(a.pos, {})[a.role] = a.ip
    ctx.db.add_event("qualify", f"qualify started: {', '.join(d.pos for d in targets) or 'none'}")
    lr = ctx.db.latest_runs()               # Discover's verdicts, read before anything runs
    rows = {x["pos"]: x for x in ctx.db.devices()}
    xs = [_Dev(d, ips.get(d.pos, {}), rows.get(d.pos) or {"pos": d.pos}) for d in targets]
    prog = _Progress(ctx)
    try:
        prog.start("boot", [x.pos for x in xs])
        _wave(ctx, "boot", xs, lambda x: _boot(ctx, R, cr, lr, x), MAX_PARALLEL, prog)
        _phase_end(ctx, "boot", xs)
        _console_end(ctx, R, [x for x in xs if x.failed])   # out of Qualify: stop its SOL
        for phase in PHASE_STAGES:          # inventory, storage, burn: barrier between each
            _cancel_out(ctx, xs)
            for x in xs:
                if x.failed:
                    _skip_phase(ctx, x, phase)
            _agent_phase(ctx, R, cr, prog, phase, [x for x in xs if not x.failed])
            _phase_end(ctx, phase, xs)
            _console_end(ctx, R, [x for x in xs if x.failed])
        stop = _cancel_out(ctx, xs)
        not_ready = {x.pos: (_why_out(x), "its qual agent is still running (stop failed)"
                             if x.busy else ("not measured" if x.unmeasured else "failed")
                             + f" in phase {x.failed}")
                     for x in xs if x.failed}
        scope = [x.d for x in xs]
        srv = next((d for d in g.devices if d.pos == g.iperf_server), None)
        if not stop and srv is not None and srv.endpoints.get("hosts") and srv not in scope:
            why = server_not_ready(ctx.db.latest_runs(), srv)
            if why is None:
                scope.append(srv)           # its fabric addresses are not persistent
            else:
                not_ready[srv.pos] = (why, why)
        if stop:
            for d in scope:
                _skip(ctx, d.pos, [r for r in NET_RUNGS if r in device_rungs(d)], "cancelled")
        else:
            _network(ctx, R, prog, scope, ips, cr, not_ready)
    finally:
        _console_end(ctx, R, xs)            # always: no SOL session outlives the stage
        prog.clear()
    ok, bad = qualify_passed(ctx.db, g)
    done = "qualify cancelled" if cancelled(ctx) else "qualify done"
    ctx.db.add_event("qualify", f"{done}; rack passed" if ok
                     else f"{done}; not passed: {', '.join(bad)}")
