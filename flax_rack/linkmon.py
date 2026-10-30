"""Live link monitor: every 30 s each rack switch is read once (one ssh session:
`show interfaces status; show lldp table; show mac`, flax_rungs.sonic.read_live) and
every geometry link is judged against that snapshot. Read-only: no switch config, no
power action, never a `run` row.

Per link (evaluate):
  ok       oper up + the expected identity on that interface: an LLDP system name that
           is the device's hostname (<prefix><pos>, e.g. rr101-l37, or <name>-...), or
           one of the device's order.db MACs (host / BMC / IOMs) in the port's FDB
  up       oper up, no identity evidence (an active-backup backup port is up but
           silent: no FDB entry, maybe no LLDP) -- green, never "missing"
  wrong    oper up and the port carries an identity of ANOTHER device of this rack
           (LLDP name or FDB MAC; one the geometry does not also put on this interface),
           a JBOD iomN port whose IOM MAC last reported the other side (geometry
           `sides`, check_pal_sku.sh: the two IOM cables are swapped),
           an LLDP name that looks like a rackpost hostname but no device of this rack,
           or (mgmt switch only, the device's MACs known) MACs of no device of this rack
           and none of its own. `text` names what is seen.
  down     oper down
  unknown  the switch's snapshot is missing (did not answer / no mgmt IP / not read
           yet) or older than STALE_S -- never `down`: nothing was observed

Switch links (key "_sw-<id>"): uplink<N> (up/down/unknown from the switch's own
status) and `site`, read from the switch's own mgmt `eth0` LLDP neighbour (site_link:
ok = the geometry site_port's switch + port, wrong = another, up = answers ssh but no
eth0 neighbour, unknown = no fresh snapshot; the site turtle itself is not read).

LinkPoller: one thread per served rack. Each tick starts a read for every switch that
has none in flight (a slow ssh never piles up; other switches still update), keeps
the snapshots in memory (a restarted daemon rebuilds them within one poll: nothing
stale carried over), and appends each state change to order.db `link_event`. The
order's event log gets at most one line per switch per poll (link_summary), and only
for changes into or out of down/wrong/unknown: ok <-> up is identity evidence aging
(an FDB entry), not a link change. A link whose switch has not been read yet in this
process is not logged (a restart does not log ok → unknown → ok). A wedge's mgmt IP is
the geometry's, else the lease Discover last proved in any pass (switch_ips).
"""
import re
import threading
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone

from flax_rungs import sonic
from flax_rungs.sonic import wedge_iface

from .stages import creds

INTERVAL_S = 30
STALE_S = 90
STATES = ("ok", "up", "down", "wrong", "unknown")
# a rackpost hostname of some rack: <2 letters><3 digits>[rack letter]-...
_RACKISH = re.compile(r"[a-z]{2}\d{3}[a-z]?-")
_NO_NAME = {"", "localhost", "(none)", "none"}


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds") \
        if t is not None else None


def _d(x):
    return asdict(x) if is_dataclass(x) else dict(x)


def _colon(m):
    return ":".join(m[i:i + 2] for i in range(0, 12, 2)) if m and len(m) == 12 else m


def link_iface(sw, port: int, lane: int | None) -> str:
    """Geometry port (+lane) on switch `sw` -> SONiC interface name."""
    if sw.model == "wedge100s":
        return wedge_iface(port, lane)
    return f"Ethernet{port - 1}"           # N3248TE: front port N = EthernetN-1


def device_names(g) -> dict[str, str]:
    """pos -> the device's base hostname (<prefix><pos>; multi-host cards add -nK)."""
    return {d.pos: f"{g.prefix}{d.pos.lower()}" for d in g.devices}


