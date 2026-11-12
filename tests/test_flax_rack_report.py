"""flax_rack.report: Done's component list and the customer CSVs."""
import csv

import pytest

pytest.importorskip("flax_rack")
from flax_rack import report
from flax_rack.geometry import load_geometry
from flax_rack.orderdb import OrderDB
from flax_rungs.result import RungResult
from tests.test_flax_rack_geometry import FIX

# macinv -v detail lines in the R05 shape (fake serials / locally-administered MACs)
R05_MACINV_V = """\
BMC Version:  0.08  Facebook, Inc.
BIOS Version: F09_3A23, Release Date: 07/29/2021, BIOS Revision: 5.14, Firmware Revision: 0.0
Board Mfg: Wiwynn, Product: Tioga Pass Single Side, Serial: FAKEBOARD01, Product Serial: FAKEPS0501, Chassis Serial: FAKECS0501
Processor Version: Intel(R) Xeon(R) Gold 6138 CPU @ 2.00GHz, Serial: To be filled by O.E.M.
Memory: 32 GB, DIMM A0, _Node0_Channel0_Dimm0, 2666 MT/s, Samsung, 0000F00D, M393A4K40CB2-CTD
BusID: 5e:00.0, Eth MAC: 02:00:00:00:05:01, Model: "Mellanox MCX4411A-ACQN, ConnectX-4 Lx EN OCP, 1x25Gb", SubVendor: pci 0x15b3 "Mellanox Technologies", Interface: pxedev
BusID: 5e:00.0, firmware-version: 14.32.1010 (MT_2450112034)
BusID: 5e:00.0, [SN] Serial number: FAKENICSN01
Handle_: 92, BusID: 00:17.0, Model: "Intel C620 Series Chipset Family SATA Controller [AHCI mode]", Driver: "ahci"
Handler: 190, BusID: nvme0, Model: "Sandisk WD Black SN850", SubVendor: pci 0x15b7 "Sandisk Corp", SubDevice: pci 0x5011 , Serial ID: "222815805096", Capacity: 931 GB
Handler: 7, BusID: 1:0:0:0, Model: "Sandisk SSD PLUS", Revision: "UH51", Capacity: 447 GB
BusID: 03:00.0, Model: "ASPEED AST1000/2000", Device: pci 0x2000 "AST1000/2000", SubDevice: pci 0x2000 , Revision: 0x41
"""


@pytest.fixture
def rack(tmp_path):
    g = load_geometry(FIX)
    db = OrderDB(tmp_path / "order.db")
    db.init(g)
    return tmp_path, g, db


def _run(db, pos, stage, rung, verdict, detail=None, artifacts=None):
    rid = db.start_run(pos, stage, rung)
    db.finish_run(rid, RungResult(verdict, detail or {}, artifacts or {}))
    return rid


def _inventory(order_dir, db, pos, text):
    rel = f"artifacts/{pos}/q1/inventory/macinv-v.txt"
    (order_dir / rel).parent.mkdir(parents=True, exist_ok=True)
    (order_dir / rel).write_text(text)
    return _run(db, pos, "qualify", "inventory", "pass", {"stage": "inventory"},
                {"macinv-v": rel})


def _read(path):
    with open(path, newline="") as f:
        return list(csv.reader(f))


def test_components_from_inventory():
    rows = report.components_from_inventory(R05_MACINV_V)
    disks = [r for r in rows if r["kind"] == "disk"]
    assert disks[0] == {"kind": "disk", "model": "Sandisk WD Black SN850",
                        "serial": "222815805096", "slot": "nvme0"}
    assert [r["kind"] for r in rows] == ["cpu", "dimm", "nic", "disk", "disk", "gpu"]
    cpu, dimm, nic = rows[0], rows[1], rows[2]
    assert cpu["model"] == "Intel(R) Xeon(R) Gold 6138 CPU @ 2.00GHz" and cpu["slot"] == "CPU0"
    assert dimm == {"kind": "dimm", "model": "M393A4K40CB2-CTD", "serial": "0000F00D",
                    "slot": "DIMM A0"}
    assert nic["serial"] == "FAKENICSN01" and nic["slot"] == "5e:00.0"


def test_blank_serial_stays_blank():
    rows = report.components_from_inventory(R05_MACINV_V)
    plus = next(r for r in rows if r["model"] == "Sandisk SSD PLUS")
    assert plus["serial"] == "" and plus["slot"] == "1:0:0:0"
    gpu = next(r for r in rows if r["kind"] == "gpu")
    assert gpu["serial"] == ""


