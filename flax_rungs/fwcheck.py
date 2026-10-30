"""Detect-only firmware reads for rackpost's Firmware stage: loose-node Post's
fwd/biosd/nicd probes, run once per device, mapped to Post's 6 steps
(bmc/bios/mlx x checked/updated) with Post's evidence (stepinfo _fw_rows +
_PHASE_MEANING, copied here: stepinfo imports blades, which needs psycopg).

Read-only: only the daemons' probe/version functions are called --
  - BMC: flax_post.fwd.flasher.probe_one (product, version, OEM/stock root); the row
    it writes lands in a local dict (our set_row);
  - BIOS: biosd's probe sequence (dmidecode -t system -> family map -> manifest
    entry -> driver.check_script = dmidecode -t bios -> classify);
  - Mellanox: nicd's QUERY_SCRIPT (lspci + mstflint query + mstconfig query) ->
    parse_cards -> classify_device per card -> aggregate.
The manifests and family map are the loose-node daemons' own /etc/flax files.

Each read returns (slice, artifacts): the slice is the row the Post daemon would
have written (phase, current/target, fault_reason; nic: devices), artifacts the raw
outputs. steps() turns a slice into the (checked, updated) RungResults.

On an installed OS (no live image) BIOS is the same dmidecode read, and the Mellanox
ports are read by nic_installed: `ethtool -i` firmware-version "<ver> (<PSID>)" per
port, the ports picked BY MAC (the mezz = Discover's host MAC, the fabric ports =
Deploy's bond MACs). ethtool has no UEFI ROM bit: it is reported "not read", never
judged (only version + PSID are compared).

The backup BMC (bmc-backup-checked, rackpost's own step beside Post's six): a
TiogaPass's backup chip via `bmc-backup-flash version` (bmc_backup; read-only, never
its write/verify/netreset), an IOM's fw-util `ROM Version` (iom_rom). Report only: a
version read passes, an unread one is `unmeasured` and carries no version (never the
active one in its place).

A slice whose phase is `unreachable` (the BMC's Redfish or the host's ssh did not
answer: nothing was read) makes BOTH steps `unmeasured`; detail.state keeps Post's own
state (fwsteps, equal to Post's tables), so only the verdict is rackpost's."""
import json
import os
import re
import subprocess
from types import SimpleNamespace

from flax_post.biosd import classify as bios_classify
from flax_post.biosd import driver as bios_driver
from flax_post.biosd import manifest as bios_manifest
from flax_post.fwd import flasher
from flax_post.fwd import manifest as bmc_manifest
from flax_post.fwd import redfish as fwd_redfish
from flax_post.nicd import classify as nic_classify
from flax_post.nicd import driver as nic_driver
from flax_post.nicd import manifest as nic_manifest

from . import fwsteps, hostssh, remote
from .result import UNMEASURED, RungResult

CONFIG_DIR = os.environ.get("FLAX_CONFIG_DIR", "/etc/flax")     # = fwd/biosd/nicd config
DMI_SCRIPT = "dmidecode -t system"                               # = biosd.service.DMI_SCRIPT
MANIFESTS = {"bmc": "bmc-firmware-versions.json", "bios": "bios-firmware-versions.json",
             "nic": "nic-firmware-versions.json"}
HOST_TIMEOUT_S = 120

# Post state (done/cur/pending/fault) -> rackpost verdict. `cur` is "not there yet":
# version unread (unreachable) or off target (needs_update; triage flashes) -- a
# finished detect-only stage reports it as a fail; detail.state keeps Post's colour.
VERDICT = {"done": "pass", "fault": "fail", "cur": "fail", "pending": "skip", "skip": "skip"}
UNREACHABLE = "unreachable"     # the phase whose steps are `unmeasured`


# ---------------------------------------------------------------- Post's evidence (copies)
def _fw_rows(slice_, frozen_state):
    """= flax_post.stepinfo._fw_rows (parity-tested)."""
    s = slice_ or {}
    rows = [["current", s.get("current") or s.get("current_version") or "not read"],
            ["target", s.get("target") or s.get("target_version") or "unknown"],
            ["daemon phase", s.get("phase") or "not evaluated"],
            ["mode", s.get("mode") or "detect"]]
    if s.get("fault_reason"):
        rows.append(["reason", s["fault_reason"]])
    return rows