def device_macs(db) -> dict[str, set[str]]:
    """pos -> the device's MACs order.db knows (host, BMC, IOMs; 12 hex lower)."""
    out = {}
    for x in db.devices():
        out[x["pos"]] = {m for m in (x.get("host_mac"), x.get("bmc_mac"),
                                     *(x.get("iom_macs") or [])) if m}
    # an IOM's last seen MAC (order.db iom_macs stays empty until both IOMs are found);
    # any pass: a MAC is a fact about the hardware, Start fresh does not hide it
    for (key, _), det in db.last_passed(("bmc-mac-seen",), all_passes=True).items():
        pos = key.split(".")[0]
        if pos in out and key != pos and det.get("iom_mac"):
            out[pos].add(det["iom_mac"].replace(":", "").lower())
    return out


def _snap_view(snap):
    """(ifaces {iface: {oper, admin, speed}}, lldp {iface: [names]}, fdb {iface: [macs]})"""
    ifaces = {k: _d(v) for k, v in (snap.get("ifaces") or {}).items()}
    lldp, fdb = {}, {}
    for e in snap.get("lldp") or []:
        e = _d(e)
        lldp.setdefault(e.get("local_iface"), []).append((e.get("remote_device") or "").strip())
    for e in snap.get("fdb") or []:
        e = _d(e)
        m = (e.get("mac") or "").replace(":", "").lower()
        if m not in fdb.setdefault(e.get("iface"), []):
            fdb[e["iface"]].append(m)
    return ifaces, lldp, fdb


