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


def _product_serial_from_fru(text):
    """The FIRST non-empty "Product Serial" of an `ipmitool fru` dump (FRU 0, the
    baseboard, prints first): the slot occupant's identity. No Chassis Serial
    fallback (a lot number shared across blades) and never a Redfish serial
    (SMBIOS placeholder, blank when off) — spec 2026-09-14-post-occupant-reset §3."""
    for line in (text or "").splitlines():
        if "Product Serial" in line and ":" in line:
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


def bmc_data_check(ip, creds, ipmi_runner, ping) -> "dict | None":
    """The slot ladder's bmc-ready evidence (ruling 2026-09-12): the BMC answers
    ping AND `ipmitool fru` returns the board identity the population rules
    are built on. {board_mfg, product, serial} on success, None otherwise —
    a BMC that pings but has not brought IPMI back yet is None."""
    if not ip or not ping(ip):
        return None
    for c in creds or []:
        try:
            text = ipmi_runner(ip, c["bmcuser"], c["bmcpass"], ["fru"])
        except Exception:
            continue
        # The BASEBOARD is the first FRU device in the dump; _parse_fru is
        # last-wins across devices (NIC, M.2 adapter) and would name the wrong
        # board. The serial can come from any block (Product, else Chassis).
        board = _first_fru_field(text, "Board Mfg")
        product = _first_fru_field(text, "Board Product") or _first_fru_field(text, "Product Name")
        fru = _parse_fru(text)
        serial = fru.get("Product Serial") or fru.get("Chassis Serial")
        if board and serial:
            return {"board_mfg": board, "product": product or "", "serial": serial}
    return None


def _first_fru_field(text, key):
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip() == key and v.strip():
                return v.strip()
    return None


def probe_blade(ip, creds, ipmi_runner, redfish_client=None):
    """All IPMI fields for one BMC, best-effort; first working credential wins.

    When IPMI answers nothing (a Redfish-only AMI board — no IPMI, or MegaRAC
    session exhaustion), the optional redfish_client fills the fields IPMI could
    not read (serial, power). IPMI stays the primary path; Redfish is a pure
    fallback, so a working IPMI board never touches it (no regression)."""
    result = {"serial": None, "power_on": None, "watts": None, "sdr": {},
              "sel": [], "fru": {}, "product_serial": None}
    for c in creds:
        u, p = c["bmcuser"], c["bmcpass"]
        try:
            fru_txt = ipmi_runner(ip, u, p, ["fru"])
            result["serial"] = _serial_from_fru(fru_txt)
            result["product_serial"] = _product_serial_from_fru(fru_txt)
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
        "serial": None, "power_on": None, "watts": None, "sdr": {}, "sel": [], "fru": {},
        "product_serial": None}
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
    return port, d.get("mac"), fields.get("product_serial")


# Occupant identity (spec 2026-09-14-post-occupant-reset §3/§4). A post_state
# row belongs to a SLOT, but done/qual/pop/ladder/power_last describe the BLADE
# that produced them; a swapped-in blade inherited them (et24b2, et24b3, et8b3,
# et10b4 on 2026-09-14). Only a real FRU read through a BMC counts as a
# sighting: the reservation MAC alone is not an occupant (et25b3, 116 resets,
# 2026-09-11).
OCCUPANT_RESET_MIN_S = int(os.environ.get("FLAX_POST_OCCUPANT_RESET_MIN_S", "600"))
# A candidate must be read again within this gap to count as consecutive, and a
# reset waits until the stored blade has not answered for this gap (spec §3.2).
OCCUPANT_CONFIRM_GAP_S = int(os.environ.get("FLAX_POST_OCCUPANT_CONFIRM_GAP_S", "120"))


def _ident(serial, bmc_mac):
    return (str(serial).strip(), str(bmc_mac or "").strip().lower())


