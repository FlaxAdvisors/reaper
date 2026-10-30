"""Firmware for a rack order: loose-node Post's 6 firmware steps per device, DETECT ONLY.

Step names are Post's (bmc-checked, bmc-updated, bios-checked, bios-updated,
mlx-checked, mlx-updated); each pair's states come from the phase the loose-node
daemon would have written (flax_rungs.fwsteps = blades._FW_BMC/_FW_BIOS/_nic_steps)
and ride in detail.state; the verdict is done -> pass, fault/cur -> fail, pending ->
skip (fwcheck.VERDICT). Evidence is Post's stepinfo shape; raw reads are saved as
artifacts/<key>/f<run>/<step>/<name>.txt. Rackpost never flashes: an off-target
version is `needs_update` (triage flashes it).

Not lockstepped: every device (and switch) runs on its own, at most MAX_PARALLEL at once.

  Every BMC/IOM read first needs that endpoint's latest Discover bmc-leased and
  bmc-pinged to be pass (discover_gate: the planned IP is leased by the discovered MAC
  and answers), else its steps `skip` "discover first: ..." and no login is sent.
  The rack's Cancel flag is checked before every step pair (`skip` "cancelled").

  TiogaPass host+BMC (compute-tp, bc-head-tp): BMC = fwd's Redfish probe against the
    BMC manifest; BIOS + Mellanox = biosd/nicd's reads over ssh to the host, only when
    Discover's host-ssh passed AND a re-read now shows the live image with this
    device's BOOTIF (else those 4 steps `skip` "host not in live image"; an ssh that
    no longer answers is Post's `unreachable`).
  JBOD (bc-jbod): each IOM is its own row set (key <pos>.iomN): bmc-checked = its
    fw-util versions (identity.iom), phase `unsupported` (report only); bios/mlx
    `skip`. The device's rows roll its IOMs up.
  Switches (key _sw-<id>): a wedge's BMC = /etc/issue + cpld_rev.sh (identity.wedge_bmc)
    at the IP Discover's bmc-leased found, phase `unsupported`; a Dell has no BMC.
    bios/mlx `skip`.

ctx.rungs (a namespace like real_rungs(); FakeRungs in tests and RACKPOST_FAKE)
replaces the rung functions.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from flax_rungs import fwcheck, hostssh, identity, liveboot
from flax_rungs.fwsteps import FW_STEPS
from flax_rungs.result import RungResult

from .addressing import plan_addresses
from .discover_ladder import _rollup, run_step, wanted
from .stages import cancelled, creds

STAGE = "firmware"
MAX_PARALLEL = 8
TP_KINDS = ("compute-tp", "bc-head-tp")      # TiogaPass OpenBMC + host: Post's three daemons
_STATE_RANK = ("fault", "cur", "pending", "done")


def real_rungs():
    return SimpleNamespace(
        fw_manifests=fwcheck.load_manifests, fw_bmc=fwcheck.tp_bmc,
        fw_bios=fwcheck.bios, fw_nic=fwcheck.nic, host_run=hostssh.run,
        iom=identity.iom, wedge_bmc=identity.wedge_bmc, wall=time.time)


class _Rows:
    """One key's six steps, recorded in Post's order; .res feeds discover's _rollup."""

    def __init__(self, ctx, key):
        self.ctx, self.key, self.res = ctx, key, {}

    def pair(self, prefix, fn):
        """<prefix>-checked shows 'running' while fn() reads; both rows land after."""
        chk, upd = f"{prefix}-checked", f"{prefix}-updated"
        if cancelled(self.ctx):
            fn = lambda: fwcheck.skipped("cancelled")      # noqa: E731
        out = {}

        def first():
            try:
                out["pair"] = fn()
            except Exception as e:          # noqa: BLE001 -- the checked row records it
                why = f"{type(e).__name__}: {e}"
                out["pair"] = (RungResult("fail", {"headline": why, "error": why,
                                                   "state": "fault", "rows": [], "notes": []}),
                               RungResult("skip", {"headline": f"{chk} failed",
                                                   "reason": f"{chk} failed", "rows": [],
                                                   "notes": []}))
            return out["pair"][0]

        self.res[chk] = run_step(self.ctx, self.key, chk, first, STAGE)
        upd_r = out.get("pair", (None, RungResult("skip", {"headline": f"{chk} failed"})))[1]
        self.res[upd] = run_step(self.ctx, self.key, upd, lambda: upd_r, STAGE)

    def skip(self, prefix, why):
        self.pair(prefix, lambda: fwcheck.skipped(why))


GATE_STEPS = ("bmc-leased", "bmc-pinged")


def discover_gate(lr, key) -> str | None:
    """Why `key`'s BMC/IOM may not be read yet (None = Discover proved its IP): the
    latest bmc-leased (the discovered MAC holds an active lease on the planned IP) and
    bmc-pinged must be pass -- before that the IP may be someone else's."""
    v = {r: (lr.get((key, r)) or {}).get("verdict") for r in GATE_STEPS}
    bad = [f"{r} {v[r] or 'not run'}" for r in GATE_STEPS if v[r] != "pass"]
    return ("discover first: " + ", ".join(bad)) if bad else None