def evaluate(g, snapshots: dict, macs: dict, names: dict, now: float,
             iom_sides: dict | None = None) -> dict:
    """Pure: {pos: {link: {"state", "text", "seen": {"lldp", "macs"}, "at", "switch",
    "iface"}}} for every geometry device link, plus "_sw-<id>" for each switch's
    uplinks and site port. snapshots: switch id -> {"at": epoch, "ifaces", "lldp",
    "fdb"} (dataclasses or dicts) or None; "at" is the snapshot's time (None without).
    iom_sides: {IOM MAC: side it reported} (OrderDB.iom_sides)."""
    iom_sides = iom_sides or {}
    sws = {s.id: s for s in g.switches}
    views, fresh = {}, {}
    for sid, snap in snapshots.items():
        if snap and snap.get("at") is not None:
            views[sid] = _snap_view(snap)
            fresh[sid] = now - snap["at"] <= STALE_S
    # who the geometry puts on each (switch, iface): a BOM that repeats a lane must not
    # turn one device's correct cable into another's "wrong"
    claim: dict[tuple[str, str], set[str]] = {}
    for d in g.devices:
        for l in d.links.values():
            if l.switch in sws:
                claim.setdefault((l.switch, link_iface(sws[l.switch], l.port, l.lane)),
                                 set()).add(d.pos)
    mac_owner = {m: pos for pos, ms in macs.items() for m in ms}

    def name_owner(n):
        n = n.lower()
        for pos, base in names.items():
            if n == base or n.startswith(base + "-"):
                return pos
        return None

    def unknown(sid, iface, why):
        snap = snapshots.get(sid)
        return {"state": "unknown", "text": why, "seen": {"lldp": None, "macs": []},
                "at": _iso((snap or {}).get("at")), "switch": sid, "iface": iface}

    def judge(sid, iface, pos=None, want_side=None):
        sw = sws.get(sid)
        if sw is None:
            return unknown(sid, iface, f"no switch {sid} in geometry")
        snap = snapshots.get(sid)
        if sid not in views:
            return unknown(sid, iface, f"switch {sid} not answering" if sid in snapshots
                           else f"switch {sid} not read yet")
        if not fresh[sid]:
            return unknown(sid, iface, f"switch {sid} last read {int(now - snap['at'])} s ago")
        ifaces, lldp, fdb = views[sid]
        st = ifaces.get(iface)
        nb = [n for n in lldp.get(iface, []) if n.lower() not in _NO_NAME]
        seen_macs = fdb.get(iface, [])
        base = {"seen": {"lldp": (lldp.get(iface) or [None])[0],
                         "macs": [_colon(m) for m in seen_macs]},
                "at": _iso(snap["at"]), "switch": sid, "iface": iface}
        if st is None:
            return {**base, "state": "unknown", "text": f"no {iface} on {sid}"}
        if st.get("oper") != "up":
            adm = "" if st.get("admin") in (None, "up") else f" (admin {st.get('admin')})"
            return {**base, "state": "down", "text": f"{iface} {st.get('oper') or 'down'}{adm}"}
        if pos is None:                       # a switch uplink: no identity expected
            return {**base, "state": "up", "text": f"{iface} up"
                    + (f" · {nb[0]}" if nb else "")}
        mine = macs.get(pos) or set()
        others = claim.get((sid, iface), set())
        ok = [n for n in nb if name_owner(n) == pos] + \
             [_colon(m) for m in seen_macs if m in mine]
        foreign = []
        for n in nb:
            o = name_owner(n)
            if o is None and _RACKISH.match(n.lower()):
                foreign.append(f"{n} (not this rack)")
            elif o is not None and o != pos and o not in others:
                foreign.append(f"{n} ({o})")
        for m in seen_macs:
            o = mac_owner.get(m)
            if o is not None and o != pos and o not in others:
                foreign.append(f"{_colon(m)} ({o})")
            elif o is None and sw.role == "mgmt" and mine and not ok:
                foreign.append(f"{_colon(m)} (not this rack)")
        swapped = [f"{_colon(m)} (Side {iom_sides[m]} IOM, want Side {want_side})"
                   for m in seen_macs if want_side and iom_sides.get(m) not in (None, want_side)]
        if swapped:
            return {**base, "state": "wrong", "text": "sees " + ", ".join(swapped + foreign)}
        if ok:
            return {**base, "state": "ok", "text": f"{iface} up · {ok[0]}"
                    + (f" · also {', '.join(foreign)}" if foreign else "")}
        if foreign:
            return {**base, "state": "wrong", "text": "sees " + ", ".join(foreign)}
        shared = sorted(o for n in nb if (o := name_owner(n)) and o != pos)
        return {**base, "state": "up", "text": f"{iface} up · "
                + (f"{nb[0]} (geometry also puts {shared[0]} here)" if shared
                   else (nb[0] if nb else "no identity seen"))}

    out: dict[str, dict] = {}
    for d in g.devices:
        out[d.pos] = {}
        for name, l in sorted(d.links.items()):
            sw = sws.get(l.switch)
            iface = link_iface(sw, l.port, l.lane) if sw else f"{l.switch}:{l.port}"
            out[d.pos][name] = judge(l.switch, iface, d.pos, d.sides.get(name))
    for s in g.switches:
        links = {f"uplink{p}": judge(s.id, link_iface(s, p, None)) for p in s.uplink_ports}
        if s.site_port:
            if s.id in views:
                links["site"] = site_link(s, snapshots[s.id], fresh[s.id], now)
            else:
                links["site"] = unknown(s.id, "eth0", f"switch {s.id} not answering"
                                        if s.id in snapshots else f"switch {s.id} not read yet")
        out[f"_sw-{s.id}"] = links
    return out


def _port_eq(a: str, b: str) -> bool:
    """Gi1/0/19 == GigabitEthernet1/0/19 (one name abbreviates the other, same numbers)."""
    ma, mb = (re.fullmatch(r"([a-z-]*)\s*(\S+)", (x or "").strip().lower()) for x in (a, b))
    if not (ma and mb) or ma[2] != mb[2]:
        return False
    return ma[1].startswith(mb[1]) or mb[1].startswith(ma[1])


