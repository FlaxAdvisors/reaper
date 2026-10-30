"""Qualify stage for a rack order (spec section 7), run in lockstep phases.

The integrated rack is kept in step: five rack-wide phases, each started on every
still-eligible device at once, with a barrier (wait for all) before the next:

  boot       pxe-live (identity checked)                       BOOT_DEADLINE_S
  inventory  agent: sdr-pre..inventory; then pop, fw-versions  INVENTORY_DEADLINE_S
  storage    agent: fio                                        120 s + 120 s x disks
  burn       agent: mem-pre, cpu-mem-stress .. sel-post        FLAX_STRESS_SECONDS + 900 s
  network    rack: lldp-<wedge>; per device ha-path, fabric-addr, iperf-mesh

Each agent phase relaunches the post.tgz agent with that phase's FLAX_QUAL_STAGES
(agent.launch stops an agent running another stage list and starts it fresh), all
devices in one launch wave, then polls them in parallel; each battery stage is
recorded as its own rung as soon as its device finishes (artifacts under
artifacts/<pos>/<agent run>/<stage>/), so a slow device delays only the barrier.
agent-launch and agent-stages are recorded once per agent phase (detail.phase).
The storage deadline counts the disks fio will read in the inventory's lsblk
capture (fio_disks; 8 when there is none). On a deadline the agent is stopped, so
nothing still runs when the next phase starts; a node whose stop failed is named.

A device failing a phase (a `fail` on any of that phase's rungs; fw-versions and
pop never gate -- a pop fail still fails the device in qualify_passed) gets `skip` ("failed in phase <p>") on every rung of
the later phases; the rest of the rack carries on. Identity first: a device is
only booted when its latest Discover `reserve` and `bmc-reach` passed and order.db
holds its host MAC (else pxe-live fails "discover first"); pxe-live then requires
the live image's BOOTIF MAC to be that host MAC.

`positions` re-runs every phase for those devices only; the network phase adds this
rack's iperf server (geometry.iperf_server; its fabric addresses do not survive a
reboot) when it passed every earlier phase. If the server is not ready every client
fails "server <pos> not ready". The current phase and barrier count are kept in
order.db (qualify_progress) for the UI and cleared when the stage ends.

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

from flax_post import population
from flax_rungs import agent, bmc, fabric, ha, hostssh, iperf, liveboot, sonic
from flax_rungs.result import RungResult
from flax_rungs.sonic import LldpEntry

from .addressing import plan_addresses
from .stages import _rung, creds

STAGE = "qualify"
FW_STAGE = "firmware"
AGENT_STAGES = tuple(agent.ORDER_STAGES.split(","))
PHASES = ("boot", "inventory", "storage", "burn", "network")
PHASE_STAGES = {    # the agent's FLAX_QUAL_STAGES per phase; together = ORDER_STAGES
    "inventory": ("sdr-pre", "sel-pre", "sel-clear", "tooling", "inventory"),
    "storage": ("fio",),
    "burn": ("mem-pre", "cpu-mem-stress", "mem-post", "sdr-post", "sel-post"),
}
NET_RUNGS = ("ha-path", "fabric-addr", "iperf-mesh")
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
        mc_info=bmc.mc_info, host_run=hostssh.run)


def pop_default(g, d) -> str:
    """The BOM default: geometry `pop` if set, else <customer>-<kind> (f4-compute-tp)."""
    return d.pop or f"{g.customer.lower()}-{d.kind}"


def device_rungs(d) -> list[str]:
    """The Qualify rungs a device must pass, in pipeline order ([] = no host)."""
    if not d.endpoints.get("hosts"):
        return []
    out = ["pxe-live", "agent-launch", "agent-stages", *AGENT_STAGES, "pop"]
    if any(n.startswith("pcie") for n in d.links):
        out += ["ha-path", "fabric-addr"]
    return out + ["iperf-mesh"]


def qualify_passed(db, g) -> tuple[bool, list[str]]:
    """(rack passed?, positions not passed). Every Qualify rung's latest run must be a
    finished `pass`; an IOM-only device (no host) counts via its Discover bmc-reach."""
    lr = db.latest_runs()
    bad = []
    for d in g.devices:
        need = device_rungs(d) or ["bmc-reach"]
        if not all((lr.get((d.pos, r)) or {}).get("verdict") == "pass" for r in need):
            bad.append(d.pos)
    return not bad, bad


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


def _record(ctx, pos, rung, res: RungResult, stage=STAGE, run_seg=None) -> RungResult:
    """One finished run row for a result obtained elsewhere; its artifacts (text) are
    written under artifacts/<pos>/<run_seg>/<rung>/ and the row keeps their paths."""
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
            seg = run_seg if _safe(run_seg) else f"q{rid}"
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


def parse_bios(dmidecode: str) -> dict:
    """{"version", "date"} from dmidecode's `BIOS Information` section."""
    out = {"version": None, "date": None}
    sec = re.search(r"^BIOS Information\s*\n((?:[ \t]+.*\n?)*)", dmidecode or "", re.M)
    if sec:
        for key, k in (("Version", "version"), ("Release Date", "date")):
            m = re.search(rf"^\s+{key}:\s*(.+?)\s*$", sec.group(1), re.M)
            if m:
                out[k] = m.group(1)
    return out


