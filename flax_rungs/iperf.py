"""iperf3 mesh: the rack's server node (geometry.iperf_server) serves one daemon per
network (mgmt/A/B); clients run one at a time, both directions per network, judged
against 0.8 x the client NIC's ethtool speed. Never the bang, never an IP outside the
server dict. The image has no pkill/pidof/ps, so processes are found via /proc."""
import json
import re
import shlex
import subprocess

from . import hostssh
from .result import UNMEASURED, RungResult

NETS = ("mgmt", "A", "B")
THRESHOLD = 0.8

# bash: print "PID <pid>" for every iperf3 whose argv carries exactly `-B <ip> -p <port>`
_FIND = r'''for d in /proc/[0-9]*; do
  c=$(tr '\0' ' ' < $d/cmdline 2>/dev/null) || continue
  case "$c" in *iperf3*" -B $IP -p $PORT "*) echo "PID ${d#/proc/}";; esac
done'''


def _find(ip: str, port: int) -> str:
    return f"IP={shlex.quote(ip)} PORT={port}\n{_FIND}\n"


def _pids(out: str) -> list[str]:
    return re.findall(r"^PID (\d+)$", out, re.M)


def _ports(server: dict, port: int) -> dict[str, tuple[str, int]]:
    return {n: (server["ifaces"][n][1], port + k) for k, n in enumerate(NETS)
            if n in server["ifaces"]}


def _start(server, ports, user, pw, runner) -> tuple[bool, str, dict]:
    pids = {}
    for net, (ip, p) in ports.items():
        script = (f"iperf3 -s -D -B {shlex.quote(ip)} -p {p} >/dev/null 2>&1\nsleep 1\n"
                  + _find(ip, p))
        rc, out, err = hostssh.run(server["host_ip"], user, pw, script, runner=runner)
        if rc != 0 or not _pids(out):
            return False, f"{net} {ip}:{p} rc={rc} {hostssh.tail(out, err, 120)}", pids
        pids[net] = _pids(out)
    return True, "", pids


def _stop(server, ports, user, pw, runner):
    for ip, p in ports.values():
        script = _find(ip, p).replace('echo "PID ${d#/proc/}"', 'kill ${d#/proc/}')
        hostssh.run(server["host_ip"], user, pw, script, runner=runner)


def _speed(client, ifname, user, pw, runner) -> float | None:
    rc, out, _ = hostssh.run(client["host_ip"], user, pw,
                             f"ethtool {shlex.quote(ifname)}\n", runner=runner)
    m = re.search(r"Speed:\s*(\d+)\s*Mb/s", out)
    return int(m.group(1)) * 1e6 if m else None


def _bps(client, cip, sip, p, reverse, seconds, streams, user, pw, runner):
    script = (f"iperf3 -c {shlex.quote(sip)} -p {p} -B {shlex.quote(cip)} "
              f"-t {seconds} -P {streams} -J{' -R' if reverse else ''}\n")
    rc, out, err = hostssh.run(client["host_ip"], user, pw, script,
                               timeout=seconds + 30, runner=runner)
    try:                    # stdout only; ssh/sudo noise is on stderr
        j, _ = json.JSONDecoder().raw_decode(out[out.index("{"):])
        if j.get("error"):
            return None, str(j["error"])
        return float(j["end"]["sum_received"]["bits_per_second"]), ""
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, f"no iperf result (rc={rc}): {hostssh.tail(out, err, 120)}"


def _client(client, sports, server, user, pw, seconds, streams, runner):
    nets, errs, served = {}, [], True
    for net, (ifname, cip) in client["ifaces"].items():
        if net not in sports:
            nets[net] = {"tx_bps": None, "rx_bps": None, "speed_bps": None, "ok": False}
            errs.append(f"{net}: server has no {net} address")
            served = False
            continue
        sip, p = sports[net]
        speed = _speed(client, ifname, user, pw, runner)
        tx, e1 = _bps(client, cip, sip, p, False, seconds, streams, user, pw, runner)
        rx, e2 = _bps(client, cip, sip, p, True, seconds, streams, user, pw, runner)
        ok = bool(speed) and tx is not None and rx is not None and \
            tx >= THRESHOLD * speed and rx >= THRESHOLD * speed
        nets[net] = {"tx_bps": tx, "rx_bps": rx, "speed_bps": speed, "ok": ok}
        if tx is None or rx is None:
            served = False
        if not speed:
            errs.append(f"{net}: no link speed from ethtool {ifname}")
        elif not ok:
            errs.append(f"{net}: tx {tx and round(tx / 1e6)} / rx {rx and round(rx / 1e6)} "
                        f"Mbit/s < {round(THRESHOLD * speed / 1e6)} (0.8 x {round(speed / 1e6)})"
                        + (f" [{e1 or e2}]" if (e1 or e2) else ""))
    if not nets:
        errs.append("client has no networks")
    return nets, errs, served


def mesh(server: dict, clients: list[dict], *, user: str, pw: str, port: int = 11001,
         seconds: int = 10, streams: int = 4, runner=subprocess.run) -> dict[str, RungResult]:
    out: dict[str, RungResult] = {}
    ports = _ports(server, port)
    if not ports:
        msg = f"server {server['pos']} not ready: no networks"
        return {c["pos"]: RungResult(UNMEASURED, {"error": msg, "headline": msg, "reason": msg})
                for c in clients} | {server["pos"]: RungResult("fail", {"error": msg})}
    try:                    # always stop, even on a partial start or an error
        ok, why, pids = _start(server, ports, user, pw, runner)
        if not ok:          # no server to measure against: no client was judged
            msg = f"server {server['pos']} not ready: {why}"
            for c in clients:
                out[c["pos"]] = RungResult(UNMEASURED, {"error": msg, "headline": msg,
                                                        "reason": msg})
            out[server["pos"]] = RungResult("fail", {"error": msg})
            return out
        all_served = True
        for c in clients:                       # strictly one at a time
            nets, errs, served = _client(c, ports, server, user, pw, seconds, streams, runner)
            all_served &= served
            good = not errs and all(n["ok"] for n in nets.values())
            out[c["pos"]] = RungResult("pass" if good else "fail",
                                       {**nets, **({"error": "; ".join(errs)} if errs else {})})
        if not clients:
            out[server["pos"]] = RungResult("skip", {"served": []})
        else:
            out[server["pos"]] = RungResult(
                "pass" if all_served else "fail",
                {"served": [c["pos"] for c in clients], "server_pids": pids,
                 **({} if all_served else {"error": "a client got no iperf result"})})
        return out
    finally:
        _stop(server, ports, user, pw, runner)