def site_link(s, snap, fresh, now) -> dict:
    """The switch's mgmt eth0 -> its geometry site_port, READ from the switch's own LLDP
    (`eth0  turtle-gouda.ehv.lab  Gi1/0/19`): ok when the neighbour's host name (before
    the first dot) and port match, wrong when another neighbour is there, up when the
    switch answers but has no eth0 neighbour, unknown without a fresh snapshot."""
    want_sw, want_port = s.site_port["switch"], s.site_port["port"]
    at = (snap or {}).get("at")
    base = {"at": _iso(at), "switch": s.id, "iface": "eth0"}
    if not fresh:
        return {**base, "state": "unknown", "seen": {"lldp": None, "macs": []},
                "text": f"switch {s.id} last read {int(now - at)} s ago"}
    nbs = [_d(e) for e in snap.get("lldp") or [] if _d(e).get("local_iface") == "eth0"]
    seen = [(e.get("remote_device") or "", e.get("remote_port") or "", e.get("remote_descr") or "")
            for e in nbs]
    base["seen"] = {"lldp": seen[0][0] if seen else None, "macs": []}
    for name, port, descr in seen:
        if name.split(".")[0].lower() == want_sw.lower() and \
                (_port_eq(port, want_port) or _port_eq(descr, want_port)):
            return {**base, "state": "ok", "text": f"eth0 · {name.split('.')[0]} {port or descr}"}
    if seen:
        return {**base, "state": "wrong", "text": "sees " + ", ".join(
            f"{n.split('.')[0] or '?'} {p or d or '?'}" for n, p, d in seen)
            + f" (want {want_sw} {want_port})"}
    return {**base, "state": "up",
            "text": f"answers ssh · no eth0 LLDP (want {want_sw} {want_port})"}


def presence(links: dict) -> str:
    """present if any link is ok/up/wrong; absent if every link is down; else unknown."""
    states = [v["state"] for v in links.values()]
    if any(s in ("ok", "up", "wrong") for s in states):
        return "present"
    if states and all(s == "down" for s in states):
        return "absent"
    return "unknown"


GREEN = frozenset(("ok", "up"))
_SUMMARY_MAX = 4


def link_summary(sw, changes, error=None) -> str:
    """One event line for a switch's link changes [(pos, link, old, new)] in one poll."""
    n = len(changes)
    if all(c[3] == "unknown" for c in changes):
        return (f"switch {sw} not read: {n} link(s) unknown"
                + (f" ({error})" if error else ""))
    if all(c[2] == "unknown" for c in changes) and all(c[3] in GREEN for c in changes):
        return f"switch {sw} read again: {n} link(s) back"
    shown = "; ".join(f"{p} {l} {o} → {w}" for p, l, o, w in changes[:_SUMMARY_MAX])
    more = f"; +{n - _SUMMARY_MAX} more" if n > _SUMMARY_MAX else ""
    return f"links on switch {sw}: {shown}{more}"


def switch_ips(ctx) -> dict[str, str | None]:
    """Switch id -> its SONiC mgmt IP: geometry mgmt_ip, else the lease Discover last
    proved in ANY pass (a fact about the switch: Start fresh does not hide it)."""
    leased = None
    out = {}
    for s in ctx.g.switches:
        ip = s.mgmt_ip
        if not ip:
            if leased is None:
                leased = ctx.db.last_passed(("host-leased",), all_passes=True)
            ip = (leased.get((f"_sw-{s.id}", "host-leased")) or {}).get("ip")
        out[s.id] = ip
    return out


