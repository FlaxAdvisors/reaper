"""Loose Post's Deploy stage (spec 2026-11-06 loose-post-deploy-stage): one node at a
time, on its own clock, after its Qualify passed.

  install    wipe + Ubuntu 24.04 autoinstall (flax_rungs.install.install), the family's
             iPXE platform part + its autoinstall; skipped (kept) when Post installed
             this node before (post_node.vars.deployed: same BMC MAC row, same serial)
  firstboot  Hdd boot + installed-OS proof as flax (flax_rungs.install.firstboot)
  flax-user  /opt/flax/ansible/flax_user.yml (flax_rungs.ansible.flax_user)
  iperf      the installed OS against the site's post iperf server (the live-ISO
             stage's), both directions, each >= 0.8 x the link speed
  stress     flax_rungs.stress (stress-ng, SEL judged, dmesg MCE/EDAC) plus SDR and
             dimmerr judged before/after (sdr_bad, dimm_bad)

A node whose latest Qualify run shows no system storage (the disk the family's
autoinstall targets) skips Deploy. Pass or skip -> the Done tail (identify LED + power
off, read back). A failed phase skips the rest; the node stays powered.

The state lives in post_state.vars.deploy (this module's engine is its only writer, run
by flax_post.deployd). The operator asks for a re-run / reinstall through
vars.deploy_request (written by the Post UI only). No psycopg here: the store is passed in.
"""
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from flax_rungs import ansible, bmc, hostssh, install, mcedimm, pxeboot, redfish, stress
from flax_rungs import iperf as iperf_rung
from flax_rungs.fio import disk_info
from flax_rungs.remote import unreachable
from flax_rungs.result import UNMEASURED, RungResult

# `plan` is a rung of its own (operator 2026-11-09): a Deploy never fails before its first step
PHASES = ("plan", "install", "firstboot", "flax-user", "deploy-iperf", "deploy-stress")
STAGE = "deploy"
MODES = ("auto", "rerun", "reinstall")

# family (post's family map) -> iPXE platform part. Env FLAX_POST_DEPLOY_PLATFORMS
# "family=part,..." replaces it.
DEFAULT_PLATFORMS = {"tiogapass": "wiwynn-tp-hmc", "leopard": "leopard"}
TEMPLATES = Path(pxeboot.PXE_DIR) / "templates"
USB_CONTROLLERS = ("usb-storage", "uas")
IPERF_PORT = 11001
IPERF_SECONDS = 10
IPERF_STREAMS = 4
IPERF_BUSY_TRIES = 30
IPERF_BUSY_SLEEP_S = 20
DIMMERR = "/opt/flax/post-deployd/dimmerr.sh"


def _env_map(text):
    out = {}
    for item in (text or "").split(","):
        k, _, v = item.partition("=")
        if k.strip() and v.strip():
            out[k.strip()] = v.strip()
    return out


@dataclass
class Config:
    platforms: dict = field(default_factory=lambda: dict(DEFAULT_PLATFORMS))
    iperf_server: str = "bang-edam"
    iperf_port: int = IPERF_PORT
    workdir: str = "/var/lib/flax-post-deployd"
    install_root: str = install.INSTALL_ROOT
    pxe_dir: str = pxeboot.PXE_DIR
    dimmerr: str = DIMMERR
    max_parallel: int = 8

    @classmethod
    def from_env(cls, env=os.environ):
        c = cls()
        if env.get("FLAX_POST_DEPLOY_PLATFORMS"):
            c.platforms = _env_map(env["FLAX_POST_DEPLOY_PLATFORMS"])
        c.iperf_server = env.get("FLAX_POST_DEPLOY_IPERF_SERVER") or c.iperf_server
        c.workdir = env.get("FLAX_POST_DEPLOY_WORKDIR") or c.workdir
        c.dimmerr = env.get("FLAX_POST_DEPLOY_DIMMERR") or c.dimmerr
        c.max_parallel = int(env.get("FLAX_POST_DEPLOY_MAX_PARALLEL") or c.max_parallel)
        return c


