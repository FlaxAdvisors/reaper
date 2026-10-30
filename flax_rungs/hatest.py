"""ha-test: whole-wedge failover of the rack's bond0s (operator-confirmed 2026-10-30).

The wedges have no inter-switch link (no MLAG peer cable), so ONE node failing over
to wedge B cannot reach the nodes still on A. The test therefore simulates the loss
of a whole wedge: every participating node's wedge-A port goes down at once
(`ip link set <port> down`, launched in parallel), every bond must fail over to its
B port, and the iperf mesh over bond0 (server = geometry iperf_server) must pass.
Each client pings the server's bond IP every 100 ms from before the cut until after
it; the longest run of lost replies is that node's failover gap. Then A comes back
up (`ip link set up`) and every bond must return to its primary. The same again
cutting B: the active slave must stay on A.

Links are ALWAYS restored in a finally. A node whose restore could not be confirmed
fails loudly (its port may be left down). Each cut also arms a dead-man on the node
(a setsid `sleep DEADMAN_S; ip link set <port> up`, pid in /run) that the normal
restore kills first: a daemon that dies mid-cut cannot leave fabric ports down. Ports
are found by the permanent MAC in /proc/net/bonding/bond0, never by name.

Verdicts per node: fail on a real defect (no failover, a gap over FAILOVER_GAP_MS, no
reply at all, mesh below threshold, no return to primary); unmeasured when the node
(or the server) did not answer and nothing else was wrong; pass otherwise. The iperf
server is judged on its own failover and restore: a client's missing iperf result is
that client's defect (the server's mesh verdict stays evidence), unless the server could
not start iperf3 at all."""
import shlex
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import bond, hostssh, iperf
from .remote import unreachable
from .result import UNMEASURED, RungResult

FAILOVER_GAP_MS = 1000
PING_S = 12             # each ping runs this long (covers lead + cut + recovery)
LEAD_S = 2              # pings run this long before the cut
SETTLE_S = 10           # failover must show in /proc/net/bonding within this
RESTORE_S = 30          # a re-enabled port (100G link training) must be back by then
DEADMAN_S = 600         # on-node self-restore of a cut port; well above a leg's cut window
NOTES = ["whole-wedge failover: the wedge-A (then wedge-B) port of every node was cut "
         "at once; the wedges have no inter-switch link",
         "single-node failover needs an A↔B interconnect (MLAG peer link)",
         f"HA mode delivered: {bond.HA_MODE}"]


def _par(fn, items) -> dict:
    items = list(items)
    if not items:
        return {}
    with ThreadPoolExecutor(max_workers=len(items)) as ex:
        return dict(zip([x.pos for x in items], ex.map(fn, items)))


def _ping_file(cut: str) -> str:
    return f"/run/flax-hatest-cut{cut}.ping"


def _deadman_file(cut: str) -> str:
    return f"/run/flax-hatest-cut{cut}.deadman"


class _Node:
    def __init__(self, n):
        self.n = n
        self.ports: dict[str, str] = {}
        self.problems: list[str] = []
        self.unanswered: list[str] = []
        self.legs: dict[str, dict] = {}
        self.rows: list[list[str]] = []
        self.arts: dict[str, str] = {}

    @property
    def pos(self):
        return self.n["pos"]


def _gbit(x):
    return "-" if x is None else f"{x / 1e9:.1f}"