def test_csv_roundtrip_quotes_commas(rack):
    order_dir, g, db = rack
    odd = ['Mellanox MCX4411A-ACQN, ConnectX-4 Lx EN OCP, 1x25Gb',
           '"Sandisk SSD PLUS"', 'Intel(R) Xeon(R) Gold 6138 CPU @ 2.00GHz']
    rid = _inventory(order_dir, db, "R05", R05_MACINV_V)
    db.set_components("R05", [{"kind": "disk", "model": m, "serial": "", "slot": str(i)}
                              for i, m in enumerate(odd)], source_run=rid)
    paths = report.write(order_dir, g, db)
    rows = _read(paths["inventory"])
    assert tuple(rows[0]) == ("pos", "kind", "model", "serial", "slot")    # no order / rack
    assert [r[2] for r in rows[1:]] == odd
    assert rows[1][:2] == ["R05", "disk"]
    # re-set replaces that pos's rows
    db.set_components("R05", [{"kind": "cpu", "model": "x", "serial": "s", "slot": "CPU0"}], 8)
    assert [(c["kind"], c["source_run"]) for c in db.components("R05")] == [("cpu", 8)]


def test_csv_columns_stable(rack):
    """Operator 2026-10-31: a physical file and a FW + qual file, top down (highest OU
    first, then L, C, R), no order / hostname column, rack only for a multi-rack order."""
    order_dir, g, db = rack
    paths = report.write(order_dir, g, db)
    devs, fw = _read(paths["devices"]), _read(paths["firmware"])
    assert devs[0] == ["pos", "ou", "kind", "part", "serial", "bmc_mac", "host_mac",
                       "fabric_a_mac", "fabric_b_mac", "mgmt_port", "fabric_a_port",
                       "fabric_b_port"]
    assert fw[0] == ["pos", "ou", "kind", "serial", "bios_version", "bmc_version",
                     "bmc_rom_version", "nic_firmware", "qualify", "deploy", "ha_mode", "done"]
    keys = ["_sw-C", "_sw-B", "_sw-A", "L37", "C37", "R37", "L05", "R05", "JBOD01",
            "JBOD01.iom1", "JBOD01.iom2"]
    assert [r[0] for r in devs[1:]] == keys and [r[0] for r in fw[1:]] == keys
    assert all(len(r) == len(devs[0]) for r in devs) and all(len(r) == len(fw[0]) for r in fw)
    assert not list(order_dir.glob(".*.tmp"))
    g.rack = "b"                                    # a rack-tagged order: the rack column
    for name in ("devices", "firmware", "inventory"):
        assert _read(report.write(order_dir, g, db)[name])[0][0] == "rack"


def test_rack_tag_in_csv_names(rack):
    """Two racks of one order write two sets of CSVs, not one."""
    _order_dir, g, _db = rack
    tag = f"{g.customer}-{g.order}"
    assert report.csv_names(g)["devices"] == f"{tag}-devices.csv"
    g.rack = "b"
    assert report.csv_names(g) == {k: f"{tag}-b-{k}.csv"
                                   for k in ("devices", "firmware", "inventory")}
    assert all(report.CSV_RE.fullmatch(n) for n in report.csv_names(g).values())


def test_rung_not_run_is_blank_not_fail(rack):
    order_dir, g, db = rack
    _run(db, "C37", "qualify", "inventory", "pass")     # the rest of Qualify never ran
    _run(db, "R37", "qualify", "inventory", "fail")
    fw = {r["pos"]: r for r in csv.DictReader(open(report.write(order_dir, g, db)["firmware"],
                                                   newline=""))}
    assert fw["C37"]["qualify"] == "" and fw["R37"]["qualify"] == "fail"


