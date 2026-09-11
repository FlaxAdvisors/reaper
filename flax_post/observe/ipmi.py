# flax_post/observe/ipmi.py
"""Owned IPMI producer: serial(FRU) · power · HSC watts · SDR · SEL + liveness ping.

Mirrors flax_observe.ipmi / flax_observe.bmc_probe (kept flax_post-self-contained
per the no-cross-import rule, like flax_post/fwd/creds.py). One pass writes the
LIVE post_state[port] and upserts the DURABLE post_node[bmc_mac], stamping the
active order. The sole IPMI toucher of post BMCs (docs/Post-UI-Design.md §3.2).
"""
import json
import logging
import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

from .. import queries, records, state
from ..fwd import creds as _creds

log = logging.getLogger("flax-post.ipmi")

# Fallback default ONLY (no geometry). The producers derive the live post switch
# from geometry via _post_switch() -- braintree rabbit-lorax vs eindhoven
# rabbit-edam. Hardcoding this constant as the filter/writer switch (the pre-fix
# bug) skipped every braintree BMC (they live on rabbit-lorax), so the IPMI lane
# wrote no bmc_pinged/power/serial and the post UI froze in Discover at
# bmc-pinged. Mirrors the blades.post_switch fix (viewer) on the PRODUCER side.
SWITCH = "rabbit-edam"


def _post_switch():
    """The site's post rack switch, derived from geometry (blades.post_switch):
    braintree rabbit-lorax, eindhoven rabbit-edam. Falls back to SWITCH when no
    geometry rack is declared (or the file is absent, e.g. unit tests). Local
    import avoids a module-load cycle (blades imports geometry/state)."""
    from .. import blades, geometry
    try:
        return blades.post_switch(geometry.load_geometry())
    except Exception:
        return SWITCH


IPMITOOL_TIMEOUT_SECS = 15
# Power is read on a separate FAST lane (run_power_once) with a short timeout, so a
# dead/slow BMC can't stall the cheap power read behind the heavy serial/SDR/SEL pass.
POWER_TIMEOUT_SECS = int(os.environ.get("FLAX_POST_POWER_TIMEOUT", "4"))
# `sdr` alone measured 10-14s on this fleet's Tioga Pass BMCs, essentially the whole
# of IPMITOOL_TIMEOUT_SECS -- combining it with `power status` in one session (below)
# still needs more room than a single generic call, so this gets its own wider budget.
SDR_TIMEOUT_SECS = int(os.environ.get("FLAX_POST_SDR_TIMEOUT", "25"))
# Fan-out: one IPMI session per BMC is independent, so probe them concurrently.
DEFAULT_WORKERS = int(os.environ.get("FLAX_POST_OBSERVE_WORKERS", "48"))

# Post-BMC IPMI credentials: the same list-of-{bmcuser,bmcpass} that Triage uses
# (/etc/flax/credentials-bmc.json), NOT the Redfish creds the fwd driver reads.
BMC_CREDS_PATH = os.environ.get("FLAX_POST_BMC_CREDS", "/etc/flax/credentials-bmc.json")
# Redfish credentials for the IPMI-fallback path: the AMI OEM boards (Redfish-only,
# no IPMI) authenticate with credentials-redfish.json (rfuser/rfpass = Administrator),
# NOT the USERID credentials-bmc.json. Absent/empty -> no fallback (eindhoven, whose
# post boards all answer IPMI, never mounts it).
REDFISH_CREDS_PATH = os.environ.get("FLAX_POST_REDFISH_CREDS", "/etc/flax/credentials-redfish.json")


def _load_redfish_creds(path=None):
    """Redfish creds as [{bmcuser,bmcpass}], normalizing the credentials-redfish.json
    rfuser/rfpass schema (Administrator) — mirrors the flax_observe fix. Accepts a
    plain bmcuser/bmcpass list too. Missing/vault/malformed -> []."""
    if path is None:
        path = REDFISH_CREDS_PATH
    try:
        with open(path) as f:
            first = f.readline()
            if first.startswith("$ANSIBLE_VAULT"):
                return []
            data = json.loads(first + f.read())
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for c in data:
        u = c.get("bmcuser") or c.get("rfuser")
        p = c.get("bmcpass") or c.get("rfpass")
        if u and p:
            out.append({"bmcuser": u, "bmcpass": p})
    return out