# ---------------------------------------------------------------- TiogaPass
def _live_host(ctx, R, cr, lr, d, host_ip):
    """None when the host is in the live image as this device (BOOTIF = Discover's
    host MAC); else (kind, why): kind 'skip' (not in the live image) or 'unreachable'."""
    hs = lr.get((d.pos, "host-ssh")) or {}
    if hs.get("verdict") != "pass":
        why = ((hs.get("detail") or {}).get("headline") or hs.get("verdict")
               or "not run")
        return "skip", f"host not in live image (Discover host-ssh: {why})"
    if not cr.host:
        return "skip", "host not in live image (no credentials-host.json flax row)"
    want = next((x.get("host_mac") for x in ctx.db.devices() if x["pos"] == d.pos), None)
    rc, out, err = R.host_run(host_ip, *cr.host, liveboot._PROBE, timeout=20, runner=ctx.runner)
    if rc != 0:
        return "unreachable", f"ssh {host_ip} rc={rc} {hostssh.tail(out, err, 160)}".strip()
    _bid, _up, cmdline = liveboot.parse_probe(out)
    bootif = liveboot.bootif_mac(cmdline)
    if "BOOTIF=" not in cmdline or "pxeaddr=" not in cmdline:
        return "skip", f"host not in live image ({host_ip} answers without BOOTIF/pxeaddr)"
    if not want or bootif != want:
        return "skip", (f"host not in live image of this device (BOOTIF {bootif or 'unparsed'}"
                        f" != discovered {want or 'none'})")
    return None


def _tp(ctx, R, cr, M, lr, d, ips) -> _Rows:
    rows = _Rows(ctx, d.pos)
    bmc_ip, host_ip = ips.get("bmc"), ips.get("host")

    def bmc():
        sl, arts = R.fw_bmc(bmc_ip, cr.bmc, M.bmc)
        return fwcheck.steps("bmc", sl, arts, M.sources,
                             ["read with flax-post-fwd's probe (Redfish FirmwareInventory) "
                              "against bmc-firmware-versions.json"])

    why = discover_gate(lr, d.pos)
    if why:
        rows.skip("bmc", why)
    else:
        rows.pair("bmc", bmc)
    if cancelled(ctx):
        rows.skip("bios", "cancelled")
        rows.skip("mlx", "cancelled")
        return rows
    gate = _live_host(ctx, R, cr, lr, d, host_ip)
    if gate and gate[0] == "skip":
        rows.skip("bios", gate[1])
        rows.skip("mlx", gate[1])
        return rows
    if gate:                                 # ssh to the live image no longer answers
        down = {"phase": "unreachable", "fault_reason": gate[1]}
        rows.pair("bios", lambda: fwcheck.steps("bios", {**down, "current": None}))
        rows.pair("mlx", lambda: fwcheck.steps("nic", {**down, "devices": []}))
        return rows
    kw = dict(run=R.host_run, runner=ctx.runner)
    rows.pair("bios", lambda: fwcheck.steps(
        "bios", *R.fw_bios(host_ip, *cr.host, M.bios, **kw), M.sources,
        ["read with flax-post-biosd's probe: dmidecode -t system -> family map -> "
         "bios-firmware-versions.json; dmidecode -t bios"]))
    rows.pair("mlx", lambda: fwcheck.steps(
        "nic", *R.fw_nic(host_ip, *cr.host, M.nic, **kw), M.sources,
        ["read with flax-post-nicd's query: mstflint query + mstconfig EXP_ROM_UEFI_x86_ENABLE "
         "per card, against nic-firmware-versions.json (keyed by PSID)"]))
    return rows


# ---------------------------------------------------------------- report-only BMCs
_NO_HOST = "no host BIOS/NIC firmware here"


def _report_only(ctx, key, read, notes, host_why) -> _Rows:
    rows = _Rows(ctx, key)
    rows.pair("bmc", lambda: fwcheck.steps("bmc", *fwcheck.report_only(read()), notes=notes))
    rows.skip("bios", host_why)
    rows.skip("mlx", host_why)
    return rows


def _iom(ctx, R, cr, lr, d, n, ip) -> _Rows:
    key = f"{d.pos}.iom{n}"
    why = discover_gate(lr, key)
    if why:
        rows = _Rows(ctx, key)
        rows.skip("bmc", why)
        rows.skip("bios", "an IOM has no host")
        rows.skip("mlx", "an IOM has no host")
        return rows

    def read():
        if not cr.obmc:
            return RungResult("fail", {"error": "add obmcuser/obmcpass to the vaulted "
                                                "credentials.json"})
        return R.iom(ip, *cr.obmc, runner=ctx.runner)
    return _report_only(ctx, key, read,
                        [f"IOM{n} at {ip}: /usr/bin/fw-util bmc --version and all --version "
                         "(Facebook OpenBMC; no firmware manifest exists for it)"],
                        "an IOM has no host")