def test_undeployed_device_row_has_no_borrowed_values(rack):
    order_dir, g, db = rack
    db.set_macs("C37", host_mac="02:00:00:00:37:01", bmc_mac="02:00:00:00:37:0b", iom_macs=[])
    _inventory(order_dir, db, "C37", R05_MACINV_V)
    _run(db, "C37", "firmware", "bmc-checked", "pass",
         {"slice": {"phase": "up_to_date", "current_version": "fake-bmc-2.0"}})
    _run(db, "C37", "firmware", "bmc-backup-checked", "pass",
         {"backup_version": "fake-bmc-1.9", "active_version": "fake-bmc-2.0"})
    _run(db, "C37", "firmware", "bios-checked", "pass",
         {"slice": {"phase": "up_to_date", "current": "F09_3A23"}})
    _run(db, "C37", "firmware", "mlx-checked", "pass",
         {"slice": {"phase": "up_to_date", "devices": [{"current": "14.32.1010"}]}})
    for r in ("install", "firstboot", "install-console", "flax-user", "ha-config", "ha-test",
              "deploy-iperf", "deploy-stress"):
        _run(db, "C37", "deploy", r, "pass",
             {"macs": {"A": "02:00:00:00:37:a1", "B": "02:00:00:00:37:b1"},
              "address": "10.201.37.51/16"} if r == "ha-config" else {})
    db.set_ha_mode({"mode": "active-backup (no MLAG peer link)", "devices": ["C37"],
                    "missing": ["L37"]})
    _run(db, "C37", "done", "end-summary", "pass")
    _run(db, "C37", "done", "boot-reset", "pass")
    # L37 never deployed: an unmeasured end-summary is its only Done row
    _run(db, "L37", "done", "end-summary", "unmeasured", {"headline": "SEL not read"})
    _run(db, "L37", "done", "boot-reset", "pass")

    out = report.write(order_dir, g, db)
    rows = {r["pos"]: r for r in csv.DictReader(open(out["devices"], newline=""))}
    for r in csv.DictReader(open(out["firmware"], newline="")):
        rows[r["pos"]].update(r)
    c, l = rows["C37"], rows["L37"]
    assert c["serial"] == "FAKEPS0501"
    assert (c["bmc_mac"], c["host_mac"]) == ("02:00:00:00:37:0b", "02:00:00:00:37:01")
    assert (c["fabric_a_mac"], c["fabric_b_mac"]) == ("02:00:00:00:37:a1", "02:00:00:00:37:b1")
    assert (c["bmc_version"], c["bmc_rom_version"], c["bios_version"], c["nic_firmware"]) == (
        "fake-bmc-2.0", "fake-bmc-1.9", "F09_3A23", "14.32.1010")
    assert c["deploy"] == "pass" and c["done"] == "pass"
    assert c["ha_mode"] == "active-backup"            # an old row's text, served in today's
    for col in ("serial", "bmc_mac", "host_mac", "fabric_a_mac", "bmc_version",
                "bios_version", "nic_firmware", "qualify", "deploy", "ha_mode"):
        assert l[col] == "", col
    assert l["done"] == "unmeasured"
    assert rows["JBOD01.iom1"]["bmc_version"] == "" and rows["_sw-A"]["deploy"] == ""


def test_failed_ha_config_gives_no_fabric_values(rack):
    order_dir, g, db = rack
    _run(db, "R37", "deploy", "ha-config", "fail",
         {"macs": {"A": "02:00:00:00:37:a3", "B": "02:00:00:00:37:b3"},
          "address": "10.201.37.101/16", "error": "bond0 not up"})
    out = report.write(order_dir, g, db)
    r = next(r for r in csv.DictReader(open(out["devices"], newline="")) if r["pos"] == "R37")
    f = next(r for r in csv.DictReader(open(out["firmware"], newline="")) if r["pos"] == "R37")
    assert (r["fabric_a_mac"], f["deploy"]) == ("", "fail")


def test_components_follow_the_latest_inventory(rack):
    """Done read run N; the blade was re-qualified (run N+1) and Done not re-run: the CSV
    lists run N+1's parts, never run N's beside run N+1's chassis serial. No Done at all:
    the latest inventory's parts too."""
    order_dir, g, db = rack
    old = _inventory(order_dir, db, "R05", R05_MACINV_V)
    db.set_components("R05", report.components_from_inventory(R05_MACINV_V), old)
    swapped = R05_MACINV_V.replace("222815805096", "NEWDISKSN99").replace(
        "Product Serial: FAKEPS0501", "Product Serial: FAKEPS0599")
    rel = "artifacts/R05/q2/inventory/macinv-v.txt"
    (order_dir / rel).parent.mkdir(parents=True)
    (order_dir / rel).write_text(swapped)
    _run(db, "R05", "qualify", "inventory", "pass", {}, {"macinv-v": rel})
    _inventory(order_dir, db, "L05", R05_MACINV_V)          # Done never ran for L05
    paths = report.write(order_dir, g, db)
    comps = csv.DictReader(open(paths["inventory"], newline=""))
    serials = {(r["pos"], r["serial"]) for r in comps}
    assert ("R05", "NEWDISKSN99") in serials and ("R05", "222815805096") not in serials
    assert ("L05", "222815805096") in serials
    dev = {r["pos"]: r for r in csv.DictReader(open(paths["devices"], newline=""))}
    assert dev["R05"]["serial"] == "FAKEPS0599"


