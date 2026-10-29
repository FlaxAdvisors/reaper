"""Qualify stage for a rack order (spec section 7).

Per host-bearing device, in parallel (at most MAX_PARALLEL at once):
  pxe-live -> agent-launch -> agent-stages (poll the post.tgz battery; each battery
  stage is then recorded as its own rung, its artifacts saved under
  artifacts/<pos>/<agent run>/<stage>/) -> pop (inventory/macinv vs the profile),
plus the report-only firmware rung fw-versions (stage "firmware").
Then rack-level, after every device thread: lldp-<wedge> (pos "_rack"), ha-path,
fabric-addr and iperf-mesh per device.

A device whose pxe-live fails gets `skip` (with the reason) for every later rung;
the rack carries on. `positions` re-runs only those devices' per-device rungs, then
the rack-level ones for them plus the iperf server (whose fabric addresses are
re-applied: they do not survive a reboot). The iperf server is geometry.iperf_server
of THIS rack only; if it is not live every client fails "server <pos> not ready".

flax_rungs functions never touch order.db; this module records them. ctx.rungs
(a namespace like real_rungs()) replaces them in tests and in RACKPOST_FAKE.
"""
import os
import re
from concurrent.futures import ThreadPoolExecutor
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
MAX_PARALLEL = 8
_SEG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")


def real_rungs():
    return SimpleNamespace(
        boot_live=liveboot.boot_live, launch=agent.launch, poll=agent.poll,
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


def _device(ctx, R, d, ips, cr):
    pos = d.pos
    per_dev = ["agent-launch", "agent-stages", *AGENT_STAGES, "pop"]
    if d.endpoints["hosts"] > 1:
        _record(ctx, pos, "pxe-live", RungResult("fail", {
            "error": f"{d.endpoints['hosts']}-host devices are not qualified yet "
                     "(no per-card live boot)"}))
        _skip(ctx, pos, per_dev, "pxe-live failed")
        _skip(ctx, pos, ["fw-versions"], "pxe-live failed", FW_STAGE)
        return
    host_ip, bmc_ip = ips.get("host"), ips.get("bmc")
    if not bmc_ip:
        r = _record(ctx, pos, "pxe-live", RungResult("fail", {"error": "no BMC to boot it with"}))
    else:
        r = _rung(ctx, pos, "pxe-live",
                  lambda: R.boot_live(bmc_ip, host_ip, cr, runner=ctx.runner), STAGE)
    if r.verdict != "pass":
        _skip(ctx, pos, per_dev, "pxe-live failed")
        _skip(ctx, pos, ["fw-versions"], "pxe-live failed", FW_STAGE)
        return
    user, pw = cr.host
    r = _rung(ctx, pos, "agent-launch",
              lambda: R.launch(host_ip, user, pw, runner=ctx.runner), STAGE)
    if r.verdict != "pass":
        _skip(ctx, pos, per_dev[1:], "agent-launch failed")
        _skip(ctx, pos, ["fw-versions"], "agent-launch failed", FW_STAGE)
        return

    got: dict[str, RungResult] = {}

    def poll():
        got.update(R.poll(host_ip))
        if "agent" in got:
            return RungResult("fail", dict(got.pop("agent").detail))
        names = list(got)
        run_id = next((x.detail.get("run_id") for x in got.values() if x.detail.get("run_id")),
                      None)
        detail = {"run_id": run_id, "stages": names}
        if names != list(AGENT_STAGES):
            missing = [n for n in AGENT_STAGES if n not in got]
            extra = [n for n in names if n not in AGENT_STAGES]
            detail["error"] = ("agent stages differ from the order list:"
                               + (f" missing {','.join(missing)}" if missing else "")
                               + (f" extra {','.join(extra)}" if extra else "")
                               + ("" if missing or extra else " (order differs)"))
            return RungResult("fail", detail)
        return RungResult("pass", detail)

    ar = _rung(ctx, pos, "agent-stages", poll, STAGE)
    run_seg = (ar.detail or {}).get("run_id")
    for name in AGENT_STAGES:
        if name in got:
            _record(ctx, pos, name, got[name], STAGE, run_seg)
        elif got:
            _record(ctx, pos, name, RungResult("fail", {"error": f"agent did not report {name}"}))
        else:
            _record(ctx, pos, name, RungResult("skip", {"reason": "agent-stages failed"}))
    inv = got.get("inventory")
    arts = inv.artifacts if inv else {}
    _rung(ctx, pos, "fw-versions",
          lambda: fw_versions(arts.get("dmidecode"), R.mc_info(bmc_ip, cr.bmc, runner=ctx.runner)),
          FW_STAGE)
    _rung(ctx, pos, "pop", lambda: pop_rung(ctx, d, arts.get("macinv")), STAGE)


# ---------------------------------------------------------------- rack-level rungs
def _host_lookup(ctx, R, lr, pos, host_ip, user, pw, art, cmd, parse):
    """Parse the saved inventory artifact `art`; over ssh `cmd` when it is missing."""
    text = _artifact(ctx, lr, pos, "inventory", art)
    res = parse(text) if text else None
    if not res:
        rc, out, _ = R.host_run(host_ip, user, pw, cmd, runner=ctx.runner)
        res = parse(out) if rc == 0 else None   # stdout only; stderr is ssh noise
    return res


def _rack_level(ctx, R, targets, ips, cr):
    g = ctx.g
    lr = ctx.db.latest_runs()
    live = {d.pos for d in g.devices if d.endpoints.get("hosts")
            and (lr.get((d.pos, "pxe-live")) or {}).get("verdict") == "pass"}
    srv = g.iperf_server
    scope = list(targets)
    srv_dev = next((d for d in g.devices if d.pos == srv), None)
    if srv_dev is not None and srv in live and srv_dev not in scope:
        scope.append(srv_dev)               # its fabric addresses are not persistent
    up = [d for d in scope if d.pos in live]
    for d in scope:
        if d.pos not in live:
            _skip(ctx, d.pos, [r for r in ("ha-path", "fabric-addr", "iperf-mesh")
                               if r in device_rungs(d)], "pxe-live did not pass")
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
    elif srv not in live:
        why = f"server {srv} not ready: its pxe-live did not pass"
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
    with ThreadPoolExecutor(max_workers=max(1, min(MAX_PARALLEL, len(targets))),
                            thread_name_prefix=f"qualify-{g.url_prefix}") as ex:
        futs = {d.pos: ex.submit(_device, ctx, R, d, ips.get(d.pos, {}), cr) for d in targets}
    for pos, f in futs.items():
        if f.exception() is not None:       # recording itself failed; the rack carries on
            ctx.db.add_event("error", f"qualify {pos}: {type(f.exception()).__name__}: "
                                      f"{f.exception()}")
    _rack_level(ctx, R, targets, ips, cr)
    ok, bad = qualify_passed(ctx.db, g)
    ctx.db.add_event("qualify", "qualify done; rack passed" if ok
                     else f"qualify done; not passed: {', '.join(bad)}")
