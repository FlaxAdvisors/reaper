import re, threading, types, pytest
pytest.importorskip("flax_rungs")
from flax_rungs import bond, hatest
from flax_rungs.result import RungResult

P0, P1, MZ = "enp25s0f0np0", "enp25s0f1np1", "enp94s0np0"


def mac(host_octet, k):
    return f"b8:59:9f:00:{host_octet:02x}:{k:02x}"


def ping_text(ip, gap_ms=0, sent=120, received=None):
    lost = set(range(30, 30 + gap_ms // 100))
    lines = [f"PING {ip} ({ip}) 56(84) bytes of data."]
    got = 0
    for s in range(1, sent + 1):
        if s in lost or (received == 0):
            continue
        got += 1
        lines.append(f"64 bytes from {ip}: icmp_seq={s} ttl=64 time=0.12 ms")
    lines += [f"--- {ip} ping statistics ---",
              f"{sent} packets transmitted, {got} received, 0% packet loss, time 11900ms"]
    return "\n".join(lines) + "\n"


class Node:
    def __init__(self, ip, octet, a_port=P0):
        self.ip, self.a_port = ip, a_port
        self.b_port = P1 if a_port == P0 else P0
        self.macs = {P0: mac(octet, 0), P1: mac(octet, 1), MZ: mac(octet, 9)}
        self.up = {P0: True, P1: True}
        self.bonded = True
        self.active = a_port
        self.no_failover = False
        self.restore_fails = False
        self.gap = {"A": 300, "B": 0}
        self.yaml = None            # the apply script (named for the netplan days)
        self.netplan_other = None   # another /etc/netplan file naming a fabric port
        self.netfile = {}           # link -> a Network File other than ours
        self.raised = []

    def proc(self):
        if not self.bonded:
            return "cat: /proc/net/bonding/bond0: No such file or directory\n"
        out = ["Ethernet Channel Bonding Driver: v6.8.0", "",
               "Bonding Mode: fault-tolerance (active-backup)",
               f"Primary Slave: {self.a_port} (primary_reselect always)",
               f"Currently Active Slave: {self.active or 'None'}",
               "MII Status: up", "MII Polling Interval (ms): 100", ""]
        for p in (P0, P1):
            out += [f"Slave Interface: {p}", f"MII Status: {'up' if self.up[p] else 'down'}",
                    "Speed: 100000 Mbps", f"Permanent HW addr: {self.macs[p]}",
                    "Slave queue ID: 0", ""]
        return "\n".join(out)

    def relink(self):
        if self.no_failover:
            return
        if self.up[self.a_port]:
            self.active = self.a_port
        elif not self.up.get(self.active, False):
            self.active = next((p for p in (P0, P1) if self.up[p]), None)


class Rack:
    def __init__(self, nodes):
        self.nodes = {n.ip: n for n in nodes}
        self.dead = set()
        self.log = []
        self.lock = threading.Lock()
        self.leg = "A"

    def __call__(self, argv, input="", **kw):
        ip = argv[-5].split("@")[1]
        n = self.nodes[ip]
        with self.lock:
            self.log.append((ip, input))
        if ip in self.dead:
            return types.SimpleNamespace(returncode=255, stdout="", stderr="No route to host")
        out = ""
        if "/proc/net/bonding/bond0" in input:
            out = n.proc() + "===ADDR\n" + (f"9: bond0    inet 10.201.0.{n.ip.rsplit('.',1)[1]}/16 "
                                            "brd 10.201.255.255 scope global bond0\n")
        elif m := re.search(r"ip link set (\S+) down", input):
            n.up[m.group(1)] = False
            n.relink()
            out = "CUT\n"
        elif m := re.findall(r"ip link set dev (\S+) up", input):
            n.raised += m
            out = "UP\n"
        elif m := re.search(r"ip link set (\S+) up", input):
            if n.restore_fails:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="RTNETLINK busy")
            n.up[m.group(1)] = True
            n.relink()
            out = "UP\n"
        elif "setsid ping" in input:
            out = "STARTED\n"
        elif "cat /run/flax-hatest-cut" in input:
            leg = re.search(r"cut([AB])\.ping", input).group(1)
            out = ping_text("10.201.5.51", n.gap[leg])
        elif "ip -o link show" in input:
            out = "1: lo: <LOOPBACK,UP> mtu 65536 qdisc noqueue state UNKNOWN link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00\n"
            for i, p in enumerate((MZ, P0, P1), 2):
                out += (f"{i}: {p}: <BROADCAST,MULTICAST,UP> mtu 1500 qdisc mq state UP mode DEFAULT "
                        f"group default qlen 1000\\    link/ether {n.macs[p]} brd ff:ff:ff:ff:ff:ff\n")
            out += f"===ADDR\n2: {MZ}    inet {n.ip}/16 brd 10.36.255.255 scope global dynamic {MZ}\n"
        elif "networkctl reload" in input:
            n.yaml = input
            if n.netplan_other:
                out = f"NETPLANDEF {n.netplan_other}\n"
            else:
                links = re.search(r"networkctl reconfigure (\S+) (\S+) (\S+)", input).groups()
                ours = dict(zip(links, ("60-bond0.network", "60-bond0-slave-a.network",
                                        "60-bond0-slave-b.network")))
                out = "APPLIED\n" + "".join(
                    f"NETFILE {l} {n.netfile.get(l, '/etc/systemd/network/' + f)}\n"
                    for l, f in ours.items())
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")


class Pinger:
    def __init__(self, text):
        self.text, self.started = text, []

    def __call__(self, ip):
        self.started.append(ip)
        return types.SimpleNamespace(stop=lambda: self.text)


def clock():
    t = [0.0]

    def c():
        t[0] += 0.5
        return t[0]
    return c


# ------------------------------------------------------------------ bond
def test_sides_follow_lldp_not_the_label_r05_cross_cabled():
    links = [{"link": "pcie0", "seen": "B Ethernet16", "ifname": "eth0"},
             {"link": "pcie1", "seen": "A Ethernet16", "ifname": "eth1"}]
    s, err = bond.sides(links, {"eth0": "B8:59:9F:00:05:00", "eth1": "b8599f000501"})
    assert err is None
    assert s["A"] == {"mac": "b8:59:9f:00:05:01", "link": "pcie1", "ifname": "eth1"}
    assert s["B"]["mac"] == "b8:59:9f:00:05:00"


def test_sides_errors():
    s, err = bond.sides([{"link": "pcie0", "seen": "A E1", "ifname": "eth0"}], {"eth0": mac(1, 0)})
    assert "no port seen on wedge B" in err
    s, err = bond.sides([{"link": "pcie0", "seen": "A E1", "ifname": "eth0"},
                         {"link": "pcie1", "seen": "A E2", "ifname": "eth1"}],
                        {"eth0": mac(1, 0), "eth1": mac(1, 1)})
    assert "two ports seen on wedge A" in err


def test_unit_files():
    u = bond.unit_files({"A": P1, "B": P0}, {"A": mac(5, 1), "B": mac(5, 0)}, "10.201.5.101/16")
    assert sorted(u) == ["60-bond0-slave-a.network", "60-bond0-slave-b.network",
                         "60-bond0.netdev", "60-bond0.network"]
    assert "Kind=bond" in u["60-bond0.netdev"] and "Mode=active-backup" in u["60-bond0.netdev"]
    assert "MIIMonitorSec=100ms" in u["60-bond0.netdev"]
    assert "Address=10.201.5.101/16" in u["60-bond0.network"]
    a, b = u["60-bond0-slave-a.network"], u["60-bond0-slave-b.network"]
    assert f"PermanentMACAddress={mac(5, 1)}" in a and "PrimarySlave=true" in a
    assert f"PermanentMACAddress={mac(5, 0)}" in b and "PrimarySlave" not in b
    assert all("Bond=bond0" in x for x in (a, b))
    assert not any(MZ in x for x in u.values())


def test_parsers():
    n = Node("10.36.5.101", 101)
    b = bond.parse_bonding(n.proc())
    assert b["mode"] == "active-backup" and b["active"] == P0 and b["miimon"] == 100
    assert b["slaves"][P1] == {"mii": "up", "perm": mac(101, 1)}
    assert bond.slave_by_mac(b, mac(101, 1)) == P1
    assert bond.parse_bonding("cat: no such file") is None
    lt = (f"3: {P0}: <UP> mtu 1500 master bond0 state UP\\    link/ether {mac(1, 0)} brd "
          f"ff:ff:ff:ff:ff:ff permaddr {mac(1, 1)}\n"
          f"9: bond0: <UP> mtu 1500\\    link/ether {mac(1, 0)} brd ff:ff:ff:ff:ff:ff\n")
    assert bond.parse_links(lt) == {P0: mac(1, 1), "bond0": mac(1, 0)}
    p = bond.parse_ping(ping_text("1.2.3.4", 500))
    assert p == {"sent": 120, "received": 115, "lost": 5, "max_gap_ms": 500}
    assert bond.parse_ping(ping_text("1.2.3.4", 0, received=0))["received"] == 0


def _side(n):
    return {"A": {"mac": n.macs[n.a_port], "link": "pcie1" if n.a_port == P1 else "pcie0",
                  "ifname": "eth1"},
            "B": {"mac": n.macs[n.b_port], "link": "pcie0", "ifname": "eth0"}}


def test_configure_pass_cross_cabled_primary():
    n = Node("10.36.5.101", 101, a_port=P1)        # R05: wedge A sees P1
    rack = Rack([n])
    pinger = Pinger(ping_text("10.36.5.101", 0, sent=40))
    r = bond.configure(n.ip, "flax", "p", _side(n), "10.201.0.101/16", runner=rack,
                       pinger=pinger, sleep=lambda s: None, clock=clock())
    assert r.verdict == "pass", r.detail
    # networkd units only: netplan never regenerates the mgmt NIC's file, nothing
    # reconfigures it (R37 lost 4-8 s of mgmt on every netplan-driven reload)
    assert "netplan generate" not in n.yaml and "netplan apply" not in n.yaml
    assert MZ not in n.yaml
    assert f"networkctl reconfigure bond0 {P1} {P0}" in n.yaml
    assert "/etc/systemd/network/60-bond0-slave-a.network" in n.yaml
    assert f"PermanentMACAddress={mac(101, 1)}" in r.artifacts["60-bond0-slave-a.network"]
    assert "PrimarySlave=true" in r.artifacts["60-bond0-slave-a.network"]
    assert pinger.started == ["10.36.5.101"]
    assert r.detail["names"] == {"A": P1, "B": P0}
    assert r.detail["ha_mode"] == "active-backup"
    assert ["active slave", f"{P1} (primary)"] in r.detail["rows"]


def test_configure_retires_an_old_netplan_bond_and_refuses_a_netplan_port():
    n = Node("10.36.5.101", 101)
    rack = Rack([n])
    kw = dict(runner=rack, pinger=Pinger(ping_text(n.ip, 0, sent=40)), sleep=lambda s: None,
              clock=clock())
    r = bond.configure(n.ip, "flax", "p", _side(n), "10.201.0.101/16", **kw)
    assert r.verdict == "pass", r.detail
    # rackpost's own earlier yaml is moved out of netplan with its generated units
    assert "mv -f /etc/netplan/60-bond0.yaml /var/lib/flax/60-bond0.yaml.retired" in n.yaml
    assert f"/run/systemd/network/10-netplan-{P0}.network" in n.yaml
    assert "grep -vxF /etc/netplan/60-bond0.yaml" in n.yaml
    # another netplan file naming a port: its unit would win, nothing is written
    n.netplan_other = "/etc/netplan/50-cloud-init.yaml"
    r = bond.configure(n.ip, "flax", "p", _side(n), "10.201.0.101/16", **kw)
    assert r.verdict == "fail" and "50-cloud-init.yaml" in r.detail["error"]
    # a slave still configured by a file that is not ours
    n.netplan_other, n.netfile = None, {P1: f"/run/systemd/network/10-netplan-{P1}.network"}
    r = bond.configure(n.ip, "flax", "p", _side(n), "10.201.0.101/16", **kw)
    assert r.verdict == "fail" and f"10-netplan-{P1}.network" in r.detail["error"]


def test_bring_up_by_mac_never_the_mgmt_nic():
    n = Node("10.36.5.101", 101)
    rack = Rack([n])
    names, err = bond.bring_up(n.ip, "flax", "p", [n.macs[P1], n.macs[P0]], runner=rack)
    assert err is None and names == {n.macs[P1]: P1, n.macs[P0]: P0}
    assert sorted(n.raised) == [P0, P1]
    names, err = bond.bring_up(n.ip, "flax", "p", [n.macs[P0], n.macs[MZ]], runner=rack)
    assert names is None and "mgmt NIC" in err
    assert sorted(n.raised) == [P0, P1]                     # nothing more was raised


def test_configure_mgmt_gap_fails():
    n = Node("10.36.5.101", 101)
    r = bond.configure(n.ip, "flax", "p", _side(n), "10.201.0.101/16", runner=Rack([n]),
                       pinger=Pinger(ping_text(n.ip, 12500, sent=240)), sleep=lambda s: None,
                       clock=clock())
    assert r.verdict == "fail" and "12500 ms" in r.detail["error"]


def test_configure_refuses_the_mgmt_nic():
    n = Node("10.36.5.101", 101)
    rack = Rack([n])
    side = _side(n)
    side["B"]["mac"] = n.macs[MZ]
    r = bond.configure(n.ip, "flax", "p", side, "10.201.0.101/16", runner=rack,
                       pinger=Pinger(""), sleep=lambda s: None, clock=clock())
    assert r.verdict == "fail" and "mgmt NIC" in r.detail["error"]
    assert not any("networkctl" in s for _, s in rack.log)


def test_configure_unreachable_is_unmeasured():
    n = Node("10.36.5.101", 101)
    rack = Rack([n])
    rack.dead.add(n.ip)
    r = bond.configure(n.ip, "flax", "p", _side(n), "10.201.0.101/16", runner=rack,
                       pinger=Pinger(""), sleep=lambda s: None, clock=clock())
    assert r.verdict == "unmeasured"


def test_configure_bond_not_at_primary_fails():
    n = Node("10.36.5.101", 101)
    n.active = P1
    n.no_failover = True
    r = bond.configure(n.ip, "flax", "p", _side(n), "10.201.0.101/16", runner=Rack([n]),
                       pinger=Pinger(ping_text(n.ip, 0, sent=40)), sleep=lambda s: None,
                       clock=clock(), settle_s=3)
    assert r.verdict == "fail" and "not the primary" in r.detail["error"]


# ------------------------------------------------------------------ ha test
RACK = [("C37", "10.36.37.51", 51, P0), ("R37", "10.36.37.101", 101, P0),
        ("L05", "10.36.5.1", 1, P0), ("R05", "10.36.5.101", 102, P1)]


def build(**over):
    ns = {pos: Node(ip, o, a) for pos, ip, o, a in RACK}
    nodes = [{"pos": pos, "host_ip": ip, "bond_ip": f"10.201.0.{o}",
              "macs": {"A": ns[pos].macs[a], "B": ns[pos].macs[ns[pos].b_port]}}
             for pos, ip, o, a in RACK]
    return ns, nodes, Rack(ns.values())


class Mesh:
    def __init__(self, fail=None, exc=None, no_result=None):
        self.calls, self.fail, self.exc, self.no_result = [], fail, exc, no_result

    def __call__(self, server, clients, **kw):
        self.calls.append((server, clients))
        if self.exc:
            raise self.exc
        out = {c["pos"]: RungResult("pass", {"bond": {"tx_bps": 9.4e10, "rx_bps": 9.3e10,
                                                      "speed_bps": 1e11, "ok": True}})
               for c in clients}
        if self.fail:
            out[self.fail] = RungResult("fail", {"error": "bond: tx 10 Mbit/s < 80000"})
        out[server["pos"]] = RungResult("pass", {"served": [c["pos"] for c in clients]})
        if self.no_result in out:       # iperf.mesh: that client fails, and so does the server
            out[self.no_result] = RungResult("fail", {"error": "iperf3: no result"})
            out[server["pos"]] = RungResult("fail", {"served": [c["pos"] for c in clients],
                                                     "error": "a client got no iperf result"})
        return out


def go(nodes, rack, mesh):
    return hatest.run(nodes, "L05", user="flax", pw="p", mesh=mesh, runner=rack,
                      sleep=lambda s: None, clock=clock(), settle_s=3, restore_s=3)


def all_up(ns):
    return all(all(n.up.values()) for n in ns.values())


def test_whole_wedge_failover_pass():
    ns, nodes, rack = build()
    mesh = Mesh()
    out = go(nodes, rack, mesh)
    assert {p: r.verdict for p, r in out.items()} == dict.fromkeys(ns, "pass")
    # leg A cut every node's wedge-A port (R05's is P1: cross-cabled), then leg B
    cuts = [(ip, re.search(r"set (\S+) down", s).group(1)) for ip, s in rack.log if " down" in s]
    assert cuts[:4] and {c for _, c in cuts[:4]} == {P0, P1}
    assert ("10.36.5.101", P1) in cuts[:4] and ("10.36.5.101", P0) in cuts[4:]
    assert all_up(ns) and all(n.active == n.a_port for n in ns.values())
    r05 = out["R05"].detail
    assert r05["legs"]["A"]["active_after"] == P0 and r05["legs"]["B"]["active_after"] == P1
    assert r05["legs"]["A"]["gap_ms"] == 300 and r05["legs"]["A"]["tx_bps"] == 9.4e10
    assert ["cut A (enp25s0f1np1): active", f"{P1} → {P0}"] in r05["rows"]
    assert "HA mode delivered: active-backup" in r05["notes"]
    assert not any("MLAG" in n or "single-node" in n for n in r05["notes"])
    assert out["L05"].detail["rows"][1] == ["cut A: failover gap", "n/a (iperf server)"]
    # the mesh ran over bond0 both legs, server = L05's bond IP
    assert len(mesh.calls) == 2
    srv, clients = mesh.calls[0]
    assert srv["ifaces"] == {"bond": ("bond0", "10.201.0.1")}
    assert {c["pos"] for c in clients} == {"C37", "R37", "R05"}
    # pings started before any cut, aimed at the server's bond IP
    first_cut = next(i for i, (_, s) in enumerate(rack.log) if " down" in s)
    pings = [i for i, (_, s) in enumerate(rack.log) if "setsid ping" in s]
    assert len(pings) == 6 and max(pings[:3]) < first_cut
    assert all("10.201.0.1" in s for _, s in rack.log if "setsid ping" in s)


def test_no_failover_fails_and_links_restored():
    ns, nodes, rack = build()
    ns["C37"].no_failover = True
    out = go(nodes, rack, Mesh())
    assert out["C37"].verdict == "fail" and "no failover" in out["C37"].detail["error"]
    assert out["R37"].verdict == "pass"
    assert all_up(ns)


def test_gap_over_limit_fails():
    ns, nodes, rack = build()
    ns["R37"].gap["A"] = 4500
    out = go(nodes, rack, Mesh())
    assert out["R37"].verdict == "fail" and "4500 ms" in out["R37"].detail["error"]


def test_mesh_exception_still_restores():
    ns, nodes, rack = build()
    out = go(nodes, rack, Mesh(exc=RuntimeError("boom")))
    assert all(r.verdict == "fail" for r in out.values())
    assert all_up(ns)


def test_mesh_fail_is_a_fail():
    ns, nodes, rack = build()
    out = go(nodes, rack, Mesh(fail="C37"))
    assert out["C37"].verdict == "fail" and "iperf" in out["C37"].detail["error"]


def test_one_clients_missing_iperf_result_does_not_fail_the_server():
    ns, nodes, rack = build()
    mesh = Mesh(no_result="R05")
    out = go(nodes, rack, mesh)
    assert out["R05"].verdict == "fail" and "iperf" in out["R05"].detail["error"]
    # the server is judged on its own failover: it passes and leg B still runs for the rest
    assert out["L05"].verdict == "pass", out["L05"].detail.get("error")
    assert out["L05"].detail["legs"]["A"]["iperf"] == "a client got no iperf result"
    assert {p: out[p].verdict for p in ("C37", "R37")} == {"C37": "pass", "R37": "pass"}
    assert len(mesh.calls) == 2 and {c["pos"] for c in mesh.calls[1][1]} == {"C37", "R37"}


def test_server_that_cannot_start_iperf_fails():
    ns, nodes, rack = build()

    def mesh(server, clients, **kw):
        msg = f"server {server['pos']} not ready: iperf3 -s did not start"
        return {**{c["pos"]: RungResult("unmeasured", {"error": msg}) for c in clients},
                server["pos"]: RungResult("fail", {"error": msg})}
    out = go(nodes, rack, mesh)
    assert out["L05"].verdict == "fail" and "not ready" in out["L05"].detail["error"]
    assert all(out[p].verdict == "unmeasured" for p in ("C37", "R37", "R05"))


def test_cut_arms_an_on_node_dead_man_and_restore_disarms_it():
    ns, nodes, rack = build()
    go(nodes, rack, Mesh())
    cuts = [s for _, s in rack.log if " down" in s]
    restores = [s for _, s in rack.log if re.search(r"^ip link set \S+ up", s, re.M)]
    assert cuts and all(
        s.index("setsid sh -c") < s.index(" down") and
        re.search(rf"sleep {hatest.DEADMAN_S}; ip link set (\S+) up.*ip link set \1 down", s, re.S)
        for s in cuts)
    assert restores and all(s.startswith("kill ") and ".deadman" in s for s in restores)


def test_unanswering_node_unmeasured_others_judged():
    ns, nodes, rack = build()
    rack.dead.add("10.36.37.101")
    out = go(nodes, rack, Mesh())
    assert out["R37"].verdict == "unmeasured"
    assert out["C37"].verdict == "pass"
    assert not any(ip == "10.36.37.101" and " down" in s for ip, s in rack.log)


def test_server_unanswering_nothing_cut():
    ns, nodes, rack = build()
    rack.dead.add("10.36.5.1")
    out = go(nodes, rack, Mesh())
    assert all(r.verdict == "unmeasured" for r in out.values())
    assert not any(" down" in s for _, s in rack.log)


def test_restore_failure_is_loud():
    ns, nodes, rack = build()
    ns["C37"].restore_fails = True
    out = go(nodes, rack, Mesh())
    assert out["C37"].verdict == "fail" and "left DOWN" in out["C37"].detail["error"]


def test_bond_missing_fails_precheck():
    ns, nodes, rack = build()
    ns["C37"].bonded = False
    out = go(nodes, rack, Mesh())
    assert out["C37"].verdict == "fail" and "no bond0" in out["C37"].detail["error"]
    assert not any(ip == "10.36.37.51" and " down" in s for ip, s in rack.log)