def _default_make_redfish(redfish_creds, bmc_creds=None):
    """Factory: bmc_ip -> RedfishClient (or None if no creds are usable, so the
    fallback is a no-op). Prefers dedicated credentials-redfish.json; falls back to
    the IPMI bmc_creds when that file is empty/absent -- verified 2026-09-10 that
    this fleet's Basic-auth Redfish accepts the same USERID/PASSW0RD pair as IPMI
    (credentials-bmc.json cred[1]), so eindhoven (whose redfish creds file is empty)
    isn't left with no fallback at all. Local import avoids pulling the fwd package
    at module load (and keeps the IPMI producer importable in minimal test envs)."""
    creds = redfish_creds or bmc_creds or []
    if not creds:
        return lambda ip: None
    from ..fwd.redfish import RedfishClient
    return lambda ip: RedfishClient(ip, creds)


def _default_ipmi_runner(host, user, password, args, timeout=IPMITOOL_TIMEOUT_SECS):
    """One ipmitool call -> stdout. Cipher-3 first, then auto-negotiate. Caller catches.
    Mirrors flax_observe.ipmi._default_ipmi_runner (minus the redfish-reset side-effect)."""
    common = ["-I", "lanplus", "-N", "2", "-R", "3", "-U", user, "-P", password, "-H", host]
    try:
        r = subprocess.run(["ipmitool", "-C", "3"] + common + args,
                           timeout=timeout, capture_output=True, check=True)
        return r.stdout.decode("utf-8", errors="replace")
    except subprocess.CalledProcessError:
        r = subprocess.run(["ipmitool"] + common + args,
                           timeout=timeout, capture_output=True, check=True)
        return r.stdout.decode("utf-8", errors="replace")


def _default_power_runner(host, user, password, args, timeout=POWER_TIMEOUT_SECS):
    """ipmi_runner for the fast power lane — same call, short timeout so a dead BMC
    fails fast instead of stalling the cheap power read for the full 15s."""
    return _default_ipmi_runner(host, user, password, args, timeout=timeout)


def _power_and_sdr(ip, user, password, ipmi_runner):
    """`power status` + `sdr` in ONE RMCP+ session via `ipmitool ... exec <script>`,
    mirroring flax_observe.bmc_probe.bmc_power_and_sdr_traditional. Cuts the per-BMC
    IPMI session count (one auth handshake instead of two) and lets the two reads
    share the wider SDR_TIMEOUT_SECS budget the slow `sdr` walk actually needs,
    instead of each getting its own separate clock. Returns combined stdout text —
    caller parses both `_parse_power` and `_parse_watts`/`_parse_sdr` out of it."""
    tmp = tempfile.NamedTemporaryFile(mode="w", prefix="ipmi-cmds-", suffix=".txt", delete=False)
    try:
        tmp.write("power status\nsdr\n")
        tmp.close()
        return ipmi_runner(ip, user, password, ["exec", tmp.name], timeout=SDR_TIMEOUT_SECS)
    finally:
        try:
            os.unlink(tmp.name)
        except FileNotFoundError:
            pass


def _default_ping(ip, timeout=1):
    if not ip:
        return False
    try:
        return subprocess.run(["ping", "-c", "1", "-W", str(timeout), ip],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False


def _serial_from_fru(text):
    """Product/Chassis Serial, skipping empty multi-FRU lines (mirrors bmc_probe)."""
    for needle in ("Product Serial", "Chassis Serial"):
        for line in text.splitlines():
            if needle in line:
                v = line.split(":", 1)[1].strip()
                if v:
                    return v
    return None


def _parse_power(text):
    o = text.lower()
    return "on" if "is on" in o else ("off" if "is off" in o else "unknown")


def _norm_redfish_power(state):
    """Redfish Systems.PowerState ('On'/'Off') -> our 'on'/'off'; None otherwise."""
    if not state:
        return None
    s = str(state).strip().lower()
    return s if s in ("on", "off") else None


def _parse_watts(text):
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) >= 2 and "hsc" in parts[0].lower() and "power" in parts[0].lower() \
                and "Watts" in parts[1]:
            return parts[1].strip().replace(" Watts", " W")
    return None


def _parse_sdr(text):
    out = {}
    for line in text.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3 and parts[0]:
            out[parts[0]] = {"value": parts[1], "status": parts[2]}
    return out


