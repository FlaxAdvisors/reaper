"""Unit tests for flax_switch_sense.driver_sonic.SonicDriver (community SONiC).

Pure/local: an injected runner returns canned text keyed by command substring
(shapes from the live Dell N3248TE, MACs faked), and we assert each of the four
public methods returns the EosDriver-matching shape so slice.py consumes SONiC
facts identically to Arista / Cumulus / IOS.
"""
import json
import pathlib
import subprocess

from flax_switch_sense.driver_sonic import SonicDriver, SonicError
from flax_switch_sense.slice import slice_by_port

F = pathlib.Path(__file__).parent / "fixtures/rackpost"
IFSTATUS = (F / "dell-show-interfaces-status.txt").read_text()
SHOWMAC = (F / "dell-show-mac.txt").read_text()

VLAN = """\
+-----------+--------------+------------+----------------+-------------+-----------------------+
|   VLAN ID | IP Address   | Ports      | Port Tagging   | Proxy ARP   | DHCP Helper Address   |
+===========+==============+============+================+=============+=======================+
|      1000 |              | Ethernet0  | tagged         | disabled    |                       |
|           |              | Ethernet4  | untagged       |             |                       |
|           |              | Ethernet6  | untagged       |             |                       |
+-----------+--------------+------------+----------------+-------------+-----------------------+
|      1001 |              | Ethernet0  | tagged         | disabled    |                       |
|           |              | Ethernet7  | untagged       |             |                       |
|           |              | PortChannel1 | tagged       |             |                       |
+-----------+--------------+------------+----------------+-------------+-----------------------+
"""

LLDP = json.dumps({"lldp": {"interface": [
    {"eth0": {"via": "LLDP",
              "chassis": {"turtle-gouda.ehv.lab": {
                  "id": {"type": "mac", "value": "02:00:00:00:00:01"},
                  "mgmt-ip": "172.26.0.5"}},
              "port": {"id": {"type": "ifname", "value": "Gi1/0/19"},
                       "descr": "OOB mgmt"}}},
    {"Ethernet4": {"via": "LLDP",
                   "chassis": {"tiogapass": {
                       "id": {"type": "mac", "value": "02:00:00:00:00:A8"},
                       "mgmt-ip": ["172.32.5.11", "fd00:32::1"]}},
                   "port": {"id": {"type": "mac", "value": "02:00:00:00:00:a8"},
                            "descr": "eth0"}}},
]}}, indent=2)

ALL = {"show interfaces status": IFSTATUS, "show vlan brief": VLAN,
       "show mac": SHOWMAC, "lldpctl": LLDP}


def make_runner(responses: dict):
    def runner(cmd: str) -> str:
        for needle, text in responses.items():
            if needle in cmd:
                return text
        return ""
    return runner


def drv(responses=ALL):
    return SonicDriver("172.26.0.55", "admin", "pw", runner=make_runner(responses))


def test_interfaces_status_oper_column():
    link = drv().interfaces_status()
    assert len(link) == 54
    assert link["Ethernet6"] == "link" and link["Ethernet1"] == "nolink"
    assert link["Ethernet56"] == "nolink"


def test_vlans_access_vs_trunk():
    v = drv().vlans()
    assert v["Ethernet4"] == {"access_vid": 1000, "trunk": False}
    assert v["Ethernet7"] == {"access_vid": 1001, "trunk": False}
    assert v["Ethernet0"] == {"access_vid": None, "trunk": True}
    assert "PortChannel1" not in v


def test_mac_table_dynamic_ethernet_only():
    text = SHOWMAC.replace("Ethernet13  Dynamic", "Ethernet13  Static")
    macs = drv({"show mac": text}).mac_address_table()
    assert len(macs) == 13
    assert (1000, "0c:42:a1:e1:6b:50", "Ethernet6") in macs
    assert not any(p == "Ethernet13" for _, _, p in macs)


def test_lldp_neighbors_detail_shape():
    l = drv().lldp_neighbors_detail()
    assert l["Ethernet4"] == [{"mac": "02:00:00:00:00:a8", "sysname": "tiogapass",
                               "port_description": "eth0", "port_id": "02:00:00:00:00:a8",
                               "mgmt_addrs": ["172.32.5.11", "fd00:32::1"]}]
    assert l["eth0"][0]["mgmt_addrs"] == ["172.26.0.5"]
    assert l["eth0"][0]["port_id"] == "Gi1/0/19"


def test_refresh_one_ssh_feeds_every_read():
    calls = []
    sections = {"ifstatus": IFSTATUS, "vlan": VLAN, "mac": SHOWMAC, "lldp": LLDP}

    def runner(cmd):
        calls.append(cmd)
        return "".join(f"@@FLAX-SENSE@@{k}\n{v}\n" for k, v in sections.items()) + \
            "@@FLAX-SENSE@@end\n"
    d = SonicDriver("h", "u", "p", runner=runner)
    d.refresh()
    per_port = slice_by_port(d.interfaces_status(), d.vlans(),
                             d.mac_address_table(), d.lldp_neighbors_detail())
    assert len(calls) == 1
    script = calls[0]
    assert "sudo -n show interfaces status </dev/null" in script
    assert all("</dev/null" in ln for ln in script.splitlines() if not ln.startswith("echo"))
    assert per_port["Ethernet4"]["link"] == "link"
    assert per_port["Ethernet4"]["access_vid"] == 1000
    assert len(per_port["Ethernet4"]["lldp_neighbors"]) == 1


def test_empty_output_is_empty():
    d = drv({})
    assert d.interfaces_status() == {} and d.vlans() == {}
    assert d.mac_address_table() == [] and d.lldp_neighbors_detail() == {}


def test_default_runner_password_in_env_stdin_empty(monkeypatch):
    seen = {}

    def fake(argv, **kw):
        seen.update(argv=argv, **kw)
        return f"@@FLAX-SENSE@@ifstatus\n{IFSTATUS}\n@@FLAX-SENSE@@end\n"
    monkeypatch.setattr(subprocess, "check_output", fake)
    SonicDriver("h", "admin", "s3cret").refresh()
    assert seen["argv"][:2] == ["sshpass", "-e"] and "s3cret" not in seen["argv"]
    assert seen["env"]["SSHPASS"] == "s3cret" and seen["input"] == ""


def test_unreachable_raises_without_password(monkeypatch):
    def fake(argv, **kw):
        raise subprocess.CalledProcessError(255, argv)
    monkeypatch.setattr(subprocess, "check_output", fake)
    try:
        SonicDriver("h", "admin", "s3cret").refresh()
    except SonicError as e:
        assert "255" in str(e) and "s3cret" not in str(e)
    else:
        raise AssertionError("expected SonicError")


def test_refresh_with_no_interface_rows_is_an_error_not_zero_ports():
    """sudo -n refused (stderr only): every section empty but ssh exited 0. That
    is a reachable, unreadable switch -- never a reachable one with no ports."""
    d = SonicDriver("h", "u", "p", runner=lambda cmd: (
        "@@FLAX-SENSE@@ifstatus\n@@FLAX-SENSE@@vlan\n@@FLAX-SENSE@@mac\n"
        "@@FLAX-SENSE@@lldp\n@@FLAX-SENSE@@end\n"))
    try:
        d.refresh()
    except SonicError as e:
        assert e.rc is None and "no ports" in str(e)
    else:
        raise AssertionError("expected SonicError")
