"""Unit tests for flax_switch_sense.driver_ios.IosDriver (Cisco IOS classic 15.x).

Pure/local: an injected runner returns canned `show ...` text keyed by command
substring, and we assert each of the four public methods returns the
EosDriver-matching shape so slice.py consumes Cisco facts identically to Arista
+ Cumulus. No real switch, no network. Parsing is lifted from the legacy
scripts/switchportrecond.py IosDriver + reaper_leased CiscoIOSSSH; LLDP-detail
parsing is new (the legacy IOS driver never implemented it).
"""
from flax_switch_sense.driver_ios import IosDriver


def make_runner(responses: dict):
    """runner(cmd) -> canned text by substring match (first hit wins)."""
    def runner(cmd: str) -> str:
        for needle, text in responses.items():
            if needle in cmd:
                return text
        return ""
    return runner


# --- `show interfaces status` (drives interfaces_status AND vlans) ---
# Name column is variable-width/multi-word; Status+Vlan are adjacent single
# tokens. Trunk ports show "trunk" in the Vlan column; routed show "routed".
IFSTATUS = """\
Port      Name               Status       Vlan       Duplex  Speed Type
Gi1/0/1   leopard-bmc        connected    16         a-full a-1000 10/100/1000BaseTX
Gi1/0/2   tioga bmc lab      notconnect   17           auto   auto 10/100/1000BaseTX
Gi1/0/3   broken             err-disabled 18           auto   auto 10/100/1000BaseTX
Gi1/0/4                      disabled     19           auto   auto 10/100/1000BaseTX
Gi1/0/48  uplink to rabbit   connected    trunk      a-full a-1000 10/100/1000BaseTX
Te1/1/1   stack uplink       connected    routed     full    10G 10GBase-SR
"""

MACTABLE = """\
          Mac Address Table
-------------------------------------------

Vlan    Mac Address       Type        Ports
----    -----------       --------    -----
 All    0100.0ccc.cccc    STATIC      CPU
  16    9803.9ba6.fc24    DYNAMIC     Gi1/0/1
  17    1c34.da7f.9d6c    DYNAMIC     Gi1/0/2
  16    0000.5e00.0101    STATIC      Gi1/0/1
Total Mac Addresses for this criterion: 4
"""

LLDP = """\
------------------------------------------------
Local Intf: Gi1/0/1
Chassis id: 9803.9ba6.fc24
Port id: eth0
Port Description: eth0
System Name: leopard-bmc

System Description:
OpenBMC

Time remaining: 95 seconds
System Capabilities: B
Enabled Capabilities: B
Management Addresses:
    IP: 172.16.7.101
Auto Negotiation - not supported
------------------------------------------------
Local Intf: Gi1/0/2
Chassis id: 1c34.da7f.9d6c
Port id: eth0
Port Description: NIC
System Name: tioga-bmc
Management Addresses:
    IP: 172.17.6.101
------------------------------------------------

Total entries displayed: 2
"""

ALL = {"show interfaces status": IFSTATUS,
       "show mac address-table": MACTABLE,
       "show lldp neighbors detail": LLDP}


def _drv():
    return IosDriver("turtle-gouda", "admin", "pw", runner=make_runner(ALL))


# --- interfaces_status: normalize IOS keywords -> link/nolink/unknown ---

def test_interfaces_status_normalizes_keywords():
    out = _drv().interfaces_status()
    assert out["Gi1/0/1"] == "link"        # connected
    assert out["Gi1/0/2"] == "nolink"      # notconnect
    assert out["Gi1/0/3"] == "nolink"      # err-disabled
    assert out["Gi1/0/4"] == "nolink"      # disabled
    assert out["Gi1/0/48"] == "link"
    # header + separator lines are not ports
    assert "Port" not in out


def test_interfaces_status_only_real_eth_ports():
    out = _drv().interfaces_status()
    assert set(out) == {"Gi1/0/1", "Gi1/0/2", "Gi1/0/3", "Gi1/0/4", "Gi1/0/48", "Te1/1/1"}


# --- vlans: access vid (int) or trunk, from the same `show interfaces status` ---

