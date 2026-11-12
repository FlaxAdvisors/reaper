import json, pathlib, pytest
pytest.importorskip("flax_rack")
from flax_rack.geometry import parse_geometry, load_geometry, parse_link, Link, GeometryError
from flax_rack.addressing import plan_addresses, AddressCollision

FIX = pathlib.Path(__file__).parent / "fixtures/rackpost/rr101-geometry.json"

def test_parse_link_forms():
    assert parse_link("C:7") == Link("C", 7, None)
    assert parse_link("A:28/1") == Link("A", 28, 1)
    with pytest.raises(GeometryError): parse_link("C7")

def test_load_rr101():
    g = load_geometry(FIX)
    assert g.url_prefix == "rr101" and g.vid == 32
    assert g.device("C37").links["mezz0"] == Link("C", 8, None)
    assert g.device("L05").pop == "f4-bc-head-25g"

def test_plan_addresses_formula():
    a = {(x.pos, x.role): (x.name, x.ip) for x in plan_addresses(load_geometry(FIX))}
    assert a[("L37", "host")] == ("rr101-l37", "172.32.37.1")
    assert a[("C37", "bmc")] == ("rr101-c37-bmc", "172.32.37.61")
    assert a[("R05", "host")] == ("rr101-r05", "172.32.5.101")
    assert a[("JBOD01", "iom2")] == ("rr101-jbod01-iom2", "172.32.1.12")
    assert ("JBOD01", "host") not in a

def test_collision_names_both_positions():
    d = json.loads(FIX.read_text())
    d["devices"].append(dict(d["devices"][0], pos="X37",    # same OU+col as L37
                             links={"mezz0": "C:30"}))      # own port (dupes fail earlier)
    with pytest.raises(AddressCollision, match="L37.*X37|X37.*L37"):
        plan_addresses(parse_geometry(d))

def test_rack_tag_prefix_and_source():
    d = json.loads(FIX.read_text())
    assert load_geometry(FIX).kea_source == "order:RRORD-101"
    d["rack"], d["network"]["prefix"] = "b", "rr101b-"
    g = parse_geometry(d)
    assert (g.url_prefix, g.kea_source) == ("rr101b", "order:RRORD-101/b")
    d["network"]["prefix"] = "rr101-"
    with pytest.raises(GeometryError, match="prefix"): parse_geometry(d)

def test_unknown_kind_rejected():
    d = json.loads(FIX.read_text()); d["devices"][0]["kind"] = "toaster"
    with pytest.raises(GeometryError, match="toaster"): parse_geometry(d)

def test_duplicate_switch_port_names_both_positions():
    d = json.loads(FIX.read_text())
    a, b = d["devices"][0], d["devices"][1]
    b["links"]["mezz0"] = a["links"]["mezz0"]
    with pytest.raises(GeometryError, match=f"{a['pos']} mezz0 and {b['pos']} mezz0 share"):
        parse_geometry(d)

def test_duplicate_mgmt_port_with_lane_rejected():
    d = json.loads(FIX.read_text())
    d["devices"][0]["links"]["mezz0"] = "C:20/1"
    d["devices"][1]["links"]["mezz0"] = "C:20/1"
    with pytest.raises(GeometryError, match="share switch port C:20/1"):
        parse_geometry(d)
    d["devices"][1]["links"]["mezz0"] = "C:20/2"        # other lane of the same port: fine
    parse_geometry(d)

def test_tor_links_not_checked():
    """The BOM seed repeats ToR breakout links (A:8/4 on L37/C37/R37); Plan 1
    never matches ToR links, so the fixture must keep loading."""
    g = load_geometry(FIX)
    assert [x.links["pcie0"] for x in g.devices[:3]] == [Link("A", 8, 4)] * 3

# ---- Plan 2 Task 0: columns 1..5, per-kind endpoints, 50-per-column blocks ----

def _dev(**kw):
    base = {"pos": "X", "ou": 20, "h": 2, "col": 1, "kind": "compute-tp", "part": "p",
            "links": {}}
    return dict(base, **kw)

def _with(*devs):
    d = json.loads(FIX.read_text())
    d["devices"] = list(devs)
    return d

