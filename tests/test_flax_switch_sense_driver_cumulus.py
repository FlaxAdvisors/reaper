"""Unit tests for flax_switch_sense.driver_cumulus.CumulusDriver.

Pure/local: an injected runner returns canned command output keyed by command
substring, and we assert each of the four public methods returns the EosDriver-
matching shape so slice.py consumes Cumulus and Arista facts identically. No
real switch, no network.

The driver bypasses NCLU (`net show ...`, which stalls ~5s+ in netd on the old
turtle-lorax 3.7.3) and reads the kernel/lldpd directly:
  interfaces_status()  grep -H . /sys/class/net/swp*/operstate
  vlans()              /sbin/bridge -j vlan show
  mac_address_table()  sudo bridge -j fdb show   (already direct)
  lldp_neighbors_detail()  sudo /usr/sbin/lldpctl -f json
"""
import json
import pathlib

from flax_switch_sense.driver_cumulus import (
    CumulusDriver, parse_cumulus_port_mask, _BATCH_MARK,
)

_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "braintree" / "turtle-lorax" / "show"


def make_runner(responses: dict):
    """Return a runner(cmd) that matches cmd against substring keys in
    `responses` and returns the canned text (json.dumps if not already str)."""
    def runner(cmd: str) -> str:
        for needle, payload in responses.items():
            if needle in cmd:
                if isinstance(payload, str):
                    return payload
                return json.dumps(payload)
        return ""
    return runner


# --- interfaces_status fixture: `grep -H . /sys/class/net/swp*/operstate` ---
# The glob only matches swp* in reality; the eth0 line proves the parser's
# swp-only filter (and no crash on a stray non-swp path).

OPERSTATE = (
    "/sys/class/net/swp1/operstate:up\n"
    "/sys/class/net/swp2/operstate:down\n"
    "/sys/class/net/swp3/operstate:lowerlayerdown\n"
    "/sys/class/net/swp4/operstate:unknown\n"
    "/sys/class/net/eth0/operstate:up\n"
)

BRIDGE_VLAN = {
    "swp1": [{"vlan": 24, "flags": ["PVID", "Egress Untagged"]}],
    "swp4": [
        {"vlan": 10, "flags": []},
        {"vlan": 11, "flags": []},
        {"vlan": 12, "flags": []},
    ],
    "swp5": [{"vlan": 99, "flags": ["PVID", "Egress Untagged"]},
             {"vlan": 100, "flags": []}],
}

FDB = [
    {"mac": "98:03:9b:a6:fc:24", "dev": "swp1", "vlan": 24, "master": "bridge",
     "state": "", "flags": []},
    {"mac": "98:03:9b:a6:fc:22", "dev": "swp1", "vlan": 24, "master": "bridge",
     "state": "", "flags": []},
    {"mac": "00:11:22:33:44:55", "dev": "swp2", "master": "bridge",
     "state": "permanent", "flags": ["permanent"]},
    {"mac": "01:00:5e:00:00:01", "dev": "swp1", "vlan": 24, "flags": []},
    {"mac": "aa:bb:cc:dd:ee:ff", "dev": "bridge", "flags": []},
]

# lldpctl -f json (this lldpd version) list-wraps id / name / mgmt-ip as
# single-element lists — unlike net-show-lldp-json's dict form. The chassis MAC
# lives at chassis[0].id[0].value and the neighbor's remote port at port[0].id.
LLDPCTL = {
    "lldp": [
        {
            "interface": [
                {
                    "name": "swp1",
                    "chassis": [
                        {
                            "id": [{"type": "mac", "value": "98:03:9b:a6:fc:24"}],
                            "name": [{"value": "tiogapass-bmc-6-1"}],
                            "mgmt-ip": [{"value": "172.24.6.101"}],
                        }
                    ],
                    "port": [{"id": [{"type": "ifname", "value": "eth0"}]}],
                },
            ]
        }
    ]
}


def base_runner():
    return make_runner({
        "operstate": OPERSTATE,
        "bridge -j vlan show": BRIDGE_VLAN,
        "bridge -j fdb show": FDB,
        "lldpctl -f json": LLDPCTL,
    })


def test_interfaces_status_parses_operstate():
    d = CumulusDriver("turtle-lorax", "root", "secret", runner=base_runner())
    out = d.interfaces_status()
    assert out["swp1"] == "link"       # up
    assert out["swp2"] == "nolink"     # down
    assert out["swp3"] == "nolink"     # lowerlayerdown
    assert out["swp4"] == "unknown"    # anything else
    # non-swp ports excluded
    assert "eth0" not in out


def test_driver_never_calls_netd():
    """The whole point of the bypass: no read may go through NCLU/netd."""
    calls = []
    inner = base_runner()

    def recording(cmd):
        calls.append(cmd)
        return inner(cmd)

    d = CumulusDriver("turtle-lorax", "root", "secret", runner=recording)
    d.interfaces_status()
    d.vlans()
    d.mac_address_table()
    d.lldp_neighbors_detail()
    assert calls, "expected the driver to issue commands"
    assert not any(c.split()[0] == "net" or " net " in c for c in calls), \
        f"a read still went through netd: {calls}"
    assert any("operstate" in c for c in calls)


