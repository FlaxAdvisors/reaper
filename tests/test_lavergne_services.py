"""Lavergne services files (spec 2026-10-27-lavergne-eindhoven-rebuild §6.1, §6.5)."""
import json
import os
import re

_REAPER = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
SVC = os.path.join(_REAPER, "files", "configs", "lavergne", "services")
EHV = os.path.join(_REAPER, "files", "configs", "eindhoven", "services")



def load(name, base=SVC):
    with open(os.path.join(base, name)) as f:
        return json.load(f)


def raw(name):
    with open(os.path.join(SVC, name), "rb") as f:
        return f.read()


def ports(geometry):
    return {s["port"] for s in (geometry["slots"] if isinstance(geometry, dict) else geometry)}


def mgmt_hosts_lines():
    out = []
    with open(os.path.join(SVC, "mgmt-hosts")) as f:
        for line in f:
            line = line.split("#", 1)[0].split()
            if len(line) >= 2:
                out.append((line[0], line[1]))
    return out


def reservations():
    out = {}
    with open(os.path.join(SVC, "bmc-reservations.conf")) as f:
        for line in f:
            m = re.match(r"^dhcp-host=([0-9a-f:]{17}),([0-9.]+),", line.strip())
            if m:
                out[m.group(1)] = m.group(2)
    return out


def test_triage_geometry_is_rhythms_live_file_plus_stations():
    g = load("geometry.json")
    assert ports(g) == {"et%db%d" % (c, b) for c in (29, 30, 31) for b in (1, 2, 3, 4)} | {
        "et25b1", "et25b2", "et25b3"}
    # mezz-flash stations, OU16 of the triage rack (operator 2026-10-27)
    assert {s["port"]: s["ou"] for s in g if s["port"].startswith("et25")} == {
        "et25b1": "16L", "et25b2": "16C", "et25b3": "16R"}


def test_post_geometry_is_eindhoven_rack_on_rabbit_blues():
    lvq, ehv = load("post-geometry.json"), load("post-geometry.json", EHV)
    assert lvq["racks"] == [{"switch": "rabbit-blues", "tag": "", "label": "Lavergne Post"}]
    # same ports and OU layout as eindhoven's rack, on lavergne's post switch
    assert {s["switch"] for s in lvq["slots"]} == {"rabbit-blues"}
    strip = lambda rows: [{k: v for k, v in r.items() if k != "switch"} for r in rows]
    assert strip(lvq["slots"]) == strip(ehv["slots"])


def test_switches_json():
    assert {(s["name"], s["driver"], s["credentials_key"]) for s in load("switches.json")} == {
        ("rabbit-rhythm", "eos", "eos"), ("rabbit-blues", "eos", "eos"),
        ("turtle-rhythm", "cumulus", "cumulus")}


def test_mgmt_hosts_match_spec_table():
    # Operator 2026-10-27: bang BMCs .23/.24; switch BMCs packed into .11-.16;
    # turtle-c2g .36, rabbit-muddy .37, rabbit-cato .38.
    assert dict(mgmt_hosts_lines()) == {
        "192.168.88.11": "rabbit-rhythm-bmc", "192.168.88.12": "rabbit-blues-bmc",
        "192.168.88.13": "rabbit-howlin-bmc", "192.168.88.14": "rabbit-c2g-bmc",
        "192.168.88.15": "rabbit-muddy-bmc", "192.168.88.16": "rabbit-cato-bmc",
        "192.168.88.23": "bang-rhythm-bmc", "192.168.88.24": "bang-blues-bmc",
        "192.168.88.31": "rabbit-rhythm", "192.168.88.32": "rabbit-blues",
        "192.168.88.33": "turtle-rhythm", "192.168.88.34": "rabbit-howlin",
        "192.168.88.35": "rabbit-c2g", "192.168.88.36": "turtle-c2g",
        "192.168.88.37": "rabbit-muddy", "192.168.88.38": "rabbit-cato"}


def test_mgmt_dhcp_pins():
    # Imported into kea.hosts by flax_classify.legacy_import at the Kea cutover.
    assert reservations() == {
        "3c:2c:99:69:4a:e7": "192.168.88.11", "3c:2c:99:60:d3:37": "192.168.88.12",
        "98:19:2c:3a:95:79": "192.168.88.13", "b8:6a:97:fd:20:66": "192.168.88.14",
        "80:a2:35:d2:cd:c4": "192.168.88.15", "3c:2c:99:3c:73:d5": "192.168.88.16",
        "00:90:fb:62:2d:bd": "192.168.88.31", "00:90:fb:61:da:d0": "192.168.88.32",
        "a8:2b:b5:17:aa:c0": "192.168.88.33", "00:90:fb:75:f0:f0": "192.168.88.34",
        "00:90:fb:65:ce:84": "192.168.88.35", "a8:2b:b5:2e:fc:40": "192.168.88.36",
        "00:90:fb:6a:89:9f": "192.168.88.37", "00:90:fb:61:d1:a8": "192.168.88.38"}


def test_pins_and_hosts_agree():
    hosts = dict(mgmt_hosts_lines())
    for ip in reservations().values():
        assert ip in hosts, ip


def test_no_mgmt_address_used_twice():
    names = mgmt_hosts_lines()
    assert len({ip for ip, _ in names}) == len(names)
    assert "192.168.88.10" not in dict(names)  # the VIP is keepalived's, not a host


def test_credentials_are_vaulted():
    for f in ("credentials.json", "credentials-bmc.json", "credentials-host.json",
              "credentials-redfish.json"):
        assert raw(f)[:14] == b"$ANSIBLE_VAULT", f


def test_copied_maps_match_eindhoven():
    # node_config is one shared set (files/services/_common/node_config), not compared here.
    for d in ("family-map", "macmath"):
        assert sorted(os.listdir(os.path.join(SVC, d))) == sorted(os.listdir(os.path.join(EHV, d))), d
    assert load("bmc-only-families.json") == load("bmc-only-families.json", EHV)
    assert load("no-steer-ports.json") == []


def test_triage_geometry_names_its_switch():
    # flax_observe defaults a switch-less entry to --default-switch=rabbit-lorax
    # (braintree), which keyed every lavergne port to a switch that does not
    # exist here -> all tiles "off" (2026-10-27). Eindhoven's entries carry it.
    assert {e.get("switch") for e in load("geometry.json")} == {"rabbit-rhythm"}