_PHASE_MEANING = {      # = flax_post.stepinfo._PHASE_MEANING (parity-tested)
    "up_to_date": "current version equals the manifest target: nothing to flash",
    "done": "flashed this run and re-read at the target version",
    "needs_update": "current version differs from the target; in detect mode nothing is flashed, so this step stays open until triage flashes it",
    "checking": "version read in progress",
    "flashing": "flash in progress", "monitoring": "flash in progress (monitoring)",
    "activating": "new image activating (BMC reboot)",
    "fault": "the daemon recorded a fault (see reason)",
    "unreachable": "the daemon could not reach the device on its last pass (BMC off the network, or the host is off)",
    "unknown": "version unreadable on either side",
    "oem": "OEM board: firmware is not flax-managed",
    "unsupported": "platform not in the manifest: nothing to compare against",
}


# ---------------------------------------------------------------- manifests
def load_manifests(config_dir: str = CONFIG_DIR, family_map=None) -> SimpleNamespace:
    """The three loose-node matchers, built exactly as the daemons' __main__ build
    them (fwd: PostMatcher; biosd: BiosMatcher over the live family map; nicd:
    NicMatcher), plus which manifest files were readable."""
    bmc = bmc_manifest.load_manifest(config_dir)
    bios = bios_manifest.load_bios_manifest(config_dir)
    nic = nic_manifest.load_nic_manifest(config_dir)
    bm = (bios_manifest.BiosMatcher(bios) if family_map is None
          else bios_manifest.BiosMatcher(bios, family_map=family_map))
    return SimpleNamespace(
        bmc=bmc_manifest.PostMatcher(bmc), bios=bm, nic=nic_manifest.NicMatcher(nic),
        sources={k: (os.path.join(config_dir, f), bool(x))
                 for (k, f), x in zip(MANIFESTS.items(), (bmc, bios, nic))})


# ---------------------------------------------------------------- BMC (TiogaPass Redfish)
class LoggedRedfish:
    """A fwd RedfishClient whose calls (and their (value, detail)) are logged for the
    artifact."""

    def __init__(self, client):
        self._client = client
        self.log: list[str] = []

    def __getattr__(self, name):
        fn = getattr(self._client, name)

        def call(*a):
            v = fn(*a)
            self.log.append(f"{name}({', '.join(map(repr, a))}) -> {v!r}")
            return v
        return call


def tp_bmc(bmc_ip, creds, matcher, *, client_factory=fwd_redfish.RedfishClient):
    """fwd's probe_one against one TiogaPass BMC (Redfish basic auth with the
    credentials-bmc.json pairs, as flax-post-fwd). -> (slice, artifacts). A BMC whose
    firmware inventory does not answer is probe_one's own `unreachable` (unmeasured)."""
    row: dict = {}

    def set_row(_port, **f):
        row.update(f)

    client = LoggedRedfish(client_factory(
        bmc_ip, [{"bmcuser": u, "bmcpass": p} for u, p in creds]))
    phase = flasher.probe_one(bmc_ip, client, matcher, set_row)
    return {**row, "phase": phase, "bmc_ip": bmc_ip}, {"redfish.txt": "\n".join(client.log) + "\n"}


def report_only(r: RungResult):
    """A Facebook-OpenBMC version read (identity.iom / identity.wedge_bmc) as a fw
    slice: versions read -> `unsupported` (no manifest: report only), else
    `unreachable` -- measured (fail) unless the read itself was unmeasured (the BMC
    did not answer ssh): a missing login or a failing command is a real failure.
    -> (slice, artifacts)."""
    d = r.detail or {}
    versions = d.get("versions") or {}
    if versions:
        sl = {"phase": "unsupported", "versions": versions,
              "not_present": d.get("not_present") or {}, "errors": d.get("errors") or {},
              "current": versions.get("BMC Version") or versions.get("OpenBMC"),
              "fault_reason": ""}
    else:
        sl = {"phase": UNREACHABLE, "versions": {}, "ssh_unreachable": r.verdict == UNMEASURED,
              "fault_reason": d.get("error") or d.get("headline") or "versions not read"}
    return sl, dict(r.artifacts or {})