def occupant_change(prior_row, reads, now, run_owner=None) -> dict:
    """Fields to merge into the slot row for this pass's FRU reads; {} = nothing.

    reads: [(bmc_mac, serial)] from every BMC reservation on the port that
    answered this pass. A reservation that did not answer is absent, so it can
    neither trigger nor confirm a change.
    run_owner: run_id -> (bmc_mac, serial) | None (state.run_owner), consulted
    only for a row that has no `occupant` yet, to judge a latch written before
    this existed."""
    row = prior_row or {}
    seen = []
    for mac, serial in reads or []:
        if serial and mac and _ident(serial, mac) not in seen:
            seen.append(_ident(serial, mac))
    if not seen:
        return {}
    occ = row.get("occupant") or None
    if not occ:
        run_id = (row.get("qual") or {}).get("run_id")
        owner = run_owner(run_id) if (run_id and run_owner) else None
        if owner and owner[0] and owner[1]:
            since = (row.get("ladder") or {}).get("since")
            occ = {"serial": owner[1], "bmc_mac": owner[0], "since": since, "last_read": since}
        elif len(seen) == 1:
            serial, mac = seen[0]
            return {"occupant": {"serial": serial, "bmc_mac": mac, "since": now, "last_read": now},
                    "occupant_candidate": None}
        else:
            return {}
    stored = _ident(occ.get("serial"), occ.get("bmc_mac"))
    if stored in seen:
        return {"occupant": {"serial": stored[0], "bmc_mac": stored[1],
                             "since": occ.get("since"), "last_read": now},
                "occupant_candidate": None}
    if len(seen) > 1:
        log.warning("ipmi: occupant ambiguous, %d new identities answered in one pass: %s",
                    len(seen), seen)
        return {}
    serial, mac = seen[0]
    cand = row.get("occupant_candidate") or None
    fresh = {"serial": serial, "bmc_mac": mac, "first_read": now, "last_read": now, "reads": 1}
    if not cand or _ident(cand.get("serial"), cand.get("bmc_mac")) != (serial, mac):
        return {"occupant_candidate": fresh}
    last = cand.get("last_read", cand.get("first_read"))
    if last is None or now - last > OCCUPANT_CONFIRM_GAP_S:
        # not read again in time (its BMC went dark, or passes read nothing): start over
        return {"occupant_candidate": fresh}
    bumped = {"occupant_candidate": dict(cand, reads=int(cand.get("reads") or 1) + 1, last_read=now)}
    stored_read = occ.get("last_read")
    if stored_read is not None and now - stored_read <= OCCUPANT_CONFIRM_GAP_S:
        return bumped          # the stored blade answered too recently to call it gone
    since = occ.get("since")
    if since is not None and now - since < OCCUPANT_RESET_MIN_S:
        log.info("ipmi: occupant reset to %s/%s deferred (occupant adopted %ds ago)",
                 serial, mac, now - since)
        return bumped
    return _occupant_reset_fields(row, occ, serial, mac, now)


def _occupant_reset_fields(row, old, serial, mac, now) -> dict:
    """Clear the previous blade's story and start a fresh ladder (spec §4)."""
    from . import ladder as _ladder
    born_at = old.get("last_read")
    if born_at is None:
        born_at = (row.get("ladder") or {}).get("since")
    if born_at is None or born_at > now:
        born_at = now
    return {"done": {}, "qual": {}, "pop": {}, "power_last": None, "power_on": None,
            "occupant": {"serial": serial, "bmc_mac": mac, "since": now, "last_read": now},
            "occupant_candidate": None,
            "ladder": _ladder.occupant_reset(born_at, now),
            "ladder_reset_at": now, "ladder_reset_kind": "occupant", "ladder_reset_born_at": born_at}


_LATCH_SLICES = ("done", "qual", "pop")
# How long after the worker's own `chassis power on` an observed off->on is
# still the ENGINE's. The power-on action itself (powertriage) can take the
# full 120s, and `prior` is read once at the start of each pass, so
# power_on_pending alone is not proof of a human when the flag write and this
# pass's probe interleave.
ENGINE_POWER_WINDOW_S = int(os.environ.get("FLAX_POST_ENGINE_POWER_WINDOW", "120"))