class _Run:
    def __init__(self, nodes, server_pos, user, pw, mesh, runner, sleep, clock, ping_s,
                 lead_s, settle_s, restore_s, mesh_seconds, deadman_s):
        self.st = {n["pos"]: _Node(n) for n in nodes}
        self.server_pos = server_pos
        self.user, self.pw, self.mesh, self.runner = user, pw, mesh, runner
        self.sleep, self.clock = sleep, clock
        self.ping_s, self.lead_s = ping_s, lead_s
        self.settle_s, self.restore_s, self.mesh_seconds = settle_s, restore_s, mesh_seconds
        self.deadman_s = deadman_s

    def ssh(self, x: _Node, script: str, timeout: int = 60):
        return hostssh.run(x.n["host_ip"], self.user, self.pw, script, timeout=timeout,
                           runner=self.runner)

    def status(self, x: _Node):
        return bond.status(x.n["host_ip"], self.user, self.pw, runner=self.runner)

    # -- before anything is cut: every bond at its primary, ports by permanent MAC
    def precheck(self) -> list[_Node]:
        ready = []
        for pos, (rc, b, _addr, raw) in _par(self.status, self.st.values()).items():
            x = self.st[pos]
            if unreachable(rc):
                x.unanswered.append(f"{x.n['host_ip']} did not answer ssh (rc={rc})")
                continue
            if not b:
                x.problems.append("no bond0 (ha-config not applied?)")
                continue
            for side in ("A", "B"):
                x.ports[side] = bond.slave_by_mac(b, bond._mac(x.n["macs"][side]))
            missing = [s for s in ("A", "B") if not x.ports[s]]
            if missing:
                x.problems.append("bond0 has no slave with the wedge-"
                                  + "/".join(missing) + " MAC")
                continue
            down = [n for n, s in b["slaves"].items() if s["mii"] != "up"]
            if b["mode"] != "active-backup" or b["active"] != x.ports["A"] or down:
                x.problems.append(f"bond0 not at its primary before the cut (mode {b['mode']}, "
                                  f"active {b['active']}, down {down or '-'})")
                continue
            x.legs["before"] = {"active": b["active"]}
            ready.append(x)
        return ready

    def _wait_active(self, xs, want: dict[str, str], deadline_s: int, need_up=None) -> dict:
        """pos -> (rc, bond) once every node shows active == want[pos] (and, when
        need_up, both slaves MII up), or at the deadline."""
        t0, last = self.clock(), {}
        pending = list(xs)
        while pending:
            res = _par(self.status, pending)
            for pos, (rc, b, _a, raw) in res.items():
                last[pos] = (rc, b, raw)
            done = [x for x in pending if (b := last[x.pos][1]) and b["active"] == want[x.pos]
                    and (not need_up or all(s["mii"] == "up" for s in b["slaves"].values()))]
            pending = [x for x in pending if x not in done]
            if not pending or self.clock() - t0 >= deadline_s:
                break
            self.sleep(1)
        return last

    # -- one leg: cut `cut` on every node, the other side must carry the traffic
    def leg(self, cut: str, xs: list[_Node], server: _Node | None):
        keep = "B" if cut == "A" else "A"
        sip = server.n["bond_ip"]
        f = shlex.quote(_ping_file(cut))
        pingers = [x for x in xs if x.pos != self.server_pos]
        started = _par(lambda x: self.ssh(x, (
            f"rm -f {f}\nsetsid ping -n -i {bond.PING_MS / 1000} -w {self.ping_s} "
            f"{shlex.quote(sip)} >{f} 2>&1 </dev/null &\necho STARTED\n")),
            pingers)
        t_ping = self.clock()
        for x in xs:
            x.legs[cut] = {"cut": x.ports[cut], "active_after": None, "gap_ms": None,
                           "ping": None, "tx_bps": None, "rx_bps": None, "returned": None}
        barrier = threading.Barrier(len(xs))
        cut_res: dict = {}
        try:
            self.sleep(self.lead_s)

            def do_cut(x):
                try:
                    barrier.wait(timeout=30)
                except threading.BrokenBarrierError:
                    pass
                port, dm = shlex.quote(x.ports[cut]), shlex.quote(_deadman_file(cut))
                up = shlex.quote(f"sleep {int(self.deadman_s)}; ip link set {port} up")
                # the dead-man first: should this daemon die, the node restores itself
                return self.ssh(x, f"setsid sh -c {up} >/dev/null 2>&1 </dev/null &\n"
                                   f"echo $! >{dm}\nip link set {port} down && echo CUT\n")

            cut_res = _par(do_cut, xs)
            # failover: the kept side must be (or stay) active
            after = self._wait_active(xs, {x.pos: x.ports[keep] for x in xs}, self.settle_s)
            for x in xs:
                rc, out, err = cut_res[x.pos]
                lg = x.legs[cut]
                if unreachable(rc):
                    x.unanswered.append(f"cut {cut}: {x.n['host_ip']} did not answer ssh")
                elif "CUT" not in out:
                    x.problems.append(f"cut {cut}: `ip link set {x.ports[cut]} down` failed "
                                      f"(rc={rc}): {hostssh.tail(out, err, 120)}")
                arc, b, raw = after.get(x.pos, (None, None, ""))
                x.arts[f"bond0-cut-{cut}.txt"] = raw
                if arc is None or unreachable(arc):
                    x.unanswered.append(f"cut {cut}: bond0 not read")
                    continue
                lg["active_after"] = b["active"] if b else None
                if lg["active_after"] != x.ports[keep]:
                    x.problems.append(
                        f"cut {cut}: no failover to {x.ports[keep]} (active "
                        f"{lg['active_after']})" if cut == "A" else
                        f"cut {cut}: active moved to {lg['active_after']} "
                        f"(want {x.ports[keep]})")
            # the pings run out on their own (-w); collect them
            rest = self.ping_s + 1 - (self.clock() - t_ping)
            if rest > 0:
                self.sleep(rest)
            got = _par(lambda x: self.ssh(x, f"cat {f}\nrm -f {f}\n"), pingers)
            for x in pingers:
                lg = x.legs[cut]
                src, srco, _ = started[x.pos]
                rc, out, _ = got[x.pos]
                if unreachable(src) or unreachable(rc) or "STARTED" not in srco:
                    x.unanswered.append(f"cut {cut}: failover ping not measured")
                    continue
                x.arts[f"ping-cut-{cut}.txt"] = out
                pg = bond.parse_ping(out)
                lg["ping"], lg["gap_ms"] = pg, pg["max_gap_ms"]
                if not pg["received"]:
                    x.problems.append(f"cut {cut}: no reply from the server's bond0 {sip}")
                elif pg["max_gap_ms"] > FAILOVER_GAP_MS:
                    x.problems.append(f"cut {cut}: failover gap {pg['max_gap_ms']} ms "
                                      f"(> {FAILOVER_GAP_MS} ms)")
            # iperf mesh over bond0 while the cut side is down
            srv = {"pos": server.pos, "host_ip": server.n["host_ip"],
                   "ifaces": {"bond": ("bond0", sip)}}
            clients = [{"pos": x.pos, "host_ip": x.n["host_ip"],
                        "ifaces": {"bond": ("bond0", x.n["bond_ip"])}} for x in pingers]
            crashed = False
            try:
                mres = self.mesh(srv, clients, user=self.user, pw=self.pw,
                                 seconds=self.mesh_seconds, runner=self.runner)
            except Exception as e:      # noqa: BLE001 -- the restore below must still run
                crashed = True
                mres = {x.pos: RungResult("fail", {"error": f"{type(e).__name__}: {e}"})
                        for x in xs}
            for x in xs:
                r = mres.get(x.pos) or RungResult("fail", {"error": "no iperf result"})
                net = r.detail.get("bond") or {}
                x.legs[cut]["tx_bps"], x.legs[cut]["rx_bps"] = net.get("tx_bps"), net.get("rx_bps")
                x.legs[cut]["iperf"] = r.verdict
                if x.pos == self.server_pos:
                    # evidence only: a client without a result is that client's defect
                    err = r.detail.get("error") or ""
                    x.legs[cut]["iperf"] = err or r.verdict
                    if r.verdict == "fail" and (crashed or
                                                err.startswith(f"server {x.pos} not ready")):
                        x.problems.append(f"cut {cut}: iperf server: {err}")
                    continue
                if r.verdict == UNMEASURED:
                    x.unanswered.append(f"cut {cut}: iperf {r.detail.get('error')}")
                elif r.verdict == "fail":
                    x.problems.append(f"cut {cut}: iperf over bond0: {r.detail.get('error')}")
        finally:
            self.restore(cut, xs)

    # -- always: bring the cut ports back, then the bond must return to its primary
    def restore(self, cut: str, xs: list[_Node]):
        dm = shlex.quote(_deadman_file(cut))
        res = _par(lambda x: self.ssh(
            x, f"kill \"$(cat {dm} 2>/dev/null)\" 2>/dev/null; rm -f {dm}\n"
               f"ip link set {shlex.quote(x.ports[cut])} up && echo UP\n"), xs)
        up = []
        for x in xs:
            rc, out, err = res[x.pos]
            if "UP" not in out:
                x.problems.append(f"restore failed: {x.ports[cut]} may be left DOWN "
                                  f"(rc={rc}) {hostssh.tail(out, err, 120)}".rstrip())
            else:
                up.append(x)
        back = self._wait_active(up, {x.pos: x.ports["A"] for x in up}, self.restore_s,
                                 need_up=True)
        for x in up:
            rc, b, _raw = back.get(x.pos, (None, None, ""))
            x.legs[cut]["returned"] = b["active"] if b else None
            if not b or b["active"] != x.ports["A"] or \
                    any(s["mii"] != "up" for s in b["slaves"].values()):
                x.problems.append(f"{cut} restored: bond0 did not return to its primary "
                                  f"{x.ports['A']} with both ports up (active "
                                  f"{b and b['active']})")

    def result(self, x: _Node) -> RungResult:
        rows = []
        for cut in ("A", "B"):
            lg = x.legs.get(cut)
            if not lg:
                continue
            before = x.legs["before"]["active"] if cut == "A" else x.legs["A"].get("returned")
            rows.append([f"cut {cut} ({lg['cut']}): active", f"{before} → {lg['active_after']}"])
            if x.pos == self.server_pos:
                rows.append([f"cut {cut}: failover gap", "n/a (iperf server)"])
            elif lg["ping"]:
                p = lg["ping"]
                rows.append([f"cut {cut}: failover gap",
                             f"≈{lg['gap_ms']} ms ({p['lost']}/{p['sent']} lost)"])
            else:
                rows.append([f"cut {cut}: failover gap", "not measured"])
            rows.append([f"cut {cut}: iperf over bond0" if x.pos != self.server_pos
                         else f"cut {cut}: iperf server",
                         f"tx {_gbit(lg['tx_bps'])} / rx {_gbit(lg['rx_bps'])} Gbit/s"
                         if x.pos != self.server_pos else lg.get("iperf") or "-"])
            rows.append([f"{cut} restored: active", f"{lg['returned']}"])
        det = {"ha_mode": bond.HA_MODE, "ports": x.ports, "legs": x.legs, "rows": rows,
               "notes": list(NOTES)}
        if x.problems:
            why = "; ".join(x.problems)
            return RungResult("fail", {**det, "headline": x.problems[0], "error": why},
                              x.arts)
        if x.unanswered:
            why = "; ".join(x.unanswered)
            return RungResult(UNMEASURED, {**det, "headline": x.unanswered[0], "reason": why,
                                           "error": why}, x.arts)
        gaps = [lg["gap_ms"] for lg in (x.legs.get("A"), x.legs.get("B"))
                if lg and lg["gap_ms"] is not None]
        head = (f"failover gap ≤{max(gaps)} ms" if gaps else "iperf server") + \
            f", back on {x.ports['A']}"
        return RungResult("pass", {**det, "headline": head}, x.arts)