def test_rr101_col_aliases_parse_to_ints():
    g = load_geometry(FIX)
    assert [g.device(p).col for p in ("L37", "C37", "R37", "JBOD01")] == [1, 2, 3, None]
    assert g.device("JBOD01").endpoints == {"hosts": 0, "bmc": 0, "ioms": 2}
    assert g.device("L37").endpoints == {"hosts": 1, "bmc": 1, "ioms": 0}

def test_col_alias_equals_int():
    a = parse_geometry(_with(_dev(pos="C37", ou=37, col="C"))).device("C37")
    b = parse_geometry(_with(_dev(pos="C37", ou=37, col=2))).device("C37")
    assert a.col == b.col == 2

@pytest.mark.parametrize("col", [6, 0, "X", "2", True])
def test_bad_col_rejected(col):
    with pytest.raises(GeometryError, match="bad col"):
        parse_geometry(_with(_dev(col=col)))

def test_address_examples_table():
    """Every row of the operator-approved examples (spec section 6)."""
    a = {(x.pos, x.role): x.ip for x in plan_addresses(load_geometry(FIX))}
    assert (a[("L37", "host")], a[("L37", "bmc")]) == ("172.32.37.1", "172.32.37.11")
    assert (a[("C37", "host")], a[("C37", "bmc")]) == ("172.32.37.51", "172.32.37.61")
    assert (a[("R37", "host")], a[("R37", "bmc")]) == ("172.32.37.101", "172.32.37.111")
    assert (a[("L05", "host")], a[("L05", "bmc")]) == ("172.32.5.1", "172.32.5.11")
    assert (a[("R05", "host")], a[("R05", "bmc")]) == ("172.32.5.101", "172.32.5.111")
    assert (a[("JBOD01", "iom1")], a[("JBOD01", "iom2")]) == ("172.32.1.11", "172.32.1.12")

def test_col5_block():
    a = {x.role: x.ip for x in plan_addresses(parse_geometry(_with(_dev(col=5))))}
    assert a == {"host": "172.32.20.201", "bmc": "172.32.20.211"}

def test_multi_host_endpoints_override():
    """Yosemite-style sled at OU20 col 4: cards 1-4 .151-.154, BMC .161."""
    g = parse_geometry(_with(_dev(pos="Y20D", col=4, endpoints={"bmc": 1, "hosts": 4})))
    a = {x.role: (x.name, x.ip) for x in plan_addresses(g)}
    assert a == {
        "host1": ("rr101-y20d-n1", "172.32.20.151"), "host2": ("rr101-y20d-n2", "172.32.20.152"),
        "host3": ("rr101-y20d-n3", "172.32.20.153"), "host4": ("rr101-y20d-n4", "172.32.20.154"),
        "bmc": ("rr101-y20d-bmc", "172.32.20.161")}

def test_ioms_follow_bmc():
    g = parse_geometry(_with(_dev(endpoints={"bmc": 1, "hosts": 1, "ioms": 2})))
    a = {x.role: x.ip for x in plan_addresses(g)}
    assert (a["bmc"], a["iom1"], a["iom2"]) == ("172.32.20.11", "172.32.20.12", "172.32.20.13")

@pytest.mark.parametrize("ep,msg", [
    ({"hosts": 10, "bmc": 1}, "max 9 cards"),
    ({"bmc": 1, "ioms": 9}, "max 9"),
    ({"bmc": 2}, "bmc must be 0 or 1"),
    ({"hosts": -1}, "non-negative"),
    ({"cards": 2}, "bad endpoints"),
    ({}, "no endpoints"),
])
def test_bad_endpoints_rejected(ep, msg):
    with pytest.raises(GeometryError, match=msg):
        parse_geometry(_with(_dev(endpoints=ep)))

def test_collision_across_columns_of_multi_host():
    """col 1 with 9 hosts (.1-.9) cannot collide with col 2 (.51+); same col does."""
    plan_addresses(parse_geometry(_with(_dev(pos="A", endpoints={"hosts": 9, "bmc": 1}),
                                        _dev(pos="B", col=2))))
    with pytest.raises(AddressCollision, match="A.*B"):
        plan_addresses(parse_geometry(_with(_dev(pos="A", endpoints={"hosts": 9, "bmc": 1}),
                                            _dev(pos="B"))))


