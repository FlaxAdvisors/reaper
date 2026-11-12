"""flax_rack.switches: rackpost's switch reads through the shared flax_switch_sense
drivers, port naming per NOS, and the site turtle's MAC read.

The *-lldpctl.json / *-show-* fixtures are one read-only capture of RRORD-101's Dell
(172.26.0.55) and wedge A (172.26.0.53), 2026-10-31; the `show lldp table` captured in
the same session is the oracle the old private reader (flax_rungs.sonic) parsed."""
import pathlib
import re
from unittest.mock import patch

import pytest

from flax_rack import switches
from flax_rack.geometry import Switch, load_geometry
from flax_rack.linkmon import device_names, evaluate
from flax_rungs.result import UNMEASURED
from flax_switch_sense.driver_ios import IosError
from flax_switch_sense.driver_sonic import _BATCH_MARK, SonicError

F = pathlib.Path(__file__).parent / "fixtures/rackpost"
CRED = {"sonicuser": "u", "sonicpass": "p", "eosuser": "u", "eospass": "p",
        "cumuser": "u", "cumpass": "p", "cisco_user": "u", "cisco_pass": "p"}
LIVE = {"dell": ("dell-show-interfaces-status.txt", "dell-show-vlan-brief.txt",
                 "dell-show-mac.txt", "dell-lldpctl.json", "dell-show-lldp-table.txt"),
        "wedge": ("wedge-show-interfaces-status.txt", "wedge-show-vlan-brief.txt",
                  "wedge-show-mac.txt", "wedge-lldpctl.json", "wedge-show-lldp-table-live.txt")}


def sw(nos="sonic", model="dell-n3248te", sid="C"):
    return Switch(sid, "mgmt", 41, model, nos)


def sonic_runner(kind, calls=None):
    """A SonicDriver runner over one live capture (the batched script, or one command)."""
    ifs, vlan, mac, lldp, _ = (F / n for n in LIVE[kind])
    by_key = {"ifstatus": ifs, "vlan": vlan, "mac": mac, "lldp": lldp}

    def run(cmd):
        if calls is not None:
            calls.append(cmd)
        if _BATCH_MARK in cmd:
            return "".join(f"{_BATCH_MARK}{k}\n{p.read_text()}" for k, p in by_key.items()) \
                + f"{_BATCH_MARK}end\n"
        for needle, p in (("interfaces status", ifs), ("vlan brief", vlan), ("show mac", mac),
                          ("lldpctl", lldp)):
            if needle in cmd:
                return p.read_text()
        raise AssertionError(cmd)
    return run


def lldp_table(text):
    """Oracle: the `show lldp table` columns (cut at the dash ruler), mgmt row included."""
    lines = text.splitlines()
    i = next(k for k, l in enumerate(lines) if l.startswith("LocalPort"))
    starts = [m.start() for m in re.finditer(r"-+", lines[i + 1])]
    out = []
    for l in lines[i + 2:]:
        if not l.strip() or set(l.strip()) <= {"-"} or l.startswith("Total"):
            continue
        c = [l[a:b].strip() for a, b in zip(starts, starts[1:] + [None])]
        out.append((c[0], c[1], c[2], c[4]))
    return sorted(out)


# ---------------------------------------------------------------- live captures
@pytest.mark.parametrize("kind", ["dell", "wedge"])
def test_read_live_matches_show_lldp_table_and_status(kind):
    calls = []
    r = switches.read_live("172.26.0.55", "sonic", CRED, runner=sonic_runner(kind, calls))
    assert r.verdict == "pass" and len(calls) == 1          # one ssh session
    got = sorted((e.local_iface, e.remote_device, e.remote_port, e.remote_descr)
                 for e in r.detail["lldp"])
    assert got == lldp_table((F / LIVE[kind][4]).read_text())
    st = r.detail["ifaces"]
    if kind == "dell":
        assert st["Ethernet6"] == switches.IfStatus("up", "up", "1G")
        assert st["Ethernet1"].oper == "down" and len(r.detail["fdb"]) == 14
        assert ("eth0", "turtle-gouda.ehv.lab", "Gi1/0/19") in [g[:3] for g in got]
    else:
        assert st["Ethernet31"].oper == "up" and st["Ethernet0"].speed == "100G"
    assert "show-live.txt" in r.artifacts


def test_live_dell_snapshot_judges_rr101_links():
    """evaluate() on the shared driver's snapshot: the RRORD-101 mgmt links as live."""
    g = load_geometry(F / "rr101-geometry.json")
    c = next(s for s in g.switches if s.id == "C")
    c.site_port = {"switch": "turtle-gouda", "port": "Gi1/0/19"}   # as RRORD-101's
    r = switches.read_live("172.26.0.55", "sonic", CRED, runner=sonic_runner("dell"))
    res = evaluate(g, {"C": {**r.detail, "at": 100.0}}, {}, device_names(g), 100.0)
    assert res["L37"]["mezz0"]["state"] == "ok" and "rr101-l37" in res["L37"]["mezz0"]["text"]
    assert res["_sw-C"]["site"]["state"] == "ok"
    assert res["_sw-C"]["uplink1"]["state"] == "up"


