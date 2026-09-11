# flax_post/observe/worker.py
"""One worker thread per post slot (spec 2026-09-11 post-slot-ladder §3).

The worker reads its slot's blade record from a shared SlotFeed (one
_blade_slots() call per FEED_INTERVAL_S for all 48 workers, not 48 calls),
gathers the single piece of evidence the ladder asks for, advances the pure
machine, performs the returned actions, persists the slice. It owns power-on,
the claim sentinel, boot-marker scraping, host ping/ssh, on-demand firmware
probes, and the agent poll (host_qual.poll_target, which owns qual/pop/done
and the console artifact). Everything else stays with the IPMI lanes.
"""
import copy
import logging
import os
import subprocess
import threading
import time
import urllib.request

from .. import actions
from . import bootlog, claims, host_qual, ladder, solclient

log = logging.getLogger("flax-post.worker")

FEED_INTERVAL_S = int(os.environ.get("FLAX_POST_FEED_INTERVAL", "2"))
HOLD_DIR = os.environ.get("FLAX_POST_POWER_HOLD_DIR", "/etc/flax/post-power-hold")
FWD_URL = os.environ.get("FLAX_POST_FWD_URL", "http://127.0.0.1:8447")
BIOSD_URL = os.environ.get("FLAX_POST_BIOSD_URL", "http://127.0.0.1:8449")
NICD_URL = os.environ.get("FLAX_POST_NICD_URL", "http://127.0.0.1:8450")
PROBE_TIMEOUT_S = 10
_PROBE_URL = {"probe-fwd": FWD_URL, "probe-biosd": BIOSD_URL, "probe-nicd": NICD_URL}


def allowed(port, allowlist) -> bool:
    """FLAX_POST_LADDER_PORTS gate: empty list = every slot."""
    return not allowlist or port in allowlist


def snapshot_from_record(rec, *, allowed, hold) -> dict:
    return {"bmc_pinged": bool(rec.get("bmc_pinged")),
            "power_on": rec.get("power_on"),
            "host_leased": bool(rec.get("host_leased")),
            "verdict": rec.get("verdict"),
            "hold": bool(hold),
            "fw_flashing": actions.flash_active(rec),
            "fw_gates": bool(rec.get("fw_gates")),
            "allowed": bool(allowed),
            "launch_at": rec.get("launch_at")}


def iterate_once(port, deps, is_allowed, now):
    """One worker step for `port`. Returns (ladder, seconds-to-sleep); the
    ladder is None for an empty/unreserved slot. Never raises for a bad action:
    a failed power-on is a ladder fault, everything else is logged and retried
    by the next step.

    Write discipline: post_state is only written when the slice actually
    changed (48 workers rewriting an identical ladder every 2s is 11-24
    UPDATEs/s against the pool the IPMI lanes share), and a step that neither
    acted nor changed anything sleeps the idle interval instead of 2s."""
    rec = deps.record(port)
    if not rec or rec.get("empty") or not rec.get("bmc_ip"):
        # The blade was pulled (or never arrived). Anything we claimed for its
        # boot window would otherwise leak forever: gc deletes the row, and no
        # later step for this port ever emits `unclaim`.
        if deps.holds_claim(port):
            try:
                deps.act("unclaim", {"port": port}, None)
            except Exception:
                log.exception("%s: unclaim of an emptied slot failed", port)
        return None, ladder.IDLE_INTERVAL_S
    lad = rec.get("ladder") or None
    snap = snapshot_from_record(rec, allowed=is_allowed, hold=deps.hold(port))
    kind = ladder.evidence_needed(lad)
    evidence = {}
    try:
        if kind == "agent":
            qual = deps.poll_agent(rec, is_allowed)
            evidence["agent"] = bool((qual.get("agent") or {}).get("reachable"))
        elif kind is not None:
            evidence[kind] = deps.evidence(kind, rec, lad)
    except Exception:
        # A raising probe (missing creds file, DB error inside poll_target, ...)
        # must not abort the iteration: advance still runs with no evidence for
        # this step, so budgets keep expiring into faults instead of the claim
        # (and the port) being held forever.
        log.exception("%s: evidence %s failed", port, kind)
    new, acts = ladder.advance(lad, snap, evidence, now)
    baseline = lad
    if "power-on" in acts:
        # Persist power_on_pending BEFORE the chassis moves. The IPMI power
        # lane reads `prior` at the start of each 6s pass and decides off->on
        # is a HUMAN's when the flag is unset; writing it only after
        # sol-mark (5s) + powertriage (120s) leaves a window where the lane
        # clobbers done/qual/pop and rotates the SOL capture away from the
        # engine's own power-on.
        deps.write_ladder(port, new)
        baseline = copy.deepcopy(new)
    for act in acts:
        if act == "poll-agent":
            if kind != "agent":                         # else already polled this step
                try:
                    deps.poll_agent(rec, is_allowed)    # launch only when allowlisted
                except Exception:
                    log.exception("%s: agent poll failed", port)
            continue
        try:
            res = deps.act(act, rec, new)
        except Exception:
            log.exception("%s: action %s failed", port, act)
            continue
        if act == "power-on" and not (res or {}).get("ok"):
            reason = "rejected: " + ((res or {}).get("output") or "no output").strip()[:200]
            new = ladder.fault(new, "power-on", reason, now)
            try:
                deps.act("unclaim", rec, new)
            except Exception:
                log.exception("%s: unclaim after rejected power-on failed", port)
            break
        if act.startswith("probe-"):
            new.setdefault("probe_calls", {})[act[len("probe-"):]] = now
    changed = new != baseline
    if changed:
        deps.write_ladder(port, new)
    if not acts and new == lad and ladder.evidence_needed(new) is None:
        return new, ladder.IDLE_INTERVAL_S
    return new, ladder.interval_s(new)