def enabled(env=os.environ) -> bool:
    """host_qual's switch: a Qualify pass waits for Deploy instead of the Done tail."""
    return (env.get("FLAX_POST_DEPLOY") or "").lower() in ("1", "on", "true", "yes")


# ---------------------------------------------------------------- credentials
@dataclass
class Creds:
    bmc: list
    host: tuple = ()
    redfish: list = field(default_factory=list)


def _rows(path):
    try:
        with open(path) as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def load_creds(bmc_path="/etc/flax/credentials-bmc.json",
               host_path="/etc/flax/credentials-host.json",
               redfish_path="/etc/flax/credentials-redfish.json") -> Creds:
    """The vault-deployed logins (never in the repo): BMC pairs, the host flax row,
    the Redfish pairs. Same files and rows as rackpost's loader."""
    pairs = [(r["bmcuser"], r["bmcpass"]) for r in _rows(bmc_path)
             if r.get("bmcuser") and "bmcpass" in r]
    hrow = next((r for r in _rows(host_path) if r.get("user") == "flax" and "pass" in r), None)
    rf = [(r["rfuser"], r["rfpass"]) for r in _rows(redfish_path)
          if r.get("rfuser") and "rfpass" in r]
    return Creds(pairs, (hrow["user"], hrow["pass"]) if hrow else (), rf)


# ---------------------------------------------------------------- pure judges
_NODE_SETTING = re.compile(r"^\s*set\s+node-setting\s+(\S+)", re.M)
_DISK = re.compile(r"\bpath:\s*(/dev/[\w/.-]*\w)")


def node_setting(part_text: str) -> "str | None":
    m = _NODE_SETTING.search(part_text or "")
    return m.group(1) if m else None


def target_disk(user_data: str) -> "str | None":
    """The first `path: /dev/...` of the autoinstall's storage config: the system disk."""
    at = (user_data or "").find("storage:")
    m = _DISK.search(user_data or "", at if at >= 0 else 0)
    return m.group(1) if m else None


def has_system_storage(alldisks_text: "str | None", disk: str,
                       no_media: bool = False) -> "tuple[bool | None, str]":
    """(present, why) for `disk` (/dev/nvme0n1) in the run's `alldisks -v`. None when
    the inventory is missing: not measured, never read as 'no disk'. A captured but
    EMPTY inventory is what a diskless node gives (et10b4 2026-11-09); it means no
    disk only when the same run's Qualify found no physical media too (`no_media`:
    fio / smart-health skipped "no physical storage media", the agent's lsblk)."""
    if alldisks_text is None:
        return None, "no alldisks inventory in the Qualify run"
    if not alldisks_text.strip():
        if no_media:
            return False, f"no system storage (no physical media; {disk} absent)"
        return None, "empty alldisks inventory, and Qualify saw storage media"
    info = disk_info(alldisks_text)
    dev = disk.rsplit("/", 1)[-1]
    d = info.get(dev)
    if d is None:
        return False, f"no system storage ({disk} not in alldisks)"
    if (d.get("controller") or "") in USB_CONTROLLERS:
        return False, f"no system storage ({disk} is USB)"
    return True, f"{disk} {d.get('model') or ''} {d.get('size_gb') or ''} GB".strip()


_SDR_LINE = re.compile(r"^([^|]+?)\s*\|\s*[0-9A-Fa-f]+h\s*\|\s*(\w*)\s*\|")
_SDR_CRIT = ("cr", "nr", "lcr", "ucr", "lnr", "unr")
_SDR_QUIET = ("ok", "ns", "na", "")


def sdr_status(text: "str | None") -> dict:
    out = {}
    for ln in (text or "").splitlines():
        m = _SDR_LINE.match(ln)
        if m:
            out[m.group(1).strip()] = m.group(2).strip().lower()
    return out


