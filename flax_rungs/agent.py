"""Drive the post.tgz qual agent on a live node: launch over ssh, poll, collect."""
import re
import time
from pathlib import Path

from flax_post.qualclient import QualClient, QualUnreachable

from . import hostssh
from .result import UNMEASURED, RungResult

# Every default stage except iperf (the mesh rung owns iperf). Order is the agent's own.
ORDER_STAGES = ("sdr-pre,sel-pre,sel-clear,tooling,inventory,fio,mem-pre,"
                "cpu-mem-stress,mem-post,sdr-post,sel-post")

AGENT_PORT = 8087
POST_URL = "http://bang/post.tgz"
PORT_WAIT_S = 60            # launch waits this long for the agent's port (the poll keeps on)
_TERMINAL = ("pass", "fail", "skip")
_ACTIVE = "FLAX_AGENT_ACTIVE"
_STAGES_RE = re.compile(r"^[a-z0-9,-]+$")


def launch_script(stages: str) -> str:
    """Mirror of flax_post.observe.host_qual._LAUNCH_SH, plus the stage selection.
    post.sh reads <dir of post.sh>/flax-qual.env (= /opt/flax/hook here); older
    FLAX_QUAL_STAGES lines are dropped so a relaunch never stacks selections."""
    if not _STAGES_RE.match(stages):
        raise ValueError(f"bad stage list {stages!r}")
    # FLAXEV <step> <text> lines are the evidence rows (launch_rows); a failure also
    # prints the node's addresses + routes between FLAXNET markers (the `net` artifact)
    return (
        "echo \"FLAXEV ssh up $(cut -d' ' -f1 /proc/uptime 2>/dev/null)\"\n"
        f"systemctl is-active --quiet flax-qual-agent && {{ echo {_ACTIVE}; exit 0; }}\n"
        "net() { echo FLAXNET_BEGIN; ip -br a; echo; ip r; echo FLAXNET_END; }\n"
        "set -e\n"
        "mkdir -p /opt/flax/hook && cd /opt/flax/hook\n"
        # up to 2 min: a fresh live image's network can need >15 s to settle (L37, 2026-10-30:
        # the fetch worked 12 s after 5 tries x 3 s had given up, twice)
        f"t0=$SECONDS; ok=0; n=0; for i in $(seq 1 40); do n=$i; curl -sSf --max-time 10 {POST_URL} -o post.tgz 2>curl.err && ok=1 && break; sleep 3; done\n"
        "ip=$(getent hosts bang | awk '{print $1; exit}')\n"
        f"echo \"FLAXEV fetch {POST_URL} -> ${{ip:-unresolved}}, $n tries, $((SECONDS-t0)) s"
        "$([ $ok = 1 ] || echo \"; last: $(tail -n1 curl.err 2>/dev/null)\")\"\n"
        "[ \"$ok\" = 1 ] || { echo 'post.tgz fetch failed for 2 min'; net; exit 2; }\n"
        "tar xzf post.tgz || { echo 'FLAXEV unpack tar xzf post.tgz failed'; exit 3; }\n"
        "echo 'FLAXEV unpack post.tgz unpacked'\n"
        "touch flax-qual.env && sed -i '/^FLAX_QUAL_STAGES=/d' flax-qual.env\n"
        f"echo 'FLAX_QUAL_STAGES={stages}' >> flax-qual.env\n"
        "./post.sh postautomate || { rc=$?; echo \"FLAXEV launch post.sh postautomate rc=$rc\"; net; exit 4; }\n"
        "for i in $(seq 1 10); do systemctl is-active --quiet flax-qual-agent && break; sleep 1; done\n"
        "echo \"FLAXEV launch post.sh postautomate started, flax-qual-agent $(systemctl is-active flax-qual-agent)\"\n"
    )