# ---------------------------------------------------------------- per-device rungs
def pop_rung(ctx, d, macinv: str | None) -> RungResult:
    ovr = next((x["pop_override"] for x in ctx.db.devices() if x["pos"] == d.pos), None)
    p = ovr or pop_default(ctx.g, d)
    path = os.path.join(population.PROFILE_DIR, p)
    if "/" in p or not os.path.isfile(path):
        return RungResult("fail", {"profile": p, "error": f"no profile {p}"})
    rules = population.load_profile(p)
    if not rules:                           # never pass on nothing (spec A4.6)
        return RungResult("fail", {"profile": p, "error": f"profile {p} has no rules"})
    if not macinv or not macinv.strip():
        return RungResult("fail", {"profile": p, "error": "no macinv from the inventory stage"})
    ev = population.evaluate(rules, macinv)
    detail = {"profile": p, "results": ev["results"]}
    if not ev["ok"]:
        n = sum(1 for r in ev["results"] if not r["ok"])
        detail["error"] = f"{n} of {len(ev['results'])} {p} rule(s) not found"
    return RungResult("pass" if ev["ok"] else "fail", detail)


def fw_versions(dmidecode: str | None, mc: dict | None) -> RungResult:
    """Report only: pass whenever both versions were read; never enforced or flashed."""
    b = parse_bios(dmidecode or "")
    mc = mc or {}
    detail = {"bios_version": b["version"], "bios_date": b["date"],
              "bmc_firmware": mc.get("firmware"), "bmc_aux": mc.get("aux"),
              "bmc_cipher": mc.get("cipher")}
    missing = [w for w, v in (("BIOS version (dmidecode)", b["version"]),
                              ("BMC firmware (mc info)", mc.get("firmware"))) if not v]
    if missing:
        detail["error"] = "unreadable: " + ", ".join(missing)
        return RungResult("fail", detail)
    return RungResult("pass", detail)


def discover_gate(lr, dev_row) -> str | None:
    """Why this device may not be booted yet (None = Discover proved it): its latest
    reserve and bmc-reach must be pass and order.db must hold its host MAC."""
    v = {r: (lr.get((dev_row["pos"], r)) or {}).get("verdict") for r in ("reserve", "bmc-reach")}
    bad = [f"{r} {v[r] or 'not run'}" for r in v if v[r] != "pass"]
    if not bad and not dev_row.get("host_mac"):
        bad.append("no host MAC matched")
    return ("discover first: " + ", ".join(bad)) if bad else None




# ---------------------------------------------------------------- phase plumbing
@dataclass
class _Dev:
    """One target device's walk through the phases."""
    d: object
    ips: dict
    row: dict
    failed: str | None = None       # the phase it failed in (later phases skip it)
    busy: bool = False              # an agent stop failed: its battery may still run
    lsblk: str | None = None        # the inventory's lsblk capture (storage deadline)

    @property
    def pos(self) -> str:
        return self.d.pos