def run(nodes: list[dict], server_pos: str, *, user: str, pw: str, mesh=iperf.mesh,
        runner=subprocess.run, sleep=time.sleep, clock=time.monotonic,
        ping_s: int = PING_S, lead_s: int = LEAD_S, settle_s: int = SETTLE_S,
        restore_s: int = RESTORE_S, mesh_seconds: int = 10,
        deadman_s: int = DEADMAN_S) -> dict[str, RungResult]:
    """nodes: [{"pos", "host_ip", "bond_ip", "macs": {"A", "B"}}] (bond_ip without the
    prefix; macs as bond.sides()). server_pos must be one of them. One RungResult per pos."""
    R = _Run(nodes, server_pos, user, pw, mesh, runner, sleep, clock, ping_s, lead_s,
             settle_s, restore_s, mesh_seconds, deadman_s)
    ready = R.precheck()
    server = next((x for x in ready if x.pos == server_pos), None)
    if server is None:
        sx = R.st.get(server_pos)
        why = (f"iperf server {server_pos} not ready: "
               + ("; ".join(sx.problems + sx.unanswered) if sx else "not in this run"))
        for x in ready:
            x.unanswered.append(why)
        return {p: R.result(x) for p, x in R.st.items()}
    R.leg("A", ready, server)
    # only bonds back at their primary go on to cut B (and the server must be one)
    ready_b = [x for x in ready if not x.problems and x.legs["A"].get("returned") == x.ports["A"]]
    if server in ready_b:
        R.leg("B", ready_b, server)
    else:
        for x in ready_b:
            x.unanswered.append(f"cut B: iperf server {server_pos} did not come back from cut A")
    return {p: R.result(x) for p, x in R.st.items()}