def sdr_bad(before: "str | None", after: "str | None") -> list:
    """Sensors irregular after stress: any critical/non-recoverable status, or any
    other non-ok status (non-critical) that was ok/absent before. ns/na never count."""
    b, a = sdr_status(before), sdr_status(after)
    bad = []
    for name, st in a.items():
        if st in _SDR_QUIET:
            continue
        if st in _SDR_CRIT or b.get(name) in _SDR_QUIET or name not in b:
            bad.append(f"{name}: {b.get(name, '-')} -> {st}")
    return bad


# the label is dimmerr's silkscreen name and may hold spaces ("DIMM A0" on TiogaPass)
_DIMM = re.compile(r"^(\S[^\n]*?)\s+sn:\s*(\S*)\s+ce:\s*(\d+)\s+ue:\s*(\d+)", re.M)


def dimm_counts(text: "str | None") -> dict:
    return {m.group(1): (int(m.group(3)), int(m.group(4))) for m in _DIMM.finditer(text or "")}


def dimm_bad(before: "str | None", after: "str | None") -> list:
    """Any uncorrectable DIMM error (ue went up, or ue > 0 after), and correctable ones
    when their increase across stress reaches stress.CE_FAIL_AT in total (operator
    2026-11-06: 1, i.e. any correctable fails)."""
    b, a = dimm_counts(before), dimm_counts(after)
    bad, ce_up = [], []
    for d, (ce, ue) in sorted(a.items()):
        ce0, ue0 = b.get(d, (0, 0))
        if ue > ue0 or ue > 0:
            bad.append(f"{d}: ue {ue0}->{ue}")
        if ce > ce0:
            ce_up.append((d, ce0, ce))
    total = sum(ce - ce0 for _, ce0, ce in ce_up)
    if total >= stress.CE_FAIL_AT:
        bad += [f"{d}: ce {ce0}->{ce}" for d, ce0, ce in ce_up]
    return bad


def keep_install(node_vars: dict, serial: "str | None") -> "dict | None":
    """The node's earlier Post install when it is this blade's: post_node is keyed by
    BMC MAC, and the recorded serial must equal the current one (a moved card or a
    swapped blade never keeps a disk)."""
    d = (node_vars or {}).get("deployed") or {}
    return d if serial and d.get("serial") == serial else None


def wants_deploy(row: dict) -> "str | None":
    """'auto' when the slot's Qualify passed and waits for a Deploy that has not run
    for that run; the request mode when the operator asked for one after a failure;
    else None."""
    done = row.get("done") or {}
    if done.get("verdict") != "pass" or done.get("deploy") != "pending":
        return None
    run_id = (row.get("qual") or {}).get("run_id")
    dep = row.get("deploy") or {}
    if dep.get("run_id") != run_id:
        return "auto"
    req = row.get("deploy_request") or {}
    if dep.get("state") in ("fail", "interrupted") and req.get("mode") in MODES \
            and (req.get("at") or 0) > (dep.get("request_at") or 0):
        return req["mode"]
    return None


# ---------------------------------------------------------------- the engine
def art_stage(phase: str) -> str:
    """post_artifact stage of a Deploy phase: deploy-install, ..., deploy-iperf."""
    return phase if phase.startswith(STAGE + "-") else f"{STAGE}-{phase}"


def _now():
    return int(time.time())