def test_vlans_access_vs_trunk_shape():
    d = CumulusDriver("turtle-lorax", "root", "secret", runner=base_runner())
    out = d.vlans()
    # access port: single PVID-untagged vlan
    assert out["swp1"] == {"access_vid": 24, "trunk": False}
    # trunk port: multiple vlans
    assert out["swp4"]["trunk"] is True
    assert out["swp4"]["access_vid"] is None
    # PVID + a tagged vlan => trunk
    assert out["swp5"]["trunk"] is True


def test_vlans_matches_slice_derivation():
    """slice.py reads v['trunk'] + v.get('access_vid'); confirm the shape it
    expects is produced identically to EOS."""
    from flax_switch_sense.slice import slice_by_port
    d = CumulusDriver("turtle-lorax", "root", "secret", runner=base_runner())
    per_port = slice_by_port({}, d.vlans(), [], {})
    assert per_port["swp1"]["mask"] == "access"
    assert per_port["swp1"]["access_vid"] == 24
    assert per_port["swp4"]["mask"] == "trunk"
    assert "access_vid" not in per_port["swp4"]


def test_vlans_falls_back_to_interfaces_file():
    """When bridge-vlan json is empty, parse /etc/network/interfaces."""
    interfaces_text = (
        "auto swp1\n"
        "iface swp1\n"
        "    bridge-access 24\n"
        "\n"
        "auto swp4\n"
        "iface swp4\n"
        "    bridge-vids 10 11 12\n"
    )
    runner = make_runner({
        "bridge -j vlan show": {},
        "cat /etc/network/interfaces": interfaces_text,
    })
    d = CumulusDriver("turtle-lorax", "root", "secret", runner=runner)
    out = d.vlans()
    assert out["swp1"] == {"access_vid": 24, "trunk": False}
    assert out["swp4"] == {"access_vid": None, "trunk": True}


def test_mac_address_table_parses_dynamic_only():
    d = CumulusDriver("turtle-lorax", "root", "secret", runner=base_runner())
    out = d.mac_address_table()
    assert (24, "98:03:9b:a6:fc:24", "swp1") in out
    assert (24, "98:03:9b:a6:fc:22", "swp1") in out
    # permanent entry skipped
    assert not any(e[1] == "00:11:22:33:44:55" for e in out)
    # multicast mac skipped
    assert not any(e[1] == "01:00:5e:00:00:01" for e in out)
    # non-swp dev skipped
    assert not any(e[2] == "bridge" for e in out)


def test_lldp_neighbors_detail_extracts_per_port():
    d = CumulusDriver("turtle-lorax", "root", "secret", runner=base_runner())
    out = d.lldp_neighbors_detail()
    assert "swp1" in out
    n = out["swp1"][0]
    assert n["mac"] == "98:03:9b:a6:fc:24"
    assert n["sysname"] == "tiogapass-bmc-6-1"
    assert n["port_description"] == "eth0" and n["port_id"] == "eth0"
    assert n["mgmt_addrs"] == ["172.24.6.101"]


def test_lldp_tolerates_list_interface_form():
    """lldpd alt shape: interface as list, chassis as a direct dict with id."""
    alt = {
        "lldp": {
            "interface": [
                {
                    "name": "swp2",
                    "chassis": {
                        "id": {"type": "mac", "value": "AA:BB:CC:00:11:22"},
                        "name": {"value": "leopard-bmc"},
                        "mgmt-ip": [{"value": "172.24.6.50"}],
                    },
                    "port": [{"descr": {"value": "mgmt0"}}],
                }
            ]
        }
    }
    runner = make_runner({"lldpctl -f json": alt})
    d = CumulusDriver("turtle-lorax", "root", "secret", runner=runner)
    out = d.lldp_neighbors_detail()
    n = out["swp2"][0]
    assert n["mac"] == "aa:bb:cc:00:11:22"
    assert n["sysname"] == "leopard-bmc"
    assert n["port_description"] == "mgmt0"
    assert n["mgmt_addrs"] == ["172.24.6.50"]


def test_run_json_strips_ssh_banner_prefix():
    """A stray banner line can prepend cruft before the JSON; _run_json finds
    the first brace. vlans() still routes through _run_json (bridge -j vlan)."""
    noisy = "Shared connection to turtle closed.\r\n" + json.dumps(BRIDGE_VLAN)
    runner = make_runner({"bridge -j vlan show": noisy})
    d = CumulusDriver("turtle-lorax", "root", "secret", runner=runner)
    out = d.vlans()
    assert out["swp1"] == {"access_vid": 24, "trunk": False}