# ---------------------------------------------------------------- host (live image)
def _sh(run, host_ip, user, pw, arts, name, script, runner):
    kw = {} if runner is None else {"runner": runner}
    rc, out, err = run(host_ip, user, pw, script, timeout=HOST_TIMEOUT_S, **kw)
    arts[name] = out + (err and f"\n--- stderr ---\n{err}")
    return rc, out, err


def bios(host_ip, user, pw, matcher, *, run=hostssh.run, runner=None):
    """biosd's probe for one live host (= biosd.service._probe_host). -> (slice, arts)."""
    arts: dict = {}
    sh = lambda name, script: _sh(run, host_ip, user, pw, arts, name, script, runner)
    rc, dmi, err = sh("dmidecode-system", DMI_SCRIPT)
    product = bios_manifest.dmi_product(dmi)
    if rc != 0:
        return {"phase": "unreachable", "current": None, "target": None,
                "fault_reason": "", "error": hostssh.tail(dmi, err, 200)}, arts
    entry = matcher.match(dmi)
    if entry is None:
        # no expected string to compare against is a FAILED check (biosd ruling 2026-10-06)
        _rc, out, _ = sh("dmidecode-bios", bios_driver.BIOS_SCRIPT + "\n")
        return {"phase": "fault", "current": bios_driver.parse_bios_version(out),
                "target": None, "product": product,
                "fault_reason": "platform not in the manifest: DMI product %r"
                % (product or "unread")}, arts
    _rc, out, _ = sh("dmidecode-bios", bios_driver.check_script(entry))
    current = bios_driver.parse_bios_version(out)
    return {"phase": bios_classify.classify(current, entry["target"]), "current": current,
            "target": entry["target"], "family": entry.get("family"), "product": product,
            "fault_reason": ""}, arts


def nic(host_ip, user, pw, matcher, *, run=hostssh.run, runner=None):
    """nicd's probe for one live host (= nicd.service._classify_row). -> (slice, arts)."""
    arts: dict = {}
    rc, out, err = _sh(run, host_ip, user, pw, arts, "mstflint-query", nic_driver.QUERY_SCRIPT,
                       runner)
    if rc != 0:
        return {"phase": "unreachable", "devices": [], "fault_reason": "",
                "error": hostssh.tail(out, err, 200)}, arts
    cards = nic_driver.parse_cards(out)
    devs = [nic_classify.classify_device(c, matcher.match(c["psid"])) for c in cards]
    _chk, _upd, roll = nic_classify.aggregate(devs)
    return {"phase": roll, "devices": devs, "fault_reason": ""}, arts


# ---------------------------------------------------------------- host (installed OS)
_UEFI_UNREAD = "not read (installed OS: ethtool has no UEFI bit)"
ETHTOOL_SCRIPT = r"""for i in /sys/class/net/*; do
  n=${i##*/}; [ -e "$i/device" ] || continue
  echo "===IF $n==="
  echo "address: $(cat "$i/address")"
  ethtool -P "$n" 2>/dev/null
  ethtool -i "$n" 2>/dev/null
done
"""
_FWVER = re.compile(r"^(\S+)\s*\((\S+)\)")


def _hex(mac) -> str:
    m = re.sub(r"[^0-9a-f]", "", (mac or "").lower())
    return m if len(m) == 12 else ""


def parse_ethtool(out: str) -> list[dict]:
    """ETHTOOL_SCRIPT output -> [{ifname, mac (permanent, else current; hex12), driver,
    current, psid, pci}] per PCI net port. firmware-version "14.32.1912 (MT_2420110034)"
    = version + PSID (Mellanox); another shape keeps the whole string, psid None."""
    out_ = []
    for m in re.finditer(r"===IF (\S+)===\n(.*?)(?=\n===IF |\Z)", out or "", re.DOTALL):
        f = {}
        for line in m.group(2).splitlines():
            k, sep, v = line.partition(":")
            if sep and k.strip() not in f:
                f[k.strip()] = v.strip()
        fw = f.get("firmware-version") or ""
        fm = _FWVER.match(fw)
        out_.append({"ifname": m.group(1),
                     "mac": _hex(f.get("Permanent address")) or _hex(f.get("address")),
                     "driver": f.get("driver"), "current": fm.group(1) if fm else (fw or None),
                     "psid": fm.group(2) if fm else None,
                     "pci": (f.get("bus-info") or "").removeprefix("0000:") or m.group(1)})
    return out_