def test_undeployed_fabric_macs_from_qualify_ha_path(rack):
    order_dir, g, db = rack
    rel = "artifacts/L37/q1/inventory/ip-d-link.txt"
    (order_dir / rel).parent.mkdir(parents=True)
    (order_dir / rel).write_text(
        "2: enp94s0f0np0: <BROADCAST,MULTICAST,UP> mtu 1500\n"
        "    link/ether 02:00:00:00:37:aa brd ff:ff:ff:ff:ff:ff\n"
        "3: enp94s0f1np1: <BROADCAST,MULTICAST,UP> mtu 1500\n"
        "    link/ether 02:00:00:00:37:bb brd ff:ff:ff:ff:ff:ff\n")
    _run(db, "L37", "qualify", "inventory", "pass", {}, {"ip-d-link": rel})
    _run(db, "L37", "qualify", "ha-path", "pass", {"links": [
        {"link": "pcie1", "seen": "B Ethernet12", "ifname": "enp94s0f1np1"},
        {"link": "pcie0", "seen": "A Ethernet12", "ifname": "enp94s0f0np0"}]})
    rows = {r["pos"]: r for r in csv.DictReader(open(report.write(order_dir, g, db)["devices"],
                                                      newline=""))}
    assert (rows["L37"]["fabric_a_mac"], rows["L37"]["fabric_b_mac"]) == (
        "02:00:00:00:37:aa", "02:00:00:00:37:bb")
    assert rows["C37"]["fabric_a_mac"] == ""                # no evidence: blank


def test_unread_backup_is_blank_never_the_active_version(rack):
    """A backup read that failed (unmeasured, its detail still carrying the active
    version the bin printed) leaves the cell blank; an IOM's ROM Version fills its row."""
    order_dir, g, db = rack
    _run(db, "L37", "firmware", "bmc-checked", "pass",
         {"slice": {"phase": "up_to_date", "current": "flax-onetree-1.1.4"}})
    _run(db, "L37", "firmware", "bmc-backup-checked", "unmeasured",
         {"backup_version": None, "active_version": "flax-onetree-1.1.4",
          "headline": "backup BMC not read: reader_canary_failed"})
    _run(db, "JBOD01.iom1", "firmware", "bmc-backup-checked", "pass",
         {"backup_version": "fbttn-v2.0"})
    _run(db, "JBOD01.iom1", "firmware", "bmc-checked", "pass",
         {"slice": {"phase": "unsupported", "versions": {"BMC Version": "fbttn-v2019.52.0",
                                                         "ROM Version": "fbttn-v2.0"}}})
    _inventory(order_dir, db, "L37", R05_MACINV_V)
    out = report.write(order_dir, g, db)
    rows = {r["pos"]: r for r in csv.DictReader(open(out["firmware"], newline=""))}
    assert (rows["L37"]["bmc_version"], rows["L37"]["bmc_rom_version"]) == (
        "flax-onetree-1.1.4", "")
    assert (rows["JBOD01.iom1"]["bmc_version"], rows["JBOD01.iom1"]["bmc_rom_version"]) == (
        "fbttn-v2019.52.0", "fbttn-v2.0")
    # operator 2026-10-31: the BMC images are firmware, not components
    kinds = {r["kind"] for r in csv.DictReader(open(out["inventory"], newline=""))}
    assert kinds == {"cpu", "dimm", "nic", "disk", "gpu"}


def _ports(path):
    return {r["pos"]: (r["mgmt_port"], r["fabric_a_port"], r["fabric_b_port"])
            for r in csv.DictReader(open(path, newline=""))}