_STOP_SH = (
    "systemctl stop flax-qual-agent\n"
    "for i in $(seq 1 20); do systemctl is-active --quiet flax-qual-agent || exit 0; sleep 1; done\n"
    "echo 'flax-qual-agent still active after stop'; exit 3\n"
)


def stop(host_ip, user, password, *, runner=None) -> RungResult:
    """Stop a running battery (e.g. after a poll timeout, so stress cannot overlap the
    iperf mesh). Bounded: fails when the unit is still active 20 s later."""
    kw = {"runner": runner} if runner else {}
    rc, out, err = hostssh.run(host_ip, user, password, _STOP_SH, timeout=45, **kw)
    if rc != 0:
        return RungResult("fail", {"error": f"agent stop rc={rc}",
                                   "output": hostssh.tail(out, err)})
    return RungResult("pass", {"stopped": True})


_EV = re.compile(r"^FLAXEV (\S+) ?(.*)$", re.M)
_NET = re.compile(r"FLAXNET_BEGIN\n(.*?)FLAXNET_END", re.S)


def launch_rows(user, host_ip, out) -> list[list[str]]:
    """The evidence rows of a launch script's FLAXEV lines: ssh, fetch, unpack, launch."""
    rows = []
    for step, text in _EV.findall(out or ""):
        if step == "ssh":
            up = text.removeprefix("up").strip()
            text = f"{user}@{host_ip} ok" + (f" · up {up.split('.')[0]} s" if up else "")
        rows.append([step, text.strip()])
    return rows


def _wait_port(client, *, wait_s, sleep, clock) -> float | None:
    """Seconds until the agent's port answered /health, None if not within wait_s."""
    t0 = clock()
    while True:
        try:
            client.health()
            return clock() - t0
        except (QualUnreachable, KeyError, TypeError):
            pass
        if clock() - t0 >= wait_s:
            return None
        sleep(2)


def launch(host_ip, user, password, *, stages=ORDER_STAGES, runner=None,
           client_factory=QualClient, wait_s=PORT_WAIT_S, sleep=time.sleep,
           clock=time.monotonic) -> RungResult:
    kw = {"runner": runner} if runner else {}
    script = launch_script(stages)
    rc, out, err = hostssh.run(host_ip, user, password, script, timeout=180, **kw)   # covers the 2 min post.tgz fetch window
    rows = launch_rows(user, host_ip, out)
    if rc != 0:
        if not rows:
            rows = [["ssh", f"{user}@{host_ip} rc={rc}: {hostssh.tail('', err, 160)}"]]
        net = _NET.search(out or "")
        said = [ln for ln in _NET.sub("", out or "").splitlines()
                if ln.strip() and not ln.startswith("FLAX")]
        head = f"agent launch rc={rc}" + (f": {said[-1]}" if said else "")
        return RungResult("fail", {"error": f"agent launch rc={rc}", "headline": head,
                                   "rows": rows, "output": hostssh.tail(out, err)},
                          {"net": net.group(1)} if net else {})
    if _ACTIVE not in out:
        client = client_factory(f"http://{host_ip}:{AGENT_PORT}")
        s = _wait_port(client, wait_s=wait_s, sleep=sleep, clock=clock)
        rows.append(["agent port", f"{AGENT_PORT} answered after {s:.0f} s" if s is not None
                     else f"{AGENT_PORT} not answering after {wait_s} s (the poll keeps trying)"])
        return RungResult("pass", {"mode": "fresh", "stages": stages, "rows": rows})
    # Already running from an earlier Qualify. An agent reads FLAX_QUAL_STAGES only at
    # process start, so if its stage list differs from the request (e.g. a full default
    # list would run iperf against the bang) it is stopped and launched fresh; if it
    # matches, the agent's restart contract (restart -> ack) gives a fresh run.
    client = client_factory(f"http://{host_ip}:{AGENT_PORT}")
    want = stages.split(",")
    try:
        have = [s["name"] for s in client.stages()]
    except (QualUnreachable, KeyError, TypeError):
        have = None                         # can't tell: don't trust it, relaunch
    if have == want:
        try:
            ids = client.restart()
            client.restart_ack()
        except QualUnreachable as e:
            return RungResult("fail", {"error": f"agent active but restart failed: {e}"})
        return RungResult("pass", {"mode": "restart", "stages": stages, "rows": rows,
                                   "old_run_id": ids.get("old_run_id"),
                                   "new_run_id": ids.get("new_run_id")})
    rc, out, err = hostssh.run(host_ip, user, password, _STOP_SH, timeout=45, **kw)
    if rc != 0:
        return RungResult("fail", {"error": f"agent stop rc={rc}",
                                   "output": hostssh.tail(out, err)})
    rc, out, err = hostssh.run(host_ip, user, password, script, timeout=60, **kw)
    if rc != 0 or _ACTIVE in out:
        return RungResult("fail", {"error": f"agent relaunch rc={rc}",
                                   "output": hostssh.tail(out, err)})
    return RungResult("pass", {"mode": "relaunch-stage-mismatch", "stages": stages,
                               "had_stages": have, "rows": launch_rows(user, host_ip, out)})