class _Progress:
    """The current phase and its barrier, kept in order.db for the UI (best effort)."""

    def __init__(self, ctx):
        self.ctx, self.mu, self.cur = ctx, threading.Lock(), None

    def start(self, phase, poss):
        with self.mu:
            self.cur = {"phase": phase, "n": PHASES.index(phase) + 1, "of": len(PHASES),
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
            self.ctx.db.set_qualify_progress(self.cur)
        except Exception:                   # noqa: BLE001 -- a view, never the job
            pass


def _phase_rungs(phase, d) -> list[tuple[str, str]]:
    """(rung, stage) pairs `phase` records for device d: what a device that failed an
    earlier phase gets `skip` on. agent-launch/agent-stages belong to the first agent
    phase; a later agent phase re-records them only for a device that runs it."""
    if phase == "boot":
        return [("pxe-live", STAGE)]
    if phase == "inventory":
        return ([(r, STAGE) for r in ("agent-launch", "agent-stages",
                                      *PHASE_STAGES["inventory"], "pop")]
                + [("fw-versions", FW_STAGE)])
    if phase in PHASE_STAGES:
        return [(r, STAGE) for r in PHASE_STAGES[phase]]
    return [(r, STAGE) for r in NET_RUNGS if r in device_rungs(d)]


def _why_out(x) -> str:
    return ("qual agent still running (stop failed)" if x.busy
            else f"failed in phase {x.failed}")


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


def _phase_end(ctx, phase, xs):
    ran = [x for x in xs if x.failed is None or x.failed == phase]
    bad = [x.pos for x in ran if x.failed == phase]
    ctx.db.add_event("qualify", f"phase {phase}: {len(ran) - len(bad)} passed"
                                + (f", failed: {', '.join(bad)}" if bad else ""))


def _with(r: RungResult, **extra) -> RungResult:
    return RungResult(r.verdict, {**r.detail, **extra}, r.artifacts)


# ---------------------------------------------------------------- boot phase
def _boot(ctx, R, cr, lr, x):
    pos, d = x.pos, x.d
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
        r = _rung(ctx, pos, "pxe-live",
                  lambda: R.boot_live(bmc_ip, host_ip, cr, expect_mac=x.row["host_mac"],
                                      deadline_s=BOOT_DEADLINE_S, runner=ctx.runner), STAGE)
    if r.verdict != "pass":
        x.failed = "boot"


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
        r = _rung(ctx, x.pos, "agent-launch", lambda: _with(
            R.launch(x.ips["host"], user, pw, stages=stages, runner=ctx.runner), phase=phase),
            STAGE)
        if r.verdict == "pass":
            with mu:
                started[x.pos] = time.monotonic()
            return
        x.failed = phase
        _skip(ctx, x.pos, ["agent-stages", *want] + (["pop"] if phase == "inventory" else []),
              "agent-launch failed")
        if phase == "inventory":
            _skip(ctx, x.pos, ["fw-versions"], "agent-launch failed", FW_STAGE)
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
        if "agent" in got:
            return RungResult("fail", {**got.pop("agent").detail, **sized})
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
    failed = ar.verdict != "pass"
    run_seg = (ar.detail or {}).get("run_id")
    for name in want:
        if name in got:
            r = _record(ctx, pos, name, got[name], STAGE, run_seg)
        elif got:
            r = _record(ctx, pos, name, RungResult("fail", {"error": f"agent did not report {name}"}))
        else:
            r = _record(ctx, pos, name, RungResult("skip", {"reason": "agent-stages failed"}))
        failed = failed or r.verdict == "fail"
    if phase == "inventory":
        inv = got.get("inventory")
        arts = inv.artifacts if inv else {}
        x.lsblk = arts.get("lsblk")
        _rung(ctx, pos, "fw-versions",      # report only: never gates the next phase
              lambda: fw_versions(arts.get("dmidecode"),
                                  R.mc_info(x.ips.get("bmc"), cr.bmc, runner=ctx.runner)),
              FW_STAGE)
        # POP never gates either (a missing profile must not keep the iperf server out
        # of burn/network); its fail still fails the device in qualify_passed
        _rung(ctx, pos, "pop", lambda: pop_rung(ctx, x.d, arts.get("macinv")), STAGE)
    if failed:
        x.failed = phase


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
    every earlier-phase rung but pop (which never gates) must have passed last time,
    and its agent must not have been left running."""
    stop = ((lr.get((d.pos, "agent-stages")) or {}).get("detail") or {}).get("agent_stop")
    if stop not in (None, "stopped"):
        return "its qual agent is still running (stop failed)"
    for r in device_rungs(d):
        if r not in (*NET_RUNGS, "pop") and (lr.get((d.pos, r)) or {}).get("verdict") != "pass":
            return f"its {r} did not pass"
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
        lldp, unread = {}, []
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
        macs = {}
        for d in cabled:
            macs[d.pos] = _host_lookup(ctx, R, lr, d.pos, ips[d.pos]["host"], user, pw,
                                       "ip-d-link", "ip -o link\n", parse_ip_link) or {}
        checked = ha.check(g, lldp, macs)
        for d in cabled:
            res = checked.get(d.pos) or RungResult("fail", {"error": "no HA result"})
            if unread:
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

    why = None
    if not srv:
        why = "no iperf_server in geometry.json"
    elif srv_dev is None or not srv_dev.endpoints.get("hosts"):
        why = f"server {srv} not ready: not a host-bearing device of this rack"
    elif srv in not_ready:
        why = f"server {srv} not ready: {not_ready[srv][1]}"
    server = None
    if why is None:
        server = node(srv_dev)
        if "mgmt" not in server["ifaces"]:
            why = f"server {srv} not ready: no interface holds {server['host_ip']}"
    rids = {d.pos: ctx.db.start_run(d.pos, STAGE, "iperf-mesh") for d in meshed}
    if why is not None:
        out = {p: RungResult("fail", {"error": why}) for p in rids}
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
        for phase in PHASE_STAGES:          # inventory, storage, burn: barrier between each
            for x in xs:
                if x.failed:
                    _skip_phase(ctx, x, phase)
            _agent_phase(ctx, R, cr, prog, phase, [x for x in xs if not x.failed])
            _phase_end(ctx, phase, xs)
        not_ready = {x.pos: (_why_out(x), "its qual agent is still running (stop failed)"
                             if x.busy else f"failed in phase {x.failed}")
                     for x in xs if x.failed}
        scope = [x.d for x in xs]
        srv = next((d for d in g.devices if d.pos == g.iperf_server), None)
        if srv is not None and srv.endpoints.get("hosts") and srv not in scope:
            why = server_not_ready(ctx.db.latest_runs(), srv)
            if why is None:
                scope.append(srv)           # its fabric addresses are not persistent
            else:
                not_ready[srv.pos] = (why, why)
        _network(ctx, R, prog, scope, ips, cr, not_ready)
    finally:
        prog.clear()
    ok, bad = qualify_passed(ctx.db, g)
    ctx.db.add_event("qualify", "qualify done; rack passed" if ok
                     else f"qualify done; not passed: {', '.join(bad)}")