def test_dell_interface_status_reads_with_sudo():
    """Discover's switchportlink evidence: plain-user `show interfaces status` dies on
    the Dell (EEPROM); the shared driver runs it under sudo -n with empty stdin."""
    calls = []
    r = switches.read_ifstatus("172.26.0.55", "sonic", CRED, runner=sonic_runner("dell", calls))
    assert r.verdict == "pass" and r.detail["status"]["Ethernet6"]["oper"] == "up"
    assert calls == ["sudo -n show interfaces status </dev/null"]


def test_read_fdb_dell():
    r = switches.read_fdb("172.26.0.55", "sonic", CRED, runner=sonic_runner("dell"))
    assert r.verdict == "pass" and r.detail["count"] == 14
    assert {e["mac"] for e in r.detail["fdb"] if e["iface"] == "Ethernet6"} == \
        {"0c42a1e16b50", "0c42a1e16b52"}


def test_read_lldp_front_panel_only():
    r = switches.read_lldp("172.26.0.55", "sonic", CRED, runner=sonic_runner("dell"))
    ifs = {e["local_iface"] for e in r.detail["lldp"]}
    assert r.verdict == "pass" and "eth0" not in ifs and "Ethernet4" in ifs


def dead(rc):
    def run(cmd):
        raise SonicError("h: ssh exited %d" % rc, rc)
    return run


def test_unreachable_is_unmeasured_and_auth_failure_is_fail():
    for fn in (switches.read_live, switches.read_lldp, switches.read_syseeprom):
        assert fn("h", "sonic", CRED, runner=dead(255)).verdict == UNMEASURED
        assert fn("h", "sonic", CRED, runner=dead(5)).verdict == "fail"
    assert switches.read_fdb("h", "sonic", CRED, runner=dead(255)).verdict == "fail"
    # Discover's host-ssh step reads "rc=" as "ssh did not answer"
    assert switches.read_syseeprom("h", "sonic", CRED, runner=dead(5)).detail["error"] \
        .startswith("rc=5")


def test_empty_status_section_is_fail():
    r = switches.read_live("h", "sonic", CRED, runner=lambda cmd: f"{_BATCH_MARK}ifstatus\n")
    assert r.verdict == "fail" and "listed no ports" in r.detail["headline"]


# ---------------------------------------------------------------- port names
@pytest.mark.parametrize("s,port,lane,iface", [
    (sw(), 7, None, "Ethernet6"),
    (sw(model="wedge100s"), 28, 1, "Ethernet108"),
    (sw(model="wedge100s"), 8, 4, "Ethernet31"),
    (sw("eos", "7050"), 12, 1, "Ethernet12/1"),
    (sw("eos", "7050"), 48, None, "Ethernet48"),
    (sw("cumulus", "as5812"), 49, 2, "swp49s1"),
    (sw("cumulus", "as5812"), 3, None, "swp3"),
])
def test_link_iface_and_front_panel_round_trip(s, port, lane, iface):
    assert switches.link_iface(s, port, lane) == iface
    want = (port, lane or (1 if s.model == "wedge100s" else None))
    assert switches.front_panel(s, iface) == want


def test_front_panel_short_and_foreign_names():
    assert switches.front_panel(sw("eos"), "Et12/3") == (12, 3)
    assert switches.front_panel(sw("eos"), "Port-Channel1") is None
    assert switches.front_panel(sw(), "PortChannel0001") is None
    assert switches.front_panel(sw("cumulus"), "bond0") is None


# ---------------------------------------------------------------- other NOSes
def test_eos_live_read_shape():
    eapi = {"show interfaces status": [{"interfaceStatuses": {
                "Ethernet12/1": {"linkStatus": "connected"},
                "Ethernet13/1": {"linkStatus": "notconnect"}}}],
            "show mac address-table": [{"unicastTable": {"tableEntries": [
                {"entryType": "dynamic", "interface": "Ethernet12/1", "vlanId": 25,
                 "macAddress": "88:e9:a4:00:00:01"}]}}],
            "show lldp neighbors detail": [{"lldpNeighbors": {"Ethernet12/1": {
                "lldpNeighborInfo": [{"chassisId": "88:e9:a4:00:00:00",
                                      "systemName": "rr101-l37",
                                      "neighborInterfaceInfo": {
                                          "interfaceId_v2": "88:e9:a4:00:00:01",
                                          "interfaceDescription": "ens3f0np0"}}]}}}]}
    with patch("flax_switch_sense.driver_eos.EosDriver._post",
               side_effect=lambda cmds, fmt="json": eapi[cmds[0]]):
        r = switches.read_live("e", "eos", CRED)
    assert r.verdict == "pass"
    assert r.detail["ifaces"]["Ethernet12/1"] == switches.IfStatus("up", None, None)
    assert r.detail["ifaces"]["Ethernet13/1"].oper == "down"
    assert r.detail["lldp"] == [switches.LldpEntry("Ethernet12/1", "rr101-l37",
                                                   "88:e9:a4:00:00:01", "ens3f0np0")]
    assert r.detail["fdb"] == [switches.FdbEntry(25, "88e9a4000001", "Ethernet12/1")]
    assert "live.json" in r.artifacts