def test_pos_must_be_a_safe_path_segment():
    import copy, json as _j
    base = _j.loads(open(FIX).read())
    for bad in ("../x", "a/b", ".hidden", "-L37", "L 37", "L37.", "", "A" * 33, 37, "é1"):
        geo = copy.deepcopy(base)
        geo["devices"][0]["pos"] = bad
        with pytest.raises(GeometryError, match="bad device pos"):
            parse_geometry(geo)
    geo = copy.deepcopy(base)
    geo["devices"][0]["pos"] = "Yo_sled-1"
    assert parse_geometry(geo).devices[0].pos == "Yo_sled-1"


def test_iom_sides_default_override_and_bad():
    d = json.loads(FIX.read_text())
    assert parse_geometry(d).device("JBOD01").sides == {"iom1": "A", "iom2": "B"}
    assert parse_geometry(d).device("L37").sides == {}
    j = next(x for x in d["devices"] if x["pos"] == "JBOD01")
    j["sides"] = {"iom1": "B", "iom2": "A"}
    assert parse_geometry(d).device("JBOD01").sides == {"iom1": "B", "iom2": "A"}
    for bad in ({"iom1": "C"}, {"iom3": "A"}, ["A", "B"]):
        j["sides"] = bad
        with pytest.raises(GeometryError, match="bad sides"):
            parse_geometry(d)


# ---------------------------------------------------------------- lab racks (howlin)
def base(devices=None, **top):
    d = {"order": "HOWLIN", "customer": "LAB", "site": "lavergne",
         "network": {"vid": 31, "cidr": "172.31.0.0/16", "prefix": "howlin-"},
         "switches": [{"id": "H", "role": "mgmt", "ou": 39, "model": "arista", "nos": "eos",
                       "mgmt_ip": "192.168.88.34", "uplink_ports": [32]}],
         "devices": devices or [dev("L31", "compute-tp")]}
    d.update(top)
    return d


def dev(pos, kind, ou=None, col=None, **kw):
    ou = ou if ou is not None else int("".join(c for c in pos if c.isdigit()) or 1)
    col = col if col is not None else (pos[0] if pos[0] in "LCR" and pos[1:].isdigit() else None)
    return {"pos": pos, "ou": ou, "h": 2, "col": col, "kind": kind, "part": "", **kw}


def test_lab_flags_default():
    g = parse_geometry(base(lab=True))
    assert g.lab is True and g.integration_tests is False
    g = parse_geometry(base())
    assert g.lab is False and g.integration_tests is True
    assert parse_geometry(base(lab=True, integration_tests=True)).integration_tests is True


def test_new_kinds_and_drives():
    g = parse_geometry(base(devices=[dev("C18", "bb-head", drives="BB20"),
                                     dev("BB20", "bigbasin", ou=20, expect={"gpu": 8})]))
    assert g.device("C18").drives == ("BB20",) and g.device("BB20").expect == {"gpu": 8}
    assert g.device("C18").endpoints == {"hosts": 1, "bmc": 1, "ioms": 0}
    assert g.device("BB20").endpoints == {"hosts": 0, "bmc": 1, "ioms": 0}
    for k in ("compute-leopard", "lightning-head", "bc-head", "lightning", "yv2", "yv3"):
        parse_geometry(base(devices=[dev("L31", k)]))


def test_drives_wrong_kind_rejected():
    with pytest.raises(GeometryError, match="drives"):
        parse_geometry(base(devices=[dev("C18", "bb-head", drives="L05"), dev("L05", "compute-tp")]))
    with pytest.raises(GeometryError, match="drives"):
        parse_geometry(base(devices=[dev("C18", "bb-head", drives="NOPE")]))
    with pytest.raises(GeometryError, match="drives"):      # only a head drives anything
        parse_geometry(base(devices=[dev("L31", "compute-tp", drives="BB20"),
                                     dev("BB20", "bigbasin", ou=20)]))


def test_eos_lane_link():
    assert parse_link("H:5/4") == Link("H", 5, 4)

def test_sales_order_is_a_label_only():
    d = json.loads(FIX.read_text())
    assert parse_geometry(d).sales == ""                    # optional
    d["sales"] = "S-ORD101031"
    g = parse_geometry(d)
    assert g.sales == "S-ORD101031"
    # never a key: the order id stays the Kea source, the URL prefix, the order
    assert (g.order, g.kea_source, g.url_prefix) == ("RRORD-101", "order:RRORD-101", "rr101")
    for bad in ("S ORD;1", 101031, "x" * 65):
        d["sales"] = bad
        with pytest.raises(GeometryError, match="sales"): parse_geometry(d)