class SlotFeed:
    """Shared, periodically refreshed {port: record} from app._blade_slots()."""

    def __init__(self, fetch, interval_s=FEED_INTERVAL_S):
        self._fetch = fetch
        self._interval = interval_s
        self._lock = threading.Lock()
        self._by_port = {}

    def refresh(self):
        try:
            slots = self._fetch()
        except Exception:
            log.exception("slot feed refresh failed")
            return
        with self._lock:
            self._by_port = {s["port"]: s for s in slots if s.get("port")}

    def get(self, port):
        with self._lock:
            return self._by_port.get(port)

    def start(self):
        def loop():
            while True:
                self.refresh()
                time.sleep(self._interval)
        threading.Thread(target=loop, name="slot-feed", daemon=True).start()
        return self


def _ping(ip) -> bool:
    if not ip:
        return False
    try:
        return subprocess.run(["ping", "-c", "2", "-i", "1", "-W", "1", "-q", ip],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=6).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _ssh_ok(ip) -> bool:
    from ..biosd import creds as _creds, driver as _driver
    user, pw = _creds.load_host_creds()
    rc, _ = _driver.run_over_ssh(user, pw, ip, "true", timeout=30)
    return rc == 0


def _probe_daemon(url, port) -> None:
    req = urllib.request.Request(f"{url}/probe/{port}", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:
        log.info("probe %s/%s -> %s", url, port, resp.status)


class RealDeps:
    """Production wiring. Every method maps 1:1 to a spec §3/§5/§6 action."""

    def __init__(self, feed, *, store=None):
        from .. import state as _state
        self._feed = feed
        self._store = store or _state
        self._last_ladder = {}
        # Ports whose /run/flax/bmc-fw-active sentinel THIS worker created.
        # The directory is shared with triage's bmc_fw worker (claims.py), so
        # we only ever drop a claim we actually took.
        self._claimed = set()

    def record(self, port):
        """The feed's record for `port`, with its ladder slice reconciled
        against the worker's own last write.

        The SlotFeed refreshes at most every FEED_INTERVAL_S, so the record
        it hands back can still carry the ladder from BEFORE this worker's
        most recent write_ladder(). Reading that stale copy back would make
        the next advance() re-run from the old state: duplicate probe/claim/
        sol-mark actions, a duplicate power-on (the cooldown keys off the
        stale last_power_attempt), or a fresh fault silently overwritten by a
        stale non-faulted copy.

        The only OTHER writer of a slot's ladder is the IPMI lane's one-shot
        human_reset (a human power-cycled the blade). state.set_state merges
        `vars` at the TOP level, so the lane's `ladder=human_reset(...)` and a
        worker write of `ladder=<cached>` are last-writer-wins on one key and
        the reset can be gone before anyone observes it. The lane therefore
        also stamps the scalar `ladder_reset_at` — a key nothing else writes —
        and that timestamp, not the contended `ladder` key, decides here:
        newer than the cached slice's `since` means a human reset this blade
        after our last write, so adopt ladder.human_reset(ladder_reset_at) and
        drop the cache.

        So: an empty/missing slot forgets any cached ladder (nothing to
        reconcile); a reset newer than the cache wins (and clears it, since it
        becomes the new baseline); a feed ladder carrying human_power_on with
        no cache (or with no ladder_reset_at at all — rows written before the
        scalar existed) is taken as-is; otherwise prefer the cached ladder,
        when one exists, over the feed's possibly stale copy.
        """
        rec = self._feed.get(port)
        if not rec or rec.get("empty"):
            self._last_ladder.pop(port, None)
            return rec
        feed_lad = rec.get("ladder") or {}
        reset_at = rec.get("ladder_reset_at")
        cached = self._last_ladder.get(port)
        if cached is None:
            if feed_lad.get("human_power_on"):
                return self._adopt_reset(rec, reset_at)
            return rec
        if reset_at is not None and reset_at > (cached.get("since") or 0):
            self._last_ladder.pop(port, None)
            return self._adopt_reset(rec, reset_at)
        if reset_at is None and feed_lad.get("human_power_on"):
            self._last_ladder.pop(port, None)
            return rec
        rec = dict(rec)
        rec["ladder"] = cached
        return rec

    @staticmethod
    def _adopt_reset(rec, reset_at):
        """The record with the lane's reset as its ladder. A row with no
        ladder_reset_at keeps whatever reset slice the feed carries."""
        if reset_at is None:
            return rec
        rec = dict(rec)
        rec["ladder"] = ladder.human_reset(reset_at)
        return rec

    def holds_claim(self, port) -> bool:
        """True when this worker took the claim sentinel for `port` and has
        not dropped it (iterate_once's empty-slot path uses it so a blade
        pulled mid-boot does not leak the claim forever)."""
        return port in self._claimed

    def hold(self, port):
        return os.path.exists(os.path.join(HOLD_DIR, port))

    def evidence(self, kind, rec, lad):
        since = (lad or {}).get("power_on_at") or 0
        host_ip = rec.get("host_ip")
        if kind == "tftp":
            return bootlog.tftp_seen(bootlog.DNSMASQ_LOG, host_ip, since) if host_ip else None
        if kind == "ipxe":
            return bootlog.ipxe_seen(bootlog.NGINX_ACCESS_LOG, host_ip, since) if host_ip else None
        if kind == "iso":
            return bootlog.iso_seen(bootlog.NGINX_ACCESS_LOG, host_ip, since) if host_ip else None
        if kind == "ping":
            return _ping(host_ip)
        if kind == "ssh":
            return bool(host_ip) and _ssh_ok(host_ip)
        return None

    def act(self, action, rec, lad):
        port, bmc_ip = rec["port"], rec.get("bmc_ip")
        if action.startswith("sol-mark:"):
            out = solclient.mark(bmc_ip, action.split(":", 1)[1])
            log.info("%s: sol mark -> %s", port, out)
            return out
        if action == "claim":
            took = claims.claim(port)
            if took:
                self._claimed.add(port)
            else:
                log.info("%s: claim not taken (held by another writer)", port)
            return took
        if action == "unclaim":
            # Always call through: claims.unclaim is marker-guarded, so a
            # foreign file is left in place regardless of what _claimed
            # holds. _claimed can be empty here after a flax-post-observe
            # restart even though the on-disk sentinel is still ours (see
            # claims.claim's adoption path), so port-not-in-_claimed is no
            # longer a precondition to drop.
            self._claimed.discard(port)
            return claims.unclaim(port)
        if action == "power-on":
            res = actions.run_power(bmc_ip, "on", blocked=False)
            log.info("%s: power-on %s -> ok=%s", port, bmc_ip, res.get("ok"))
            return res
        if action in _PROBE_URL:
            _probe_daemon(_PROBE_URL[action], port)
            return None
        log.warning("%s: unknown action %s", port, action)
        return None

    def poll_agent(self, rec, launch):
        target = {"port": rec["port"], "host_ip": rec.get("host_ip"), "bmc_ip": rec.get("bmc_ip"),
                  "bmc_mac": rec.get("bmc_mac"), "serial": rec.get("serial"),
                  "order_no": rec.get("order_no"), "phase": "Qualify" if rec.get("fw_gates") else rec.get("phase")}
        if not target["host_ip"]:
            return {"agent": {"reachable": False}}
        return host_qual.poll_target(
            target, store=self._store,
            launch_agent=host_qual._default_launch_agent if launch else None,
            ping=_ping, console_reader=solclient.read_capture)

    def write_ladder(self, port, lad):
        self._store.set_state(port, ladder=lad)
        self._last_ladder[port] = copy.deepcopy(lad)

    def clock(self):
        return time.time()


def _run_slot(port, deps, is_allowed):
    while True:
        interval = ladder.IDLE_INTERVAL_S
        try:
            _, interval = iterate_once(port, deps, is_allowed, deps.clock())
        except Exception:
            log.exception("%s: worker iteration failed", port)
        time.sleep(interval)


def start_workers(ports, deps, allowlist):
    threads = []
    for port in ports:
        t = threading.Thread(target=_run_slot, args=(port, deps, allowed(port, allowlist)),
                             name=f"slot-{port}", daemon=True)
        t.start()
        threads.append(t)
    log.info("slot workers started: %d (allowlist=%s)", len(threads), allowlist or "(all)")
    return threads
