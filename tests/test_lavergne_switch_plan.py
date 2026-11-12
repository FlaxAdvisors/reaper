"""Lavergne target switch configs (spec 2026-10-27-lavergne-eindhoven-rebuild §5, §6).

Final files are the declarative target pushed by apply_config.yml. (The one-shot
mgmt-step / cleanup pushes of the 10-27 rebuild are done and were removed 2026-11-12.)
"""
import os
import re

_REAPER = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
CFG = os.path.join(_REAPER, "files", "configs", "lavergne")
OLD = {6, 8, 10, 12, 14, 102, 200}
TRIAGE_PORTS = {"Ethernet%d/%d" % (c, b) for c in (29, 30, 31) for b in (1, 2, 3, 4)}
# mezz-flash stations, OU16 of the triage rack (operator 2026-10-27)
STATION_PORTS = {"Ethernet25/%d" % b for b in (1, 2, 3, 4)}
OLD_TRIAGE = {"Ethernet%d/%d" % (c, b) for c in (17, 18, 19, 20, 23) for b in (1, 2, 3, 4)}


def read(*p):
    with open(os.path.join(CFG, *p)) as f:
        return f.read()


def eos_ifaces(text):
    out, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"^interface (\S+)", line)
        if m:
            cur = out.setdefault(m.group(1), [])
        elif line.startswith("   ") and cur is not None:
            cur.append(line.strip())
        elif not line.startswith(" "):
            cur = None
    return out


def vlan_ids(text):
    return {int(v) for v in re.findall(r"^vlan (\d+)$", text, re.M)}


def stanza(text, iface):
    m = re.search(r"^iface %s\b.*\n((?:    .*\n?)*)" % re.escape(iface), text, re.M)
    assert m, iface
    return [l.strip() for l in m.group(1).splitlines() if l.strip()]


RR = read("rabbit-rhythm", "config", "running-config.txt")
RR_IF = eos_ifaces(RR)
RB = read("rabbit-blues", "config", "running-config.txt")
RB_IF = eos_ifaces(RB)
T = read("turtle-rhythm", "config", "interfaces.txt")


def test_rabbit_rhythm_final_vlans():
    assert vlan_ids(RR) == {2, 4} | set(range(16, 34))


def test_bang_trunks_native_mgmt():
    for p in ("Ethernet1/1", "Ethernet1/2"):
        assert "switchport trunk native vlan 4" in RR_IF[p], p
        assert "switchport trunk allowed vlan 4,16-25,28-33" in RR_IF[p], p


def test_uplinks():
    assert "switchport access vlan 31" in RR_IF["Ethernet2/1"]
    # rabbit-blues trunk is cabled on Et3/1 (Et4/1 has no transceiver; reaper 5cdd252)
    assert "switchport trunk allowed vlan 4,24-30" in RR_IF["Ethernet3/1"]
    assert RR_IF["Ethernet4/1"] == ["shutdown"]
    # vid 31 (howlin / Wedge 400 mgmt) rides the turtle trunk (reaper 72d11ff)
    assert "switchport trunk allowed vlan 2,4,26-27,31" in RR_IF["Ethernet32/1"]
    assert "switchport access vlan 33" in RR_IF["Ethernet28/1"]   # rabbit-muddy (LLDP 10-27)


def triage_vids(text):
    return {int(v) for v, n in re.findall(r"^vlan (\d+)\n   name (\S+)$", text, re.M)
            if n.startswith("triage-")}


def test_triage_ports_on_a_triage_vlan_and_up():
    # Which triage VLAN (one per blade family, 16-23) is reconcile's per blade, and a
    # refetch commits whatever it was at that moment; the file only fixes that the
    # port is up, 25G, and access on one of the triage VLANs.
    tv = triage_vids(RR)
    assert tv == set(range(16, 24))
    for p in TRIAGE_PORTS | STATION_PORTS:
        s = RR_IF[p]
        assert "shutdown" not in s, p
        assert "speed forced 25gfull" in s, p
        vids = [int(l.split()[-1]) for l in s if l.startswith("switchport access vlan ")]
        assert len(vids) == 1 and vids[0] in tv, (p, s)


def inactive_lanes(ifaces):
    """/2-/4 of a port whose /1 is forced to 100G: EOS rejects config there."""
    out = set()
    for n, s in ifaces.items():
        m = re.match(r"^Ethernet(\d+)/1$", n)
        if m and any(l.startswith("speed forced 100g") for l in s):
            out |= {"Ethernet%s/%d" % (m.group(1), i) for i in (2, 3, 4)}
    return out


def test_everything_else_shut():
    kept = {"Ethernet1/1", "Ethernet1/2", "Ethernet2/1", "Ethernet3/1", "Ethernet28/1",
            "Ethernet32/1"} | TRIAGE_PORTS | STATION_PORTS
    dead = inactive_lanes(RR_IF)
    assert dead == {"Ethernet13/2", "Ethernet13/3", "Ethernet13/4"}
    for name, s in RR_IF.items():
        if name.startswith("Ethernet") and name not in kept:
            assert ("shutdown" in s) != (name in dead), name


def test_management_ip():
    assert "ip address 192.168.88.31/24" in RR_IF["Management1"]
    assert "ip route 0.0.0.0/0 192.168.88.10" in RR
    assert "ip name-server vrf default 192.168.88.10" in RR


def test_rabbit_blues_post_rack():
    assert vlan_ids(RB) == {4} | set(range(24, 31))
    assert "switchport trunk allowed vlan 4,24-30" in RB_IF["Ethernet32/1"]
    assert "ip address 192.168.88.32/24" in RB_IF["Management1"]
    post = [n for n, s in RB_IF.items() if "switchport access vlan 25" in s]
    assert len(post) == 48


def test_turtle_mgmt_and_corp():
    assert stanza(T, "swp1") == stanza(T, "swp2") == ["bridge-pvid 4", "bridge-vids 2 4 26 27"]
    assert stanza(T, "swp3") == ["bridge-access 4"]
    eth0 = stanza(T, "eth0")
    assert "address 192.168.88.33/24" in eth0 and "gateway 192.168.88.10" in eth0
    assert stanza(T, "swp49") == ["bridge-vids 2 4 26 27 31"]  # vid 31 to rabbit-rhythm (72d11ff)
    assert stanza(T, "swp21") == ["bridge-access 31"]  # Wedge 400 mgmt
    vids = set(map(int, stanza(T, "bridge")[1].split()[1:])) if stanza(T, "bridge")[1].startswith("bridge-vids") else set()
    assert not vids & OLD