def _collect(client, name, st, run_id) -> RungResult:
    arts, lost = {}, []
    for a in (st.get("artifacts") or []):
        try:
            arts[a["name"]] = client.stage_artifact(name, a["name"])
        except QualUnreachable:
            lost.append(a["name"])
    detail = {"stage": name, "run_id": run_id, "summary": st.get("summary") or {}}
    if lost:
        detail["unfetched_artifacts"] = lost
    return RungResult(st.get("verdict") or st.get("status"), detail, arts)


def poll(host_ip, *, deadline_s: int, client_factory=QualClient, poll_s=20,
         sleep=time.sleep, clock=time.monotonic) -> dict:
    client = client_factory(f"http://{host_ip}:{AGENT_PORT}")
    end = clock() + deadline_s
    last_err = "agent never answered"
    names, run_id, answered = [], None, False
    while True:
        try:
            h = client.health()
            answered = True
            if not h.get("ok"):
                return {"agent": RungResult("fail", {"error": f"agent unhealthy: {h.get('error')}",
                                                     "run_id": h.get("run_id")})}
            run_id = h.get("run_id")
            stages = client.stages()
            names = [s["name"] for s in stages]
            if stages and all(s["status"] in _TERMINAL for s in stages):
                # Re-read health: a restart ack between the two reads means another run.
                if client.health().get("run_id") == run_id:
                    out = {}
                    for n in names:
                        out[n] = _collect(client, n, client.stage(n), run_id)
                    return out
        except (QualUnreachable, KeyError, TypeError) as e:
            last_err = str(e)
        if clock() + poll_s >= end:
            break
        sleep(poll_s)
    if not names:
        if not answered:    # not one health read in the deadline: nothing was judged
            head = f"agent never answered in {deadline_s}s: {last_err}"
            return {"agent": RungResult(UNMEASURED, {"error": "timeout", "last": last_err,
                                                     "headline": head, "reason": head})}
        return {"agent": RungResult("fail", {"error": "timeout", "last": last_err})}
    out = {}
    for n in names:                         # deadline: keep what finished, fail the rest
        try:
            st = client.stage(n)
        except QualUnreachable:
            st = None
        if st and st.get("status") in _TERMINAL:
            out[n] = _collect(client, n, st, run_id)
        else:
            out[n] = RungResult("fail", {"stage": n, "error": "timeout", "run_id": run_id,
                                         "last": last_err})
    return out


def save_artifacts(order_dir, pos, run_id, stage, artifacts) -> list:
    base = Path(order_dir) / "artifacts" / str(pos) / str(run_id) / stage
    base.mkdir(parents=True, exist_ok=True)
    rel = []
    for name, text in artifacts.items():
        p = base / f"{name}.txt"
        p.write_text(text)
        rel.append(str(p.relative_to(order_dir)))
    return rel