def test_vlans_access_and_trunk():
    out = _drv().vlans()
    assert out["Gi1/0/1"] == {"access_vid": 16, "trunk": False}
    assert out["Gi1/0/2"] == {"access_vid": 17, "trunk": False}
    assert out["Gi1/0/48"] == {"access_vid": None, "trunk": True}   # trunk
    # 'routed' is neither an access vid nor a trunk member
    assert out["Te1/1/1"] == {"access_vid": None, "trunk": False}


# --- mac_address_table: dynamic only, dotted -> colon, vlan int ---

def test_mac_table_dynamic_only_colon_mac():
    out = _drv().mac_address_table()
    assert (16, "98:03:9b:a6:fc:24", "Gi1/0/1") in out
    assert (17, "1c:34:da:7f:9d:6c", "Gi1/0/2") in out
    # STATIC entries (CPU + the 0000.5e00 vrrp) are excluded
    assert all(m != "01:00:0c:cc:cc:cc" for _, m, _ in out)
    assert all(m != "00:00:5e:00:01:01" for _, m, _ in out)
    assert len(out) == 2


# --- lldp_neighbors_detail: block parse -> {port: [{mac, sysname, ...}]} ---

def test_lldp_neighbors_detail_shape():
    out = _drv().lldp_neighbors_detail()
    g1 = out["Gi1/0/1"]
    assert len(g1) == 1
    n = g1[0]
    assert n["mac"] == "98:03:9b:a6:fc:24"          # dotted -> colon
    assert n["sysname"] == "leopard-bmc"
    assert n["port_description"] == "eth0" and n["port_id"] == "eth0"
    assert n["mgmt_addrs"] == ["172.16.7.101"]
    g2 = out["Gi1/0/2"][0]
    assert g2["mac"] == "1c:34:da:7f:9d:6c"
    assert g2["mgmt_addrs"] == ["172.17.6.101"]


def test_lldp_empty_output_is_empty_dict():
    drv = IosDriver("t", "u", "p", runner=make_runner({"show lldp neighbors detail": ""}))
    assert drv.lldp_neighbors_detail() == {}


# --- the runner is injectable + never leaks the password ---

def test_methods_use_injected_runner_no_network():
    calls = []
    drv = IosDriver("h", "u", "secretpw", runner=lambda cmd: calls.append(cmd) or ALL.get(
        next((k for k in ALL if k in cmd), ""), ""))
    drv.interfaces_status(); drv.mac_address_table(); drv.lldp_neighbors_detail()
    assert any("show interfaces status" in c for c in calls)
    assert any("show mac address-table" in c for c in calls)


# --- batched single-connection poll (the leak-proof design) ---------------
# refresh() runs every per-poll `show` in ONE python-owned ssh session and
# caches the combined output; each read method then parses its own lines out of
# that single blob (the parsers self-filter). One connection per poll, no
# ControlMaster background master -> nothing to orphan/zombie under PID-1 python.

def test_refresh_one_runner_call_then_parse_from_combined_blob():
    calls = []

    def runner(cmd):
        calls.append(cmd)
        return IFSTATUS + "\n" + MACTABLE + "\n" + LLDP

    d = IosDriver("turtle-gouda", "admin", "pw", runner=runner)
    d.refresh()
    assert len(calls) == 1                       # exactly one connection / poll
    for show in ("show interfaces status", "show mac address-table",
                 "show lldp neighbors detail"):
        assert show in calls[0]                  # all batched into that one call
    # every method parses its own lines out of the single combined blob
    assert d.interfaces_status()["Gi1/0/1"] == "link"
    assert d.vlans()["Gi1/0/48"] == {"access_vid": None, "trunk": True}
    assert (16, "98:03:9b:a6:fc:24", "Gi1/0/1") in d.mac_address_table()
    assert d.lldp_neighbors_detail()["Gi1/0/1"][0]["sysname"] == "leopard-bmc"
    assert len(calls) == 1                        # no extra ssh after refresh


def test_ssh_argv_has_no_controlmaster():
    # ControlMaster/ControlPersist is the trap: it backgrounds a master ssh that
    # reparents to PID-1 python and leaks as a zombie. The default runner must
    # build a plain one-shot ssh with neither option.
    d = IosDriver("turtle-gouda", "admin", "pw")
    argv = d._build_ssh_argv()
    joined = " ".join(argv)
    assert "ControlMaster" not in joined
    assert "ControlPersist" not in joined
    assert argv[0] == "sshpass"
    assert "admin@turtle-gouda" in argv
