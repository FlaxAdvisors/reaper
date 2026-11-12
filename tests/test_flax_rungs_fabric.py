import pathlib, subprocess, pytest
pytest.importorskip("flax_rungs")
from flax_rungs.fabric import fabric_addr, apply, assignments
from flax_rack.geometry import load_geometry

F = pathlib.Path(__file__).parent / "fixtures/rackpost"


def geo():
    return load_geometry(F / "rr101-geometry.json")


def dev(g, pos):
    return next(d for d in g.devices if d.pos == pos)


def test_addrs():
    g = geo()
    assert fabric_addr(g, dev(g, "L37"), "A", 1) == "10.201.37.1/16"
    assert fabric_addr(g, dev(g, "L05"), "B", 1) == "10.202.5.1/16"


def test_octet_comes_from_the_address_plan():
    from flax_rack.addressing import plan_addresses
    g = geo()
    for a in plan_addresses(g):
        if a.role == "host":
            x = int(a.ip.rsplit(".", 1)[1])
            assert fabric_addr(g, dev(g, a.pos), "A", x) == f"10.201.{dev(g, a.pos).ou}.{x}/16"
    with pytest.raises(ValueError):
        fabric_addr(g, dev(g, "L37"), "A", 0)


def test_other_columns_and_no_host():
    import json
    from flax_rack.geometry import parse_geometry
    d = json.load(open(F / "rr101-geometry.json"))
    c = dict(d["devices"][0], pos="C37", col="C")
    r = dict(d["devices"][0], pos="R05", ou=5, col="R", links={"mezz0": "C:9", "pcie0": "A:8/4", "pcie1": "B:8/4"})
    d["devices"] = [c, r]
    g = parse_geometry(d)
    assert fabric_addr(g, dev(g, "C37"), "A", 51) == "10.201.37.51/16"
    assert fabric_addr(g, dev(g, "R05"), "B", 101) == "10.202.5.101/16"
    with pytest.raises(ValueError):
        fabric_addr(g, dev(g, "C37"), "C", 51)


def test_jbod_no_host_raises():
    g = geo()
    jb = [d for d in g.devices if not d.endpoints.get("hosts")]
    if not jb:
        pytest.skip("no jbod in fixture")
    with pytest.raises(ValueError):
        fabric_addr(g, jb[0], "A", 1)


def test_assignments_use_seen_side():
    g = geo()
    links = [{"link": "pcie0", "expected": "x", "seen": "B Ethernet31", "ifname": "e0"},
             {"link": "pcie1", "expected": "x", "seen": "B Ethernet31", "ifname": "e1"},
             {"link": "pcie2", "expected": "x", "seen": None, "ifname": None}]
    assert assignments(g, dev(g, "L37"), links, 1) == {"e0": "10.202.37.1/16", "e1": "10.202.37.1/16"}


class R:
    def __init__(self, out, rc=0):
        self.out, self.rc, self.calls = out, rc, []

    def __call__(self, argv, **kw):
        self.calls.append(kw["input"])
        return subprocess.CompletedProcess(argv, self.rc, self.out, "")


def test_apply_pass_uses_replace():
    r = R("OK e0\nOK e1\n")
    res = apply("1.2.3.4", "flax", "p", {"e0": "10.201.37.1/16", "e1": "10.202.37.1/16"}, runner=r)
    assert res.verdict == "pass" and res.detail == {"e0": "10.201.37.1/16", "e1": "10.202.37.1/16"}
    s = r.calls[0]
    assert "ip addr replace 10.201.37.1/16 dev e0" in s and "ip addr add" not in s
    assert "ip -4 -o addr show dev e1" in s and "ip link set e0 up" in s


def test_apply_verify_failure():
    res = apply("1.2.3.4", "flax", "p", {"e0": "10.201.37.1/16", "e1": "10.202.37.1/16"},
                runner=R("OK e0\nFAIL e1\n"))
    assert res.verdict == "fail" and "e1" in res.detail["error"]


def test_apply_empty_fails():
    assert apply("h", "u", "p", {}, runner=R("")).verdict == "fail"
