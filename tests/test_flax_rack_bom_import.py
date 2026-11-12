"""BOM import. The fixtures are the cells import_bom reads (elevation + cable guide
sheets) lifted from the real BOM workbooks as JSON; each test run rebuilds an .xlsx from
them, so the parser still goes through openpyxl without committing a binary or the
workbooks' commercial sheets (prices, part costs)."""
import functools, json, pathlib, tempfile, pytest
pytest.importorskip("flax_rack"); openpyxl = pytest.importorskip("openpyxl")
from flax_rack.bom_import import import_bom
from flax_rack.geometry import parse_geometry, Link

FIX = pathlib.Path(__file__).parent / "fixtures/rackpost"


@functools.cache
def _xlsx(name):
    """bom-<name>-cells.json ({sheet: [[row, col, value], ...]}) -> a real .xlsx path."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for sheet, cells in json.loads((FIX / f"bom-{name}-cells.json").read_text()).items():
        ws = wb.create_sheet(sheet)
        for r, c, v in cells:
            ws.cell(r, c, v)
    out = pathlib.Path(tempfile.mkdtemp(prefix="bom-")) / f"{name}.xlsx"
    wb.save(out)
    return out


def _imp():
    return import_bom(_xlsx("f4"), order="RRORD-101", customer="F4", site="eindhoven", vid=32,
                      prefix="rr101-")

def test_elevation_devices():
    g = parse_geometry(_imp()[0])
    got = {d.pos: (d.ou, d.h, d.kind, d.part, d.links["mezz0"] if "mezz0" in d.links else None)
           for d in g.devices}
    assert got["L37"] == (37, 2, "compute-tp", "801-0023", Link("C", 7, None))
    assert got["C37"][4] == Link("C", 8, None) and got["R37"][4] == Link("C", 41, None)
    assert got["L05"][:3] == (5, 2, "bc-head-tp") and got["R05"][4] == Link("C", 43, None)
    assert got["JBOD01"][:3] == (1, 4, "bc-jbod")
    assert g.device("JBOD01").links["iom1"] == Link("C", 13, None)
    assert g.device("JBOD01").links["iom2"] == Link("C", 14, None)
    assert _imp()[0]["devices"][-1]["sides"] == {"iom1": "A", "iom2": "B"}   # explicit
    assert g.device("L37").links["pcie0"] == Link("B", 8, 4)
    assert g.device("L05").links["pcie1"] == Link("B", 5, 3)

def test_switches():
    g = parse_geometry(_imp()[0])
    assert {(s.id, s.ou) for s in g.switches} == {("C", 41), ("B", 40), ("A", 39)}

def test_contradictions_reported_not_hidden():
    _, c = _imp()
    assert any("C37" in line and "pcie0" in line for line in c)
    assert any("R37" in line and "pcie0" in line for line in c)
    assert not any("L05" in line or "R05" in line for line in c)  # heads agree

def test_switch_model_from_bom_text_not_seeded():
    g = parse_geometry(_imp()[0])
    m = {s.id: (s.role, s.model) for s in g.switches}
    assert m == {"C": ("mgmt", "celestica"), "B": ("tor", "wedge100"), "A": ("tor", "wedge100")}


# ---------------------------------------------------------------- howlin (lab rack)
from collections import Counter


def _howlin():
    return import_bom(_xlsx("howlin"), order="HOWLIN", customer="LAB", site="lavergne",
                      vid=31, prefix="howlin-")


def test_howlin_seed():
    s, notes = _howlin()
    kinds = Counter(d["kind"] for d in s["devices"])
    assert s["lab"] is True and s["integration_tests"] is False
    assert kinds["compute-tp"] == 6 and kinds["compute-leopard"] == 5
    assert kinds["lightning-head"] == 4 and kinds["lightning"] == 4
    assert kinds["bb-head"] == 1 and kinds["bc-head"] == 1
    assert kinds["bigbasin"] == 1 and kinds["bc-jbod"] == 1
    assert kinds["yv2"] == 4 and kinds["yv3"] == 1
    d = {x["pos"]: x for x in s["devices"]}
    assert d["R18"]["drives"] == "J16" and d["L18"]["drives"] == "J14"
    assert d["R05"]["drives"] == "J12" and d["L05"]["drives"] == "J07"
    assert d["C18"]["drives"] == "BB20" and d["C05"]["drives"] == "BC09"
    assert d["J16"]["expect"] == {"nvme": 4} and d["J12"]["expect"] == {"nvme": 8}
    assert d["BB20"]["expect"] == {"gpu": 8} and d["BC09"]["expect"] == {"disks": 4}
    assert (d["L05"]["ou"], d["L05"]["h"]) == (5, 2) and (d["BB20"]["ou"], d["BB20"]["h"]) == (20, 3)
    assert all(not x.get("links") for x in s["devices"])
    assert [w["id"] for w in s["switches"]] == ["H"] and s["switches"][0]["nos"] == "eos"
    assert any("BC09" in n for n in notes)


def test_howlin_seed_parses_and_addresses():
    from flax_rack.addressing import plan_addresses
    s, _ = _howlin()
    g = parse_geometry(s)
    plan_addresses(g)          # no collision across the 28 devices
    assert g.lab and g.url_prefix == "howlin"