def test_eos_connect_error_is_unmeasured():
    import requests
    with patch("requests.Session.post", side_effect=requests.ConnectionError("refused")):
        assert switches.read_lldp("e", "eos", CRED).verdict == UNMEASURED
    with patch("requests.Session.post", side_effect=requests.HTTPError("401")):
        assert switches.read_lldp("e", "eos", CRED).verdict == "fail"


# ---------------------------------------------------------------- the site turtle
TURTLE = (F / "turtle-show-mac.txt").read_text()
EMPTY_18 = "\n".join(l for l in TURTLE.splitlines() if "Gi1/0/18" not in l or "#show" in l)
PORTS = ["Gi1/0/17", "Gi1/0/18", "Gi1/0/19"]


class Turtle:
    def __init__(self, outputs, ping_ok=True):
        self.outputs, self.ping_ok, self.calls = list(outputs), ping_ok, []

    def __call__(self, cmd):
        self.calls.append(("ssh", cmd))
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    def ping(self, ip):
        self.calls.append(("ping", ip))
        return self.ping_ok


def turtle(t, ports=PORTS, ping_ips=("172.26.0.60",)):
    return switches.mac_table("192.168.88.33", "ios", CRED, ports, ping_ips=ping_ips,
                              runner=t, ping=t.ping)


def test_turtle_pings_then_reads_dynamic_macs_per_port():
    t = Turtle([TURTLE])
    r = turtle(t)
    assert r.verdict == "pass", r.detail
    assert r.detail["ports"]["Gi1/0/18"] == ["0090fb64b65d", "b86a9719c8fd"]
    assert r.detail["ports"]["Gi1/0/17"] == ["0090fb62bb51", "3c2c99e1e1b8"]
    assert [c[0] for c in t.calls] == ["ping", "ssh"]
    assert t.calls[1][1] == "show mac address-table"          # IosDriver's table read
    assert "show-mac.txt" in r.artifacts and r.detail["headline"]


def test_turtle_aged_out_port_is_pinged_and_reread_once():
    t = Turtle([EMPTY_18, TURTLE])
    r = turtle(t)
    assert r.verdict == "pass" and r.detail["reread"] is True
    assert [c[0] for c in t.calls] == ["ping", "ssh", "ping", "ssh"]
    assert "show-mac-reread.txt" in r.artifacts
    t = Turtle([EMPTY_18, EMPTY_18])
    r = turtle(t)
    assert r.verdict == "fail" and "Gi1/0/18" in r.detail["error"]


def test_turtle_long_port_names_and_failures():
    r = turtle(Turtle([TURTLE]), ports=["GigabitEthernet1/0/19"], ping_ips=())
    assert r.detail["ports"] == {"GigabitEthernet1/0/19": ["185a585ef920"]}
    t = Turtle([TURTLE])
    assert turtle(t, ports=["Gi1/0/18; reload"]).verdict == "fail" and t.calls == []
    r = turtle(Turtle([IosError("t: exited 255", 255)]))
    assert r.verdict == "fail" and "rc=255" in r.detail["error"]


# ---------------------------------------------------------------- transport (RULE ZERO)
@pytest.mark.parametrize("nos", ["sonic", "cumulus", "ios"])
def test_ssh_drivers_keep_the_password_out_of_argv(monkeypatch, nos):
    import subprocess
    seen = {}

    def fake(argv, **kw):
        seen.update(argv=argv, env=kw.get("env") or {})
        return ""
    monkeypatch.setattr(subprocess, "check_output", fake)
    cred = {**CRED, **{k: "s3cret" for k in ("sonicpass", "cumpass", "cisco_pass")}}
    switches.read_lldp("h", nos, cred)
    assert seen["argv"][:2] == ["sshpass", "-e"] and "s3cret" not in seen["argv"]
    assert seen["env"]["SSHPASS"] == "s3cret"


def test_geometry_nos_drives_the_driver():
    from flax_switch_sense.driver_cumulus import CumulusDriver
    from flax_switch_sense.driver_sonic import SonicDriver
    assert isinstance(switches.driver("h", "sonic", CRED), SonicDriver)
    d = switches.driver("h", "cumulus", CRED)
    assert isinstance(d, CumulusDriver) and d.user == "u" and d.password == "p"