def nic_installed(host_ip, user, pw, matcher, macs: dict, *, run=hostssh.run, runner=None):
    """The Mellanox ports of an installed OS (no mstflint there): ethtool -i per port,
    the ports whose permanent MAC is in `macs` ({label: mac}: mezz + fabric A/B), each
    classified like nicd's classify_device on version + PSID (UEFI not read, so not
    judged). A wanted MAC on no port is a fault. -> (slice, arts)."""
    arts: dict = {}
    rc, out, err = _sh(run, host_ip, user, pw, arts, "ethtool-i", ETHTOOL_SCRIPT, runner)
    src = "installed OS: ethtool -i (ssh flax, sudo -n)"
    if rc != 0:
        return {"phase": "unreachable", "devices": [], "fault_reason": "", "source": src,
                "ssh_unreachable": remote.unreachable(rc),
                "error": hostssh.tail(out, err, 200)}, arts
    ports = {p["mac"]: p for p in parse_ethtool(out) if p["mac"]}
    devs, missing = [], []
    for label, mac in macs.items():
        p = ports.get(_hex(mac))
        if p is None:
            missing.append(f"{label} {mac}")
            continue
        # ethtool has no UEFI ROM bit: judged on version + PSID only, UEFI reported unread
        d = nic_classify.classify_device(
            {"pci": p["pci"], "current": p["current"], "psid": p["psid"], "secure": False,
             "uefi": True}, matcher.match(p["psid"]) if p["psid"] else None)
        devs.append({**d, "uefi": None, "uefi_unread": True, "port": f"{label} {p['ifname']}"})
    _chk, _upd, roll = nic_classify.aggregate(devs)
    if missing:
        return {"phase": "fault", "devices": devs, "source": src,
                "fault_reason": "no port with MAC " + ", ".join(missing)}, arts
    return {"phase": roll, "devices": devs, "fault_reason": "", "source": src}, arts


# ---------------------------------------------------------------- backup BMC
BACKUP_FLASH = "/opt/flax/bin/bmc-backup-flash"
BACKUP_TIMEOUT_S = 90


def _backup_ev(version, headline, rows, notes, arts, **extra) -> RungResult:
    """A backup read: pass with the version, else unmeasured with backup_version None."""
    d = {"headline": headline, "rows": [[k, str(v)] for k, v in rows if v not in (None, "")],
         "notes": list(notes), "backup_version": version, **extra}
    if not version:
        d["reason"] = headline
        return RungResult(UNMEASURED, d, arts)
    return RungResult("pass", d, arts)


def bmc_backup(bmc_ip, *, runner=subprocess.run, timeout=BACKUP_TIMEOUT_S) -> RungResult:
    """The TiogaPass backup BMC chip's image version: `bmc-backup-flash version <ip>`
    (one JSON line {backup_version, active_version, net_file, net_pin, error?}; the
    bin logs in with the vaulted BMC credentials itself). Only `version`: never its
    write/verify/netreset. backup_version null or an error = unmeasured."""
    argv = [BACKUP_FLASH, "version", bmc_ip]
    try:
        p = runner(argv, capture_output=True, text=True, timeout=timeout)
        rc, out, err = p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        rc, out, err = 124, "", f"timeout after {timeout}s"
    except OSError as e:
        rc, out, err = 127, "", str(e)
    arts = {"bmc-backup-flash.txt": out + (err and f"\n--- stderr ---\n{err}")}
    line = next((x for x in reversed(out.splitlines()) if x.strip().startswith("{")), "")
    try:
        j = json.loads(line) if line else {}
    except ValueError:
        j = {}
    if not isinstance(j, dict):
        j = {}
    bv, av = j.get("backup_version"), j.get("active_version")
    why = j.get("error") or ("" if bv else hostssh.tail(out, err, 200) or f"rc={rc}")
    notes = [f"read with `{BACKUP_FLASH} version {bmc_ip}` (read-only)",
             "report only: no manifest for the backup chip"]
    rows = [["backup", bv or "not read"], ["active", av], ["net file", j.get("net_file")],
            ["net pin", j.get("net_pin")], ["error", j.get("error")], ["BMC IP", bmc_ip]]
    if not bv or j.get("error") or rc != 0:
        return _backup_ev(None, f"backup BMC not read: {why or f'rc={rc}'}", rows, notes,
                          arts, active_version=av)
    return _backup_ev(bv, f"backup BMC {bv}" + ("" if bv == av else f" (active {av})"),
                      rows, notes, arts, active_version=av)