def test_methods_tolerate_empty_output():
    """A switch returning nothing parseable yields empty containers, not raises."""
    runner = make_runner({})  # everything returns ""
    d = CumulusDriver("turtle-lorax", "root", "secret", runner=runner)
    assert d.interfaces_status() == {}
    assert d.mac_address_table() == []
    assert d.lldp_neighbors_detail() == {}
    # vlans tries bridge-vlan then interfaces file; both empty => empty dict
    assert d.vlans() == {}


# --- batched single-connection fetch (refresh() runs one ssh) ---

def _marked_blob(sections: dict) -> str:
    """Assemble the marker-delimited blob a batched ssh would return."""
    parts = []
    for key in ("operstate", "bridge_vlan", "fdb", "lldp"):
        parts.append(_BATCH_MARK + key)
        if key in sections:
            parts.append(sections[key])
    parts.append(_BATCH_MARK + "end")
    return "\n".join(parts)


def test_split_sections_splits_on_markers():
    blob = _marked_blob({"operstate": "a\nb", "bridge_vlan": "{}",
                         "fdb": "[]", "lldp": "{}"})
    sec = CumulusDriver._split_sections(blob)
    assert sec["operstate"] == "a\nb"
    assert sec["bridge_vlan"] == "{}"
    assert sec["fdb"] == "[]"
    assert sec["lldp"] == "{}"
    assert "end" not in sec  # the trailing sentinel is not a real section


def test_refresh_batches_one_ssh_and_caches():
    """refresh() must issue ONE runner call (the batch script); every read then
    parses its section from the cache, issuing no further calls."""
    sections = {
        "operstate": OPERSTATE,
        "bridge_vlan": json.dumps(BRIDGE_VLAN),
        "fdb": json.dumps(FDB),
        "lldp": json.dumps(LLDPCTL),
    }
    calls = []

    def runner(cmd):
        calls.append(cmd)
        assert _BATCH_MARK in cmd, "refresh must send the batch script"
        return _marked_blob(sections)

    d = CumulusDriver("turtle-lorax", "root", "x", runner=runner)
    d.refresh()
    assert len(calls) == 1
    assert d.interfaces_status()["swp1"] == "link"
    assert d.vlans()["swp1"] == {"access_vid": 24, "trunk": False}
    assert any(e[1] == "98:03:9b:a6:fc:24" for e in d.mac_address_table())
    assert d.lldp_neighbors_detail()["swp1"][0]["mac"] == "98:03:9b:a6:fc:24"
    # all four reads parsed from cache -> still exactly one runner call
    assert len(calls) == 1


def test_reads_fall_back_to_per_command_without_refresh():
    """Without refresh() (unit/test path) reads still work per-command."""
    d = CumulusDriver("turtle-lorax", "root", "x", runner=base_runner())
    assert d.interfaces_status()["swp1"] == "link"
    assert d.vlans()["swp1"]["access_vid"] == 24


# --- golden tests against real output captured from the live turtle-lorax ---

def golden_runner():
    return make_runner({
        "operstate": (_FIXTURES / "operstate.txt").read_text(),
        "bridge -j vlan show": (_FIXTURES / "bridge-vlan.json").read_text(),
        "bridge -j fdb show": (_FIXTURES / "bridge-fdb.json").read_text(),
        "lldpctl -f json": (_FIXTURES / "lldpctl.json").read_text(),
    })


def test_golden_interfaces_status():
    d = CumulusDriver("turtle-lorax", "root", "secret", runner=golden_runner())
    out = d.interfaces_status()
    assert len(out) == 52
    assert sum(1 for v in out.values() if v == "link") == 8
    assert sum(1 for v in out.values() if v == "nolink") == 44
    assert all(p.startswith("swp") for p in out)


def test_golden_vlans_shape():
    d = CumulusDriver("turtle-lorax", "root", "secret", runner=golden_runner())
    out = d.vlans()
    assert len(out) == 52
    # turtle-lorax's swp ports are bridge trunks (many vlans each)
    assert any(v["trunk"] for v in out.values())


def test_golden_lldp_finds_rabbit_lorax_neighbor():
    """The real lldpctl -f json list-wraps id/name; swp2 faces rabbit-lorax."""
    d = CumulusDriver("turtle-lorax", "root", "secret", runner=golden_runner())
    out = d.lldp_neighbors_detail()
    assert "swp2" in out
    n = out["swp2"][0]
    assert n["mac"] == "00:90:fb:5f:d8:af"
    assert n["sysname"] == "rabbit-lorax"


def test_golden_mac_table_nonempty():
    d = CumulusDriver("turtle-lorax", "root", "secret", runner=golden_runner())
    out = d.mac_address_table()
    assert out and all(isinstance(e, tuple) and len(e) == 3 for e in out)


def test_parse_cumulus_port_mask_captures_vid():
    text = (
        "auto swp7\n"
        "iface swp7\n"
        "    bridge-access 31\n"
        "auto swp8\n"
        "iface swp8\n"
        "    bridge-vids 100 200\n"
    )
    mask = parse_cumulus_port_mask(text)
    assert mask["access"] == {"swp7": 31}
    assert mask["trunk"] == {"swp8"}