def clear_fields_for(prior_row, mac, power, now=None) -> dict:
    """Which post_state slices this power reading must reset.

    off->on is a restart of the blade's whole story (spec 2026-09-11
    post-slot-ladder §5, decision 8): the verdict latch clears (done/qual/pop,
    as before) and the ladder restarts past power-on via ladder.human_reset —
    UNLESS the slot worker issued this power-on itself, which it announces by
    setting ladder.power_on_pending before the ipmitool call (and, belt and
    braces, by stamping ladder.last_power_attempt: a pass whose `prior`
    predates the flag write still sees the attempt).

    The reset is also stamped as the scalar `ladder_reset_at`, a key only this
    lane writes. `ladder` itself is contended — set_state merges vars at the
    top level, so a worker write of its cached slice lands on the same key and
    can erase the reset before the worker ever reads it (worker.RealDeps.record
    reconciles on the scalar, not on the slice).

    A DIFFERENT MAC on the port is deliberately NOT a reset (see gc.py; the
    116-reset incident on et25b3, 2026-09-11). An off reading stamped by a different occupant is not a transition at all (§5 of the 2026-09-14 occupant spec)."""
    if not prior_row:
        return {}
    # The row's off reading belongs to the stamped occupant; a different BMC
    # answering `on` is a new blade, which occupant_change owns (spec
    # 2026-09-14-post-occupant-reset §5). A MAC mismatch only ever SUPPRESSES
    # a reset here, never triggers one (et25b3).
    occ_mac = (prior_row.get("occupant") or {}).get("bmc_mac")
    if occ_mac and mac and str(occ_mac).strip().lower() != str(mac).strip().lower():
        return {}
    # The prior DEFINITE reading: an AMI-style BMC goes dark right after a
    # chassis power-on, so the lane reads off, then None for a minute, then
    # on. The strict off->on test missed that (et24b3 2026-09-12, its old fail
    # re-latched); `power_last` (written by the lane on every definite read)
    # bridges the unreadable gap.
    prior = prior_row.get("power_on")
    if prior not in ("on", "off"):
        prior = prior_row.get("power_last")
    if prior != "off" or power != "on":
        return {}
    lad = prior_row.get("ladder") or {}
    if lad.get("power_on_pending"):
        return {}
    now = now if now is not None else time.time()
    last_attempt = lad.get("last_power_attempt")
    if last_attempt is not None and abs(now - last_attempt) <= ENGINE_POWER_WINDOW_S:
        return {}
    from . import ladder as _ladder
    out = {s: {} for s in _LATCH_SLICES}
    out["ladder"] = _ladder.human_reset(now)
    out["ladder_reset_at"] = now
    out["ladder_reset_kind"] = "human"
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
    if power in ("on", "off"):
        cleared["power_last"] = power          # the last definite reading survives a blackout
    try:
        set_state(port, switch=switch, bmc_mac=d.get("mac"),
                  power_on=power, bmc_pinged=bmc_pinged, **cleared)
    except Exception:
        log.exception("ipmi: failed to write power for %s", port)


def run_once(devices=None, creds=None, ipmi_runner=None, ping=None,
             set_state=None, upsert_node=None, settings=None, workers=None,
             record_observation=None, switch=None, make_redfish=None,
             prior=None, run_owner=None) -> None:
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
    # The occupant check (spec 2026-09-14-post-occupant-reset §3) needs the
    # rows as they were before this pass. Production reads them here; callers
    # that inject their own writers pass `prior` or get no occupant check.
    if prior is None and set_state is None:
        try:
            prior = state.read_state()
        except Exception:
            log.exception("ipmi: could not read prior state; occupant check skipped this pass")
            prior = None
    if run_owner is None:
        run_owner = state.run_owner
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
        return _process_blade(d, hosts, creds, ipmi_runner, ping, set_state, upsert_node, order_no,
                              keys=keys, record_observation=record_observation, switch=switch,
                              make_redfish=make_redfish)

    n = DEFAULT_WORKERS if workers is None else workers
    n = max(1, min(n, len(bmcs)))
    if n == 1:
        results = [work(d) for d in bmcs]
    else:
        with ThreadPoolExecutor(max_workers=n) as ex:
            results = list(ex.map(work, bmcs))
    if prior is not None:
        apply_occupants(results, prior, set_state, run_owner, time.time(), switch=switch)


def apply_occupants(results, prior, set_state, run_owner, now, switch=SWITCH) -> None:
    """One occupant decision per port from every reservation's read this pass.
    Per-reservation decisions would race: the threads share `prior`, so one
    could confirm a candidate while another read the stored blade (spec §3)."""
    by_port = {}
    for res in results:
        if not res:
            continue
        port, mac, serial = res
        reads = by_port.setdefault(port, [])
        if mac and serial:
            reads.append((mac, serial))
    for port, reads in by_port.items():
        row = prior.get(port)
        try:
            fields = occupant_change(row, reads, now, run_owner=run_owner)
        except Exception:
            log.exception("ipmi: occupant check failed for %s", port)
            continue
        if not fields:
            continue
        if fields.get("ladder_reset_kind") == "occupant":
            old = (row or {}).get("occupant") or {}
            log.info("ipmi: %s occupant reset %s/%s -> %s/%s (born_at %s)", port,
                     old.get("serial"), old.get("bmc_mac"), fields["occupant"]["serial"],
                     fields["occupant"]["bmc_mac"], fields["ladder_reset_born_at"])
        try:
            set_state(port, switch=switch, **fields)
        except Exception:
            log.exception("ipmi: failed to write occupant for %s", port)


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