class LinkPoller(threading.Thread):
    """ctx_factory() is called every tick (a re-parsed geometry is picked up)."""

    def __init__(self, ctx_factory, interval_s=INTERVAL_S, name="link-poller", clock=time.time):
        super().__init__(name=name, daemon=True)
        self.ctx_factory = ctx_factory
        self.interval_s = interval_s
        self.clock = clock
        self._stop_ev = threading.Event()
        self._mu = threading.Lock()           # snapshots / errors / in-flight
        self._log_mu = threading.Lock()       # transition logging
        self._snaps: dict[str, dict | None] = {}
        self._errors: dict[str, str | None] = {}
        self._inflight: dict[str, threading.Thread] = {}
        self._attempted: set[str] = set()
        self._last: dict | None = None        # (pos, link) -> state last logged

    # -- views (server) --
    def snapshots(self) -> dict:
        with self._mu:
            return dict(self._snaps)

    def switch_live(self, sid: str, now: float | None = None) -> dict:
        """{"at": last good read (iso) | None, "ok": that read is fresh, "error"}."""
        with self._mu:
            snap, err = self._snaps.get(sid), self._errors.get(sid)
        now = self.clock() if now is None else now
        at = (snap or {}).get("at")
        return {"at": _iso(at), "ok": at is not None and now - at <= STALE_S, "error": err}

    # -- polling --
    def _read(self, ctx, s, ip) -> None:
        snap, err = None, None
        try:
            if not ip:
                err = f"no mgmt IP for switch {s.id} (Discover has not found it)"
            else:
                user, pw = creds(ctx).sonic
                read = getattr(ctx.rungs, "read_live", None) or sonic.read_live
                r = read(ip, user, pw, runner=ctx.runner)
                if r.verdict == "pass":
                    snap = {**r.detail, "at": self.clock()}
                else:
                    err = r.detail.get("headline") or r.detail.get("error") or r.verdict
        except Exception as e:                # noqa: BLE001 -- unknown until the next read
            err = f"{type(e).__name__}: {e}"
        with self._mu:
            self._snaps[s.id] = snap          # a failed read: unknown now, not at 90 s
            self._errors[s.id] = err
            self._attempted.add(s.id)
        try:
            self.log_transitions(ctx)
        except Exception:                     # noqa: BLE001
            pass

    def tick(self, block=False) -> None:
        try:
            ctx = self.ctx_factory()
            ips = switch_ips(ctx)
            started = []
            with self._mu:
                for s in ctx.g.switches:
                    t = self._inflight.get(s.id)
                    if t is not None and t.is_alive():
                        continue              # one read in flight per switch
                    t = threading.Thread(target=self._read, args=(ctx, s, ips.get(s.id)),
                                         name=f"{self.name}-{s.id}", daemon=True)
                    self._inflight[s.id] = t
                    started.append(t)
            for t in started:
                t.start()
            if block:
                for t in started:
                    t.join()
            self.log_transitions(ctx)         # a stale snapshot turns unknown here
        except Exception:                     # noqa: BLE001
            pass

    def log_transitions(self, ctx) -> dict:
        """Evaluate now; append each changed (pos, link) to link_event, and at most one
        order event per switch summarising its changes into/out of a non-green state
        (ok <-> up is identity evidence aging, e.g. an FDB entry: link_event only)."""
        with self._log_mu:
            g, db = ctx.g, ctx.db
            with self._mu:
                snaps, attempted = dict(self._snaps), set(self._attempted)
                errors = dict(self._errors)
            res = evaluate(g, snaps, device_macs(db), device_names(g), self.clock(),
                           db.iom_sides())
            if self._last is None:
                self._last = db.last_link_states()
            by_sw: dict[str, list] = {}
            for pos, links in res.items():
                for link, v in links.items():
                    if v["switch"] not in attempted:
                        continue
                    old = self._last.get((pos, link), "unknown")
                    if old == v["state"]:
                        continue
                    db.add_link_event(pos, link, v["state"], v["text"])
                    self._last[(pos, link)] = v["state"]
                    if not {old, v["state"]} <= GREEN:
                        by_sw.setdefault(v["switch"], []).append((pos, link, old, v["state"]))
            for sw, ch in by_sw.items():
                db.add_event("link", link_summary(sw, ch, errors.get(sw)))
            return res

    def run(self) -> None:
        while not self._stop_ev.is_set():
            self.tick()
            self._stop_ev.wait(self.interval_s)

    def stop(self) -> None:
        self._stop_ev.set()