def iom_rom(r: RungResult) -> RungResult:
    """An IOM's backup version = fw-util all's `ROM Version` (fbttn), from the same
    identity.iom read the bmc-checked step used. Not in the read = unmeasured."""
    d = r.detail or {}
    rom = (d.get("versions") or {}).get("ROM Version")
    notes = ["the IOM's ROM Version from `fw-util all --version` (report only)"]
    if rom:
        return _backup_ev(rom, f"ROM {rom}", [["ROM Version", rom],
                                              ["BMC Version", (d.get("versions") or {})
                                               .get("BMC Version")]], notes, {},
                          active_version=(d.get("versions") or {}).get("BMC Version"))
    why = (d.get("not_present") or {}).get("ROM Version")
    why = f"not present ({why})" if why else (d.get("error") or d.get("headline")
                                             or "not in fw-util's output")
    return _backup_ev(None, f"ROM Version not read: {why}", [["ROM Version", "not read"]],
                      notes, {})


# ---------------------------------------------------------------- slice -> steps
def _ev(state, headline, rows, notes, arts=None, verdict=None, **extra) -> RungResult:
    verdict = verdict or VERDICT[state]
    d = {"headline": headline, "rows": [[k, str(v)] for k, v in rows if v not in (None, "")],
         "notes": list(notes), "state": state, **extra}
    if verdict == "fail":
        d.setdefault("error", headline)
    if verdict in ("skip", UNMEASURED):
        d.setdefault("reason", headline)
    return RungResult(verdict, d, arts or {})


def skipped(why: str, na: bool = False) -> tuple[RungResult, RungResult]:
    """Both rows of a pair skipped; na: the reader does not apply to this endpoint."""
    r = RungResult("skip", {"headline": why, "reason": why, "rows": [], "notes": [], "na": na})
    return r, r


_LABEL = {"bmc": "BMC", "bios": "BIOS", "nic": "Mellanox"}


def _checked_head(key, sl):
    ph = sl.get("phase")
    if key == "nic":
        devs = sl.get("devices") or []
        if ph == "unreachable":
            return "host unreachable: cards not queried"
        if not devs:
            return "no Mellanox card"
        return f"{len(devs)} card(s): " + ", ".join(
            f"{d.get('psid', '?')} {d.get('current') or '?'}" for d in devs)
    cur = sl.get("current") or sl.get("current_version")
    if ph == "unreachable":
        why = sl.get("fault_reason") or sl.get("error") or ""
        return f"{_LABEL[key]} unreachable" + (f": {why}" if why else "")
    if ph == "fault" and not cur:
        return sl.get("fault_reason") or "fault"
    return f"{_LABEL[key]} {cur or 'not read'}"


def _nic_why(d):
    """What keeps one card off nicd's up_to_date (version AND PSID AND UEFI on)."""
    why = []
    if d.get("current") != d.get("target"):
        why.append(f"{d.get('current') or '?'} -> {d.get('target')}")
    if d.get("psid") != d.get("target_psid"):
        why.append(f"PSID {d.get('psid')} -> {d.get('target_psid')}")
    if d.get("uefi") is not True and not d.get("uefi_unread"):
        why.append("UEFI ROM " + ("off" if d.get("uefi") is False else "not exposed"))
    return ", ".join(why) or "off target"