class Node:
    """One node's Deploy. target: port, bmc_mac, serial, order_no, bmc_ip, host_ip,
    host_mac, hostname, family, run_id. store: set_state/get_node/upsert_node/
    write_artifact/get_artifact/update_result (flax_post.state or a fake)."""

    def __init__(self, target, mode, *, store, creds, cfg, prior=None, request_at=None,
                 runner=subprocess.run, sleep=time.sleep):
        self.t, self.mode, self.store, self.creds, self.cfg = target, mode, store, creds, cfg
        self.runner, self.sleep = runner, sleep
        prior = prior or {}
        self.d = {"state": "running", "run_id": target["run_id"], "mode": mode,
                  "serial": target.get("serial"), "bmc_mac": target.get("bmc_mac"),
                  "hostname": target.get("hostname"), "family": target.get("family"),
                  "platform": None, "disk": None, "kept": None, "phase": None,
                  "started_at": _now(), "ended_at": None, "verdict": None, "reason": None,
                  "request_at": request_at if request_at is not None else prior.get("request_at"),
                  "steps": {p: {"status": "pending"} for p in PHASES}, "tail": {}}
        # a re-run keeps what already passed in this run's earlier attempt on screen
        if mode == "rerun" and prior.get("run_id") == target["run_id"]:
            for p in PHASES:
                if (prior.get("steps") or {}).get(p, {}).get("status") == "pass":
                    self.d["steps"][p] = dict(prior["steps"][p], status="pass")

    # -- state
    def save(self):
        self.store.set_state(self.t["port"], deploy=self.d)

    def step(self, p, status, reason=None, **extra):
        s = self.d["steps"][p]
        if status == "running":
            s.update(status="running", started=_now(), ended=None, reason=None)
            self.d["phase"] = p
        else:
            s.update(status=status, ended=_now(), reason=reason, **extra)
        self.save()

    def art(self, phase, name, text):
        if not text:
            return
        try:
            self.store.write_artifact(self.t["bmc_mac"], self.t["run_id"], art_stage(phase),
                                      name, "raw", text, serial=self.t.get("serial"),
                                      order_no=self.t.get("order_no"))
        except Exception:   # noqa: BLE001 -- evidence loss must not strand the node
            pass

    def rung(self, p, r: RungResult) -> bool:
        for name, text in (r.artifacts or {}).items():
            self.art(p, name, text)
        det = r.detail or {}
        why = det.get("error") or det.get("headline") or det.get("reason")
        rows = det.get("rows")
        self.step(p, r.verdict, why if r.verdict != "pass" else det.get("headline"),
                  **({"rows": rows} if rows else {}))
        return r.verdict == "pass"

    def finish(self, verdict, reason=None):
        self.d.update(state=verdict, verdict=verdict, reason=reason, ended_at=_now(), phase=None)
        # no BMC boot override outlives Deploy: the node boots by its own BIOS order (spec
        # 2026-11-11 §5). Evidence only: a Leopard has no Redfish, P26F self-clears anyway.
        ov = redfish.clear_boot_override(self.t["bmc_ip"], self.creds.redfish, runner=self.runner)
        self.d["override_cleared"] = ("yes" if ov.verdict == "pass"
                                      else f"no ({ov.detail.get('error') or 'unknown'})")
        for p in PHASES:
            if self.d["steps"][p]["status"] in ("pending", "running"):
                self.d["steps"][p].update(status="skip", reason=reason if verdict != "pass" else None)
        if verdict in ("pass", "skip"):
            self.d["tail"] = self.tail()
        self.save()
        self.record()

    def tail(self) -> dict:
        """The Done tail: identify LED force-on, power off, read power back (host_qual's
        run_done, over flax_rungs.bmc)."""
        ip = self.t["bmc_ip"]
        idf = bmc.identify(ip, self.creds.bmc, "force", runner=self.runner)
        bmc.power_action(ip, self.creds.bmc, "off", runner=self.runner)
        reading = None
        for _ in range(6):
            reading = bmc.power_status(ip, self.creds.bmc, runner=self.runner)
            if reading == "off":
                break
            self.sleep(5)
        out = {"identify": "done" if (idf or {}).get("ok") else "fault",
               "power_off": "done" if reading == "off" else "fault"}
        if reading != "off":
            out["power_off_reason"] = "still_on" if reading == "on" else "unreadable"
        return out

    def record(self):
        """The run's durable record: result.deploy, and the run verdict follows Deploy
        (a Deploy fail is a function failure: not Done, not a Finish member)."""
        try:
            res = (self.store.get_node(self.t["bmc_mac"]) or {}).get("result") or {}
            if res.get("run_id") != self.t["run_id"]:
                return
            res = dict(res)
            res["deploy"] = {k: self.d.get(k) for k in
                             ("verdict", "reason", "mode", "kept", "platform", "disk",
                              "hostname", "started_at", "ended_at", "tail")}
            res["deploy"]["steps"] = {p: {"status": s.get("status"), "reason": s.get("reason")}
                                      for p, s in self.d["steps"].items()}
            res["verdict"] = "pass" if self.d["verdict"] in ("pass", "skip") else "fail"
            self.store.update_result(self.t["bmc_mac"], res)
        except Exception:   # noqa: BLE001
            pass

    # -- plan
    def _plan_fail(self, why) -> str:
        self.step("plan", "fail", why)
        self.finish("fail", why)
        return "fail"

    def plan(self) -> "str | None":
        """The `plan` rung: platform, autoinstall and system disk. None when Deploy goes
        on, else the verdict already finished (skip / fail)."""
        self.step("plan", "running")
        fam = self.t.get("family")
        plat = self.cfg.platforms.get(fam or "")
        if not plat:
            return self._plan_fail(f"no Deploy platform for family {fam or 'unknown'}")
        self.d["platform"] = plat
        try:
            ns = node_setting((Path(self.cfg.pxe_dir) / "templates" / f"{plat}.part").read_text())
            self.d["autoinstall"] = f"{ns}/user-data" if ns else None
            ud = (Path(self.cfg.install_root) / f"{ns}/user-data").read_text() if ns else ""
        except OSError as e:
            return self._plan_fail(f"platform {plat}: {e}")
        disk = target_disk(ud)
        if not disk:
            return self._plan_fail(f"{self.d['autoinstall']}: no storage path")
        self.d["disk"] = disk
        alld = self.store.get_artifact(self.t["bmc_mac"], self.t["run_id"], "inventory", "alldisks")
        ok, why = has_system_storage(alld, disk, bool(self.t.get("no_media")))
        if ok is None:
            return self._plan_fail(why)
        if not ok:
            self.step("plan", "pass", f"{why}: Deploy skipped")
            self.finish("skip", why)
            return "skip"
        self.d["storage"] = why
        self.step("plan", "pass", f"{plat} · {why}")
        return None

    # -- phases
    def do_install(self) -> bool:
        self.step("install", "running")
        r = install.install(self.t["bmc_ip"], self.t["host_ip"], self.t["hostname"], self.creds,
                            pxe_dir=self.cfg.pxe_dir, install_root=self.cfg.install_root,
                            platform=self.d["platform"], autoinstall=self.d["autoinstall"],
                            bootdev_persistent=False, runner=self.runner, sleep=self.sleep)
        return self.rung("install", r)

    def do_firstboot(self) -> bool:
        self.step("firstboot", "running")
        r = install.firstboot(self.t["bmc_ip"], self.t["host_ip"], self.t["hostname"], self.creds,
                              expect_mac=self.t.get("host_mac"), pxe_dir=self.cfg.pxe_dir,
                              bootdev_persistent=False, family=self.t.get("family") or "",
                              runner=self.runner, sleep=self.sleep)
        ok = self.rung("firstboot", r)
        if ok:
            prev = keep_install(self.store.get_node(self.t["bmc_mac"]), self.t.get("serial")) or {}
            self.store.upsert_node(self.t["bmc_mac"], deployed={
                "serial": self.t.get("serial"), "run_id": self.t["run_id"],
                "platform": self.d["platform"], "hostname": self.t.get("hostname"),
                "installed_at": prev.get("installed_at") if self.d["kept"] else _now(),
                "booted_at": _now()})
        return ok

    def do_user(self) -> bool:
        self.step("flax-user", "running")
        if not self.creds.host:
            return self.rung("flax-user", RungResult("fail", {"error": "add credentials-host.json (flax row)"}))
        user, pw = self.creds.host
        work = Path(self.cfg.workdir) / self.t["port"]
        out = ansible.flax_user({self.t["port"]: self.t["host_ip"]}, user, pw, work,
                                runner=self.runner)
        return self.rung("flax-user", out.get(self.t["port"]) or RungResult("fail", {"error": "no flax_user result"}))

    def do_iperf(self) -> bool:
        self.step("deploy-iperf", "running")
        return self.rung("deploy-iperf", self.iperf())

    def iperf(self) -> RungResult:
        user, pw = self.creds.host
        ip = self.t["host_ip"]
        prep = ("command -v iperf3 >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y "
                "-q iperf3 >/dev/null 2>&1; command -v iperf3 >/dev/null || echo NOIPERF\n"
                f"ip -o -4 addr | awk '$4 ~ /^{re.escape(ip)}\\// {{print \"IF \" $2}}'\n")
        rc, out, err = hostssh.run(ip, user, pw, prep, timeout=300, runner=self.runner)
        if unreachable(rc):
            return RungResult(UNMEASURED, {"error": f"no ssh to {ip} (rc={rc})"})
        if "NOIPERF" in out:
            return RungResult("fail", {"error": "iperf3 not installed (apt-get install iperf3 failed)"})
        m = re.search(r"^IF (\S+)", out, re.M)
        if not m:
            return RungResult("fail", {"error": f"no interface holds {ip}"})
        ifname = m.group(1)
        client = {"host_ip": ip}
        speed = iperf_rung._speed(client, ifname, user, pw, self.runner)
        res, raw, errs = {}, {}, []
        for d, rev in (("tx", False), ("rx", True)):
            bps, why, j = None, "", ""
            for _ in range(IPERF_BUSY_TRIES):
                bps, why, j = iperf_rung._bps(client, ip, self.cfg.iperf_server, self.cfg.iperf_port,
                                              rev, IPERF_SECONDS, IPERF_STREAMS, user, pw, self.runner)
                if bps is not None or "busy" not in (why or ""):
                    break
                self.sleep(IPERF_BUSY_SLEEP_S)
            res[d] = bps
            if j:
                raw[f"iperf-mgmt-{d}"] = j
            if bps is None:
                errs.append(f"{d}: {why}")
        floor = iperf_rung.THRESHOLD * speed if speed else None
        if not speed:
            errs.append(f"no link speed from ethtool {ifname}")
        for d in ("tx", "rx"):
            if floor and res[d] is not None and res[d] < floor:
                errs.append(f"{d} {round(res[d] / 1e6)} Mbit/s < {round(floor / 1e6)} "
                            f"(0.8 x {round(speed / 1e6)})")
        det = {"server": self.cfg.iperf_server, "iface": ifname, "speed_bps": speed,
               "tx_bps": res["tx"], "rx_bps": res["rx"],
               "rows": [["server", f"{self.cfg.iperf_server}:{self.cfg.iperf_port}"],
                        ["link", f"{ifname} {speed and round(speed / 1e6)} Mbit/s"],
                        ["tx", f"{res['tx'] and round(res['tx'] / 1e6)} Mbit/s"],
                        ["rx", f"{res['rx'] and round(res['rx'] / 1e6)} Mbit/s"]]}
        if errs:
            det["error"] = "; ".join(errs)
            verdict = UNMEASURED if res["tx"] is None and res["rx"] is None else "fail"
            return RungResult(verdict, det, raw)
        det["headline"] = f"tx {round(res['tx'] / 1e6)} / rx {round(res['rx'] / 1e6)} Mbit/s"
        return RungResult("pass", det, raw)

    def dimmerr(self) -> "str | None":
        user, pw = self.creds.host
        try:
            script = Path(self.cfg.dimmerr).read_text()
        except OSError:
            return None
        rc, out, _ = hostssh.run(self.t["host_ip"], user, pw, script, timeout=60, sudo=False,
                                 runner=self.runner)
        return out if rc == 0 else None

    def do_stress(self) -> bool:
        self.step("deploy-stress", "running")
        user, pw = self.creds.host
        before = self.dimmerr()
        node = {"pos": self.t["port"], "host_ip": self.t["host_ip"], "bmc_ip": self.t["bmc_ip"]}
        r = stress.run([node], user=user, pw=pw, bmc_creds=self.creds.bmc, runner=self.runner,
                       sleep=self.sleep)[self.t["port"]]
        after = self.dimmerr()
        arts = dict(r.artifacts or {})
        det = dict(r.detail or {})
        probs = [det["error"]] if r.verdict != "pass" and det.get("error") else []
        sdr = sdr_bad(arts.get("sdr-elist-before.txt"), arts.get("sdr-elist-after.txt"))
        if arts.get("sdr-elist-after.txt") is None:
            probs.append("SDR after not read")
        if sdr:
            probs.append("SDR: " + "; ".join(sdr))
        if before is None or after is None or not dimm_counts(after):
            probs.append("dimmerr not read (no EDAC DIMM counts)")
        else:
            arts["dimmerr-before.txt"], arts["dimmerr-after.txt"] = before, after
            dimm = dimm_bad(before, after)
            if dimm:
                probs.append("dimmerr: " + "; ".join(dimm))
        # rasdaemon's memory errors named by slot: EDAC's counters can stay 0 for an
        # error the kernel's CE collector kept (et7b4 2026-11-10), the MCE still has it
        mce = mcedimm.name(det.get("mem_errors") or [], after or before)
        if mce:
            probs.append("memory errors at " + "; ".join(mce))
        rows = list(det.get("rows") or [])
        rows += [["SDR", "irregular: " + "; ".join(sdr) if sdr else "clean"],
                 ["dimmerr", f"{len(dimm_counts(after))} DIMMs" if after else "not read"]]
        rows += [["memory error DIMM", ln] for ln in mce]
        det["rows"] = rows
        verdict = r.verdict
        if probs:
            det["error"] = "; ".join(probs)
            if verdict == "pass":
                verdict = UNMEASURED if all("not read" in p for p in probs) else "fail"
        return self.rung("deploy-stress", RungResult(verdict, det, arts))

    # -- the run
    def run(self):
        self.save()
        try:
            if self.plan() is not None:
                return
            prior_install = keep_install(self.store.get_node(self.t["bmc_mac"]), self.t.get("serial"))
            keep = self.mode != "reinstall" and prior_install is not None
            self.d["kept"] = keep
            if keep:
                when = time.strftime("%Y-%m-%d", time.gmtime(prior_install.get("installed_at") or 0))
                self.step("install", "skip", f"kept: installed by post {when}")
            elif not self.do_install():
                return self.finish("fail", "failed in phase install")
            if not self.do_firstboot():
                if keep and self.mode == "auto":
                    # the kept install did not boot: wipe and install after all, once
                    self.d["kept"] = False
                    if not self.do_install() or not self.do_firstboot():
                        return self.finish("fail", "failed in phase " + self.d["phase"])
                else:
                    return self.finish("fail", "failed in phase firstboot")
            for p, fn in (("flax-user", self.do_user), ("deploy-iperf", self.do_iperf),
                          ("deploy-stress", self.do_stress)):
                if self.mode == "rerun" and self.d["steps"][p]["status"] == "pass" and p != "deploy-stress":
                    continue
                if not fn():
                    return self.finish("fail", f"failed in phase {p}")
            self.finish("pass")
        except Exception as e:      # noqa: BLE001 -- a crash never leaves the node "running"
            try:
                pxeboot.remove(self.t["hostname"], pxe_dir=self.cfg.pxe_dir)
            except Exception:       # noqa: BLE001
                pass
            self.finish("fail", f"{type(e).__name__}: {e}")