def test_ports_are_front_panel_and_as_built(rack):
    """Ports as geometry writes them (never SONiC EthernetN); a device the live link
    monitor sees on one other port of a switch, and not on its geometry port, is
    reported where it is cabled."""
    order_dir, g, db = rack
    g.switches[0].site_port = {"switch": "turtle-gouda", "port": "Gi1/0/19"}
    db.set_macs("C37", host_mac="02:00:00:00:37:01", bmc_mac="02:00:00:00:37:0b", iom_macs=[])
    db.set_macs("JBOD01", host_mac=None, bmc_mac=None,
                iom_macs=["02:00:00:00:01:01", "02:00:00:00:01:02"])
    p = _ports(report.write(order_dir, g, db)["devices"])
    assert p["L37"] == ("C:7", "A:8/4", "B:8/4") and p["L05"] == ("C:5", "A:28/1", "B:28/1")
    assert p["JBOD01.iom1"] == ("C:13", "", "") and p["JBOD01"] == ("", "", "")
    assert p["_sw-C"] == ("turtle-gouda:Gi1/0/19", "C:1", "") and p["_sw-A"] == ("", "", "")
    # as built: L37 on wedge A port 9 lane 4, C37's mezz on C:10, the IOMs swapped
    snaps = {
        "A": {"at": 100.0, "fdb": [], "lldp": [
            {"local_iface": "Ethernet35", "remote_device": "rr101-l37", "remote_port": "x"},
            {"local_iface": "Ethernet31", "remote_device": "rr101-c37", "remote_port": "y"}]},
        "C": {"at": 100.0, "lldp": [], "fdb": [
            {"iface": "Ethernet9", "mac": "02:00:00:00:37:01"},
            {"iface": "Ethernet0", "mac": "02:00:00:00:37:0b"},        # the uplink: ignored
            {"iface": "Ethernet13", "mac": "02:00:00:00:01:01"},
            {"iface": "Ethernet12", "mac": "02:00:00:00:01:02"}]}}
    out = report.write(order_dir, g, db, snaps, 110.0)
    p = _ports(out["devices"])
    assert p["L37"] == ("C:7", "A:9/4", "B:8/4")       # B: no snapshot, geometry
    assert p["C37"] == ("C:10", "A:8/4", "B:8/4")      # A: seen on its geometry port
    assert p["JBOD01.iom1"][0] == "C:14" and p["JBOD01.iom2"][0] == "C:13"
    assert _ports(report.write(order_dir, g, db, snaps, 1000.0)["devices"])["L37"][1] == \
        "A:8/4"                                         # a stale snapshot proves nothing
    for f in out.values():
        assert "Ethernet" not in f.read_text(), f.name


def test_iom_side_kind_and_switch_identity(rack):
    order_dir, g, db = rack
    _run(db, "JBOD01.iom1", "discover", "serial", "fail",
         {"serial": "IOMSN0001", "side": "B", "headline": "Side B, want A"})
    _run(db, "JBOD01.iom2", "discover", "serial", "pass", {"serial": "IOMSN0002", "side": "A"})
    _run(db, "_sw-A", "discover", "serial", "pass", {"serial": "AIFAKEA"})
    _run(db, "_sw-A", "discover", "host-mac-seen", "pass", {"mac": "02000000a0a0"})
    _run(db, "_sw-A", "discover", "bmc-leased", "pass", {"rows": [["BMC MAC", "02000000a0b0"]]})
    _run(db, "_sw-C", "discover", "serial", "pass", {"serial": "CNFAKE0C"})
    _run(db, "_sw-C", "discover", "host-mac-seen", "pass", {"mac": "02000000c0c0"})
    rows = [r for r in csv.DictReader(open(report.write(order_dir, g, db)["devices"],
                                           newline=""))]
    by = {r["pos"]: r for r in rows}
    # IOMs by side: A first
    assert [r["pos"] for r in rows][-2:] == ["JBOD01.iom2", "JBOD01.iom1"]
    assert (by["JBOD01.iom2"]["kind"], by["JBOD01.iom2"]["serial"]) == (
        "iom A (left/front)", "IOMSN0002")
    assert by["JBOD01.iom1"]["kind"] == "iom B (right/back)"
    a, c = by["_sw-A"], by["_sw-C"]
    assert (a["serial"], a["host_mac"], a["bmc_mac"]) == (
        "AIFAKEA", "02:00:00:00:a0:a0", "02:00:00:00:a0:b0")
    assert (c["serial"], c["host_mac"], c["bmc_mac"]) == ("CNFAKE0C", "02:00:00:00:c0:c0", "")


def test_sales_order_in_csv_names_and_old_name_still_found(rack):
    """geometry "sales" rides beside the order id in every CSV name; a CSV Done wrote
    under the name without it is still found (served, csv_at) until rewritten."""
    order_dir, g, db = rack
    old = report.csv_names(g)
    report.write(order_dir, g, db)                      # written before "sales" was set
    g.sales = "S-ORD101031"
    new = report.csv_names(g)
    assert new == {k: f"{g.customer}-{g.order}-S-ORD101031-{k}.csv"
                   for k in ("devices", "firmware", "inventory")}
    assert all(report.CSV_RE.fullmatch(n) for n in new.values())
    assert report.csv_path(order_dir, g, "devices").name == old["devices"]
    paths = report.write(order_dir, g, db)
    assert {k: p.name for k, p in paths.items()} == new
    assert report.csv_path(order_dir, g, "devices").name == new["devices"]
    g.rack = "b"
    assert report.csv_names(g)["devices"] == f"{g.customer}-{g.order}-S-ORD101031-b-devices.csv"