def _updated_head(key, sl, state):
    ph = sl.get("phase")
    cur = sl.get("current") or sl.get("current_version")
    tgt = sl.get("target") or sl.get("target_version")
    if state == "pending":
        return "not reached: the version was not read"
    if ph == "up_to_date":
        if key == "nic":
            return ("every card at target (version, PSID, UEFI ROM on)" if sl.get("devices")
                    else "no card to update")
        return f"at target {tgt}"
    if ph == "needs_update":
        if key == "nic":
            off = [f"{d.get('pci')}: {_nic_why(d)}" for d in sl.get("devices") or []
                   if d.get("phase") == "needs_update"]
            return f"needs update: {'; '.join(off)} (detect only: triage flashes)"
        return f"needs update: {cur} -> {tgt} (detect only: triage flashes)"
    if ph == "oem":
        return "OEM board: firmware is not flax-managed"
    if ph == "unsupported":
        if key == "bmc":
            return "report only: no firmware manifest for this BMC"
        if key == "nic":
            bad = [d.get("psid", "?") for d in sl.get("devices") or [] if d.get("phase") == "unsupported"]
            return f"PSID not in the manifest: {', '.join(bad)}"
        return "platform not in the manifest"
    if ph == "fault":
        if key == "nic":
            bad = [f"{d.get('pci')}: {d.get('fault_reason')}" for d in sl.get("devices") or []
                   if d.get("phase") == "fault"]
            return "; ".join(bad) or sl.get("fault_reason") or "fault"
        return sl.get("fault_reason") or "fault"
    return f"phase {ph or 'not evaluated'}"


def _rows(key, sl, sources):
    if key == "nic":       # = stepinfo._firmware's nic rows
        devs = sl.get("devices") or []
        rows = [["daemon phase", sl.get("phase") or "not evaluated"], ["mode", "detect"],
                ["cards", str(len(devs))]]
        for d in devs:
            rows.append(["card %s" % d.get("pci", "?"),
                         "%s  %s -> %s  [%s]" % (d.get("psid", "?"), d.get("current") or "?",
                                               d.get("target") or "?", d.get("phase") or "?")])
            rows.append(["card %s UEFI" % d.get("pci", "?"), _UEFI_UNREAD if d.get("uefi_unread")
                         else {True: "on", False: "off", None: "not exposed"}[d.get("uefi")]])
        if sl.get("fault_reason"):
            rows.append(["reason", sl["fault_reason"]])
    else:
        rows = _fw_rows(sl, None)
    for k, v in (sl.get("versions") or {}).items():
        rows.append([k, v])
    for k, why in (sl.get("not_present") or {}).items():
        rows.append([k, f"not present ({why})"])
    if sl.get("source"):
        rows.append(["source", sl["source"]])
    for k in ("bmc_ip", "product", "family"):
        if sl.get(k):
            rows.append([{"bmc_ip": "BMC IP", "product": "DMI product", "family": "family"}[k],
                         sl[k]])
    if sl.get("error"):
        rows.append(["error", sl["error"]])
    src = (sources or {}).get(key)
    if src and sl.get("phase") not in (None, "unreachable") and not sl.get("versions"):
        rows.append(["manifest", src[0] + ("" if src[1] else " (missing or unreadable)")])
    return rows


def steps(key: str, sl: dict, arts: dict | None = None, sources=None,
          notes=()) -> tuple[RungResult, RungResult]:
    """(<x>-checked, <x>-updated) for a slice, key bmc|bios|nic, with Post's states
    (fwsteps) and evidence. The raw outputs ride on the checked step."""
    if key == "bmc":
        chk, upd = fwsteps.bmc_steps(sl.get("phase"))
    elif key == "bios":
        chk, upd = fwsteps.bios_steps(sl.get("phase"))
    else:
        chk, upd = fwsteps.nic_steps({"fw_nic": sl})
    rows = _rows(key, sl, sources)
    nts = []
    meaning = _PHASE_MEANING.get(sl.get("phase"))
    if meaning:
        nts.append(meaning)
    nts += list(notes)
    nts.append("detect only: rackpost reads versions and never flashes")
    extra = {"phase": sl.get("phase"), "slice": sl}
    head = _checked_head(key, sl)
    if measured(sl):
        return (_ev(chk, head, rows, nts, arts, **extra),
                _ev(upd, _updated_head(key, sl, upd), rows, nts, **extra))
    return (_ev(chk, head, rows, nts, arts, UNMEASURED, **extra),
            _ev(upd, "not measured: " + head, rows, nts, verdict=UNMEASURED, **extra))


def measured(sl: dict) -> bool:
    """False when the slice is `unreachable` (Post's phase: the BMC's Redfish or the
    host's read did not answer): its steps are `unmeasured`. Rackpost's own slices set
    ssh_unreachable False when the device answered but the read failed (a real fail)."""
    return not (sl.get("phase") == UNREACHABLE and sl.get("ssh_unreachable", True))