def _jbod(ctx, R, cr, lr, d, ips):
    walks = {f"iom{n}": _iom(ctx, R, cr, lr, d, n, ips.get(f"iom{n}"))
             for n in range(1, d.endpoints["ioms"] + 1)}

    def worst(step):
        sts = [(w.res.get(step).detail or {}).get("state") for w in walks.values()
               if w.res.get(step)]
        st = next((s for s in _STATE_RANK if s in sts), None)
        return {"state": st} if st else {}

    _rollup(ctx, d.pos, walks, worst, FW_STEPS, STAGE)


def _switch(ctx, R, cr, lr, s):
    key = f"_sw-{s.id}"
    host_why = "a switch has no host BIOS/NIC firmware manifest"
    if not s.endpoints.get("bmc"):
        rows = _Rows(ctx, key)
        for p in ("bmc", "bios", "mlx"):
            rows.skip(p, f"no BMC ({s.model} endpoints: {', '.join(s.endpoints) or 'none'})"
                      if p == "bmc" else host_why)
        return rows
    why = discover_gate(lr, key)
    if why:
        rows = _Rows(ctx, key)
        rows.skip("bmc", why)
        rows.skip("bios", host_why)
        rows.skip("mlx", host_why)
        return rows
    ip = ((lr.get((key, "bmc-leased")) or {}).get("detail") or {}).get("ip")

    def read():
        if not ip:
            return RungResult("fail", {"error": "no BMC IP: Discover's bmc-leased did not pass"})
        if not cr.obmc:
            return RungResult("fail", {"error": "add obmcuser/obmcpass to the vaulted "
                                                "credentials.json"})
        return R.wedge_bmc(ip, *cr.obmc, runner=ctx.runner)
    return _report_only(ctx, key, read,
                        [f"wedge BMC at {ip or '?'}: /etc/issue + /usr/local/bin/cpld_rev.sh "
                         "(Facebook OpenBMC; no firmware manifest exists for it)"], host_why)


def _other(ctx, d):
    rows = _Rows(ctx, d.pos)
    for p in ("bmc", "bios", "mlx"):
        rows.skip(p, f"no firmware reader for kind {d.kind}")


# ---------------------------------------------------------------- stage
def run_firmware(ctx, *, positions: list[str] | None = None) -> None:
    """Every (or the named) device's and switch's 6 firmware steps. positions: device
    positions and/or switch ids (A or _sw-A)."""
    if ctx.db.closed_at():
        raise RuntimeError("order is closed")
    g = ctx.g
    devs, sws = wanted(g, positions)
    R = ctx.rungs or real_rungs()
    cr = creds(ctx)
    M = R.fw_manifests()
    lr = ctx.db.latest_runs()
    ips: dict[str, dict] = {}
    for a in plan_addresses(g):
        ips.setdefault(a.pos, {})[a.role] = a.ip
    ctx.db.add_event("firmware", "firmware started (detect only)")
    jobs = []
    for d in devs:
        ep = d.endpoints
        if d.kind in TP_KINDS and ep.get("bmc") and ep.get("hosts"):
            jobs.append((d.pos, lambda d=d: _tp(ctx, R, cr, M, lr, d, ips.get(d.pos, {}))))
        elif ep.get("ioms") and not ep.get("hosts") and not ep.get("bmc"):
            jobs.append((d.pos, lambda d=d: _jbod(ctx, R, cr, lr, d, ips.get(d.pos, {}))))
        else:
            jobs.append((d.pos, lambda d=d: _other(ctx, d)))
    for s in sws:
        if s.endpoints:
            jobs.append((f"_sw-{s.id}", lambda s=s: _switch(ctx, R, cr, lr, s)))

    def one(job):
        key, fn = job
        try:
            fn()
        except Exception as e:              # noqa: BLE001 -- recording itself failed
            ctx.db.add_event("error", f"firmware {key}: {type(e).__name__}: {e}")

    if jobs:
        with ThreadPoolExecutor(max_workers=max(1, min(MAX_PARALLEL, len(jobs))),
                                thread_name_prefix=f"firmware-{g.url_prefix}") as ex:
            list(ex.map(one, jobs))
    lr = ctx.db.latest_runs()
    off, bad = [], []
    for key in [d.pos for d in devs] + [f"_sw-{s.id}" for s in sws if s.endpoints]:
        sts = {st: (lr.get((key, st)) or {}) for st in FW_STEPS}
        if any((r.get("detail") or {}).get("phase") == "needs_update" for r in sts.values()):
            off.append(key)
        elif any(r.get("verdict") == "fail" for r in sts.values()):
            bad.append(key)
    ctx.db.add_event("firmware", ("firmware cancelled" if cancelled(ctx) else "firmware done")
                     + " (detect only)"
                     + (f"; needs update: {', '.join(off)}" if off else "")
                     + (f"; faulted: {', '.join(bad)}" if bad else "")
                     + ("" if off or bad else "; every device at target or report only"))