def _parse_sel(text):
    """`ipmitool sel elist` -> [{'id','ts','event'}...], one per non-blank line.

    Pipe-delimited: `id | MM/DD/YYYY | HH:MM:SS | sensor | description`. Preserve
    the timestamp (date+time) and keep the event text so the UI can render one
    event per line WITH its timestamp (joining everything onto one line makes a
    70-event SEL unreadable for a human)."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 4:
            ts = " ".join(p for p in parts[1:3] if p)
            event = " ".join(parts[3:]).strip()
        else:
            ts, event = "", line
        out.append({"id": parts[0] if parts else "", "ts": ts, "event": event})
    return out


def _parse_fru(text):
    """Full FRU dump -> {field: value}. Duplicate field names across multi-FRU
    output collapse last-wins — deterministic for the same text, which is all
    the inventory content-hash dedupe needs."""
    out = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if k and v:
            out[k] = v
    return out


def probe_blade(ip, creds, ipmi_runner, redfish_client=None):
    """All IPMI fields for one BMC, best-effort; first working credential wins.

    When IPMI answers nothing (a Redfish-only AMI board — no IPMI, or MegaRAC
    session exhaustion), the optional redfish_client fills the fields IPMI could
    not read (serial, power). IPMI stays the primary path; Redfish is a pure
    fallback, so a working IPMI board never touches it (no regression)."""
    result = {"serial": None, "power_on": None, "watts": None, "sdr": {},
              "sel": [], "fru": {}}
    for c in creds:
        u, p = c["bmcuser"], c["bmcpass"]
        try:
            fru_txt = ipmi_runner(ip, u, p, ["fru"])
            result["serial"] = _serial_from_fru(fru_txt)
            result["fru"] = _parse_fru(fru_txt)
        except Exception:
            continue
        try:
            combined_txt = _power_and_sdr(ip, u, p, ipmi_runner)
            result["power_on"] = _parse_power(combined_txt)
            result["watts"] = _parse_watts(combined_txt)
            result["sdr"] = _parse_sdr(combined_txt)
        except Exception:
            pass
        try:
            result["sel"] = _parse_sel(ipmi_runner(ip, u, p, ["sel", "elist"]))
        except Exception:
            pass
        break                       # first working cred wins
    _redfish_fill(result, redfish_client)
    return result


def _redfish_fill(result, redfish_client):
    """Backfill serial/power/watts from Redfish for fields IPMI left unread. No-op
    when IPMI already supplied them (fallback only) or no client is configured."""
    if redfish_client is None:
        return
    if not result.get("serial"):
        try:
            s, _ = redfish_client.get_serial()
            if s:
                result["serial"] = s
        except Exception:
            log.exception("redfish serial fallback failed")
    if result.get("power_on") is None:
        try:
            ps, _ = redfish_client.get_power_state()
            p = _norm_redfish_power(ps)
            if p:
                result["power_on"] = p
        except Exception:
            log.exception("redfish power fallback failed")
    if result.get("watts") is None:
        try:
            w, _ = redfish_client.get_power_watts()
            if w is not None:
                result["watts"] = "%.2f W" % w
        except Exception:
            log.exception("redfish watts fallback failed")


def probe_power(ip, creds, ipmi_runner, redfish_client=None):
    """Just the chassis power state — the one cheap IPMI call. First working cred wins.

    Used by the fast power lane; kept separate from probe_blade so it can run on a
    tight interval with a short timeout without dragging the slow serial/SDR/SEL reads.
    Falls back to Redfish (redfish_client.get_power_state) when IPMI answers nothing."""
    for c in creds:
        try:
            return _parse_power(ipmi_runner(ip, c["bmcuser"], c["bmcpass"], ["power", "status"]))
        except Exception:
            continue
    if redfish_client is not None:
        try:
            return _norm_redfish_power(redfish_client.get_power_state()[0])
        except Exception:
            log.exception("redfish power fallback failed")
    return None


def _process_blade(d, hosts, creds, ipmi_runner, ping, set_state, upsert_node, order_no,
                   keys=None, record_observation=None, switch=SWITCH, make_redfish=None):
    """Heavy pass for one BMC: serial(FRU) · watts · SDR · SEL + host liveness.

    Power + bmc liveness are deliberately NOT written to live post_state here — the
    fast lane (run_power_once) owns them, so this slow pass (bounded by the worst
    BMC) can't write a stale power value over a fresh one. Durable post_node still
    records power for history. Self-contained for its own worker thread; the two
    writes use independent try/excepts so a failure on one tier never skips the other."""
    port = d["port"]
    bmc_ip = d.get("lease_ip") or d.get("reservation_ip")
    host = hosts.get(port)
    host_ip = (host.get("lease_ip") or host.get("reservation_ip")) if host else None
    host_pinged = bool(host_ip and ping(host_ip))
    rc = make_redfish(bmc_ip) if (make_redfish and bmc_ip) else None
    fields = probe_blade(bmc_ip, creds, ipmi_runner, redfish_client=rc) if bmc_ip else {
        "serial": None, "power_on": None, "watts": None, "sdr": {}, "sel": [], "fru": {}}
    # watts/sdr both come from the one power+sdr session: a timeout on that call
    # (the slow leg — SDR_TIMEOUT_SECS) leaves them at their None/{} defaults. Omit
    # them from this pass's write rather than merging None/{} over a previously-good
    # reading — this pass "didn't get an answer this time", not "the answer is now
    # unknown". Only write what this pass actually read.
    live_fields = {"serial": fields["serial"], "sel": fields["sel"]}
    if fields["watts"] is not None:
        live_fields["watts"] = fields["watts"]
    if fields["sdr"]:
        live_fields["sdr"] = fields["sdr"]
    try:
        set_state(port, switch=switch, bmc_mac=d.get("mac"), order_no=order_no,
                  host_pinged=host_pinged, **live_fields)
    except Exception:
        log.exception("ipmi: failed to write post_state for %s", port)
    if d.get("mac"):
        try:
            upsert_node(d["mac"], serial=fields["serial"],
                        host_mac=host.get("mac") if host else None, order_no=order_no,
                        last_switch=switch, last_port=port,
                        power_on=fields["power_on"], sel=fields["sel"])
        except Exception:
            log.exception("ipmi: failed to upsert post_node for %s", d.get("mac"))
    if record_observation is not None:
        try:
            record_observation(
                p0_mac=host.get("mac") if host else None,
                serial=fields["serial"], fru=fields.get("fru") or {},
                sdr=fields["sdr"], sel=fields["sel"], keys=keys or {})
        except Exception:
            log.exception("ipmi: work-record write failed for %s", port)


_LATCH_SLICES = ("done", "qual", "pop")


def clear_fields_for(prior_row, mac, power, now=None) -> dict:
    """Which post_state slices this power reading must reset.

    off->on is a restart of the blade's whole story (spec 2026-09-11
    post-slot-ladder §5, decision 8): the verdict latch clears (done/qual/pop,
    as before) and the ladder restarts past power-on via ladder.human_reset —
    UNLESS the slot worker issued this power-on itself, which it announces by
    setting ladder.power_on_pending before the ipmitool call. Then this lane
    writes nothing and the worker advances its own ladder.

    A DIFFERENT MAC on the port is deliberately NOT a reset (see gc.py; the
    116-reset incident on et25b3, 2026-09-11)."""
    if not prior_row:
        return {}
    if prior_row.get("power_on") != "off" or power != "on":
        return {}
    if (prior_row.get("ladder") or {}).get("power_on_pending"):
        return {}
    # Reset if the row has a ladder or is latched (has a verdict)
    has_ladder = prior_row.get("ladder") is not None
    is_latched = (prior_row.get("done") or {}).get("verdict") is not None
    if not (has_ladder or is_latched):
        return {}
    from . import ladder as _ladder
    out = {s: {} for s in _LATCH_SLICES}
    out["ladder"] = _ladder.human_reset(now if now is not None else time.time())
    return out


def _process_blade_power(d, creds, ipmi_runner, ping, set_state, switch=SWITCH, make_redfish=None,
                         prior_row=None):
    """Fast lane for one BMC: ping + chassis power only, merged into live post_state.
    prior_row (this port's row from the last read_state) lets the lane notice an
    off->on transition or a swapped blade and reset the slices that must not
    survive it (clear_fields_for)."""
    port = d["port"]
    bmc_ip = d.get("lease_ip") or d.get("reservation_ip")
    bmc_pinged = bool(bmc_ip and ping(bmc_ip))
    rc = make_redfish(bmc_ip) if (make_redfish and bmc_ip) else None
    power = probe_power(bmc_ip, creds, ipmi_runner, redfish_client=rc) if bmc_ip else None
    cleared = clear_fields_for(prior_row, d.get("mac"), power)
    if cleared:
        log.info("ipmi: %s reset %s (human power-on)", port, ",".join(sorted(cleared)))
    try:
        set_state(port, switch=switch, bmc_mac=d.get("mac"),
                  power_on=power, bmc_pinged=bmc_pinged, **cleared)
    except Exception:
        log.exception("ipmi: failed to write power for %s", port)


def run_once(devices=None, creds=None, ipmi_runner=None, ping=None,
             set_state=None, upsert_node=None, settings=None, workers=None,
             record_observation=None, switch=None, make_redfish=None) -> None:
    """One pass over the post BMCs, FANNED OUT across a worker pool.

    Each BMC is an independent IPMI session, so probing 48 blades sequentially
    (~seconds each) leaves the UI minutes-stale; a thread pool collapses the
    wall-clock to ~one slow BMC. `workers` defaults to FLAX_POST_OBSERVE_WORKERS
    (48) — set 1 to force the deterministic sequential path."""
    if devices is None:
        devices = queries.post_devices()
    if creds is None:
        creds = _creds.load_redfish_creds(BMC_CREDS_PATH)
    if ipmi_runner is None:
        ipmi_runner = _default_ipmi_runner
    if ping is None:
        ping = _default_ping
    if set_state is None:
        set_state = state.set_state
    if upsert_node is None:
        upsert_node = state.upsert_node
    if settings is None:
        settings = state.read_settings()
    if not creds:
        log.warning("ipmi: no BMC credentials; skipping pass")
        return
    if make_redfish is None:
        make_redfish = _default_make_redfish(_load_redfish_creds(), bmc_creds=creds)

    if switch is None:
        switch = _post_switch()
    order_no = settings.get("order_no")
    keys = records.role_keys(settings)      # {"order":…, "customer":…}, nulls omitted
    hosts = {d["port"]: d for d in devices
             if d.get("kind") == "host" and d.get("switch") == switch}
    bmcs = [d for d in devices
            if d.get("kind") == "bmc" and d.get("switch") == switch and d.get("port")]
    if not bmcs:
        return

    def work(d):
        _process_blade(d, hosts, creds, ipmi_runner, ping, set_state, upsert_node, order_no,
                       keys=keys, record_observation=record_observation, switch=switch,
                       make_redfish=make_redfish)

    n = DEFAULT_WORKERS if workers is None else workers
    n = max(1, min(n, len(bmcs)))
    if n == 1:
        for d in bmcs:
            work(d)
    else:
        with ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(work, bmcs))


def run_power_once(devices=None, creds=None, ipmi_runner=None, ping=None,
                   set_state=None, workers=None, switch=None, make_redfish=None,
                   prior=None) -> None:
    """FAST power-only pass: ping + chassis power for every post BMC, fanned out.

    Decoupled from run_once so a power-state change shows on the rack tile in ~one
    fast pass instead of waiting on the slow full pass (serial/SDR/SEL, bounded by
    the worst BMC). Writes ONLY power_on + bmc_pinged (set_state's JSONB merge leaves
    the heavy fields intact) and is the SOLE live-power writer — see _process_blade."""
    if devices is None:
        devices = queries.post_devices()
    if creds is None:
        creds = _creds.load_redfish_creds(BMC_CREDS_PATH)
    if ipmi_runner is None:
        ipmi_runner = _default_power_runner
    if ping is None:
        ping = _default_ping
    if set_state is None:
        set_state = state.set_state
    if not creds:
        log.warning("ipmi: no BMC credentials; skipping power pass")
        return
    if make_redfish is None:
        make_redfish = _default_make_redfish(_load_redfish_creds(), bmc_creds=creds)

    if switch is None:
        switch = _post_switch()
    bmcs = [d for d in devices
            if d.get("kind") == "bmc" and d.get("switch") == switch and d.get("port")]
    if not bmcs:
        return
    if prior is None:
        # One read per pass: the previous power reading and verdict per port,
        # so the lane can clear a completion latch on off->on (clear_fields_for).
        try:
            prior = state.read_state()
        except Exception:
            log.exception("ipmi: could not read prior state; latch clearing skipped this pass")
            prior = {}

    def work(d):
        _process_blade_power(d, creds, ipmi_runner, ping, set_state, switch=switch,
                             make_redfish=make_redfish, prior_row=prior.get(d["port"]))

    n = DEFAULT_WORKERS if workers is None else workers
    n = max(1, min(n, len(bmcs)))
    if n == 1:
        for d in bmcs:
            work(d)
    else:
        with ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(work, bmcs))
