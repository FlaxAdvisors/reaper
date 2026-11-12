"""flax_rack.devview: drawer data (identity, versions, switch port tables) from runs."""
from flax_rack.devview import endpoint, firmware, identity, mgmt_ports, switch_view, tor_ports
from flax_rack.geometry import load_geometry
from tests.test_flax_rack_geometry import FIX

G = load_geometry(FIX)
SW = {s.id: s for s in G.switches}


def run(verdict="pass", status="done", arts=None, **detail):
    return {"status": status, "verdict": verdict, "detail": detail, "artifacts": arts or {}}


def test_identity_none_until_serial_read():
    assert identity({}) is None
    assert identity({"serial": run(status="running")}) is None
    i = identity({"serial": run(serial="J49X", serial_field="Board Serial", part_number="33S",
                                headline="SN J49X", rows=[["Board Serial", "J49X"]],
                                arts={"fruid-util-iom.txt": "artifacts/x"})})
    assert (i["serial"], i["ok"], i["serial_field"], i["part_number"]) == ("J49X", True, "Board Serial", "33S")
    assert i["rows"] == [["Board Serial", "J49X"]]
    # a failed read keeps what it saw (the drawer marks it), never hides the fault
    assert identity({"serial": run("fail", serial="A", headline="serials differ")})["ok"] is False


def test_firmware_openbmc_versions_and_tp_parts():
    ob = firmware({"bmc-checked": run(phase="unsupported", headline="BMC v", slice={
        "phase": "unsupported", "versions": {"OpenBMC": "wedge100-v29", "CPLD": "6.101"},
        "current": "wedge100-v29", "not_present": {}})})
    assert ob["versions"] == {"OpenBMC": "wedge100-v29", "CPLD": "6.101"} and ob["parts"] == []
    tp = firmware({
        "bmc-checked": run(phase="needs_update", slice={"phase": "needs_update",
                           "current_version": "1.1.3", "target_version": "1.1.4"}),
        "bios-checked": run(slice={"phase": "up_to_date", "current": "P26F", "target": "P26F"}),
        "bmc-backup-checked": run("unmeasured", reason="backup BMC not read: canary", backup_version=None),
        "mlx-checked": run(slice={"devices": [
            {"pci": "5e:00.0", "current": "14.32", "target": "14.32", "phase": "up_to_date",
             "psid": "MT_2420110034"},
            {"pci": "5e:00.1", "port": "mezz enp94s0f1", "current": "14.32", "target": "14.32",
             "phase": "up_to_date", "psid": "MT_2420110034"}]})})
    # the drawer's firmware block: BIOS, BMC active, BMC backup, NIC per port with PSID
    assert [(p["part"], p["current"], p["target"]) for p in tp["parts"]] == [
        ("BIOS", "P26F", "P26F"), ("BMC active", "1.1.3", "1.1.4"), ("BMC backup", None, None),
        ("NIC 5e:00.0", "14.32", "14.32"), ("NIC mezz enp94s0f1", "14.32", "14.32")]
    assert tp["parts"][2]["why"] == "backup BMC not read: canary"     # never the active version
    assert tp["parts"][3]["psid"] == "MT_2420110034"
    # an IOM: BMC + ROM out of its fw-util read; the rest stays in versions
    iom = firmware({"bmc-checked": run(slice={"phase": "unsupported", "versions": {
        "BMC Version": "fbttn-v2019.52.0", "ROM Version": "fbttn-v2.0", "TPM Version": "133.33"}}),
        "bmc-backup-checked": run(backup_version="fbttn-v2.0")})
    assert [(p["part"], p["current"]) for p in iom["parts"]] == [
        ("BMC", "fbttn-v2019.52.0"), ("ROM", "fbttn-v2.0")]
    assert iom["versions"] == {"TPM Version": "133.33"}
    assert firmware({}) is None


def test_endpoint_mac_and_lease():
    e = endpoint({"bmc-mac-seen": run(mac="02fc27000002"), "bmc-leased": run(ip="172.26.0.53")},
                 "bmc-mac-seen", "bmc-leased")
    assert e == {"mac": "02:fc:27:00:00:02", "ip": "172.26.0.53"}
    assert endpoint({}, "bmc-mac-seen", "bmc-leased") == {"mac": None, "ip": None}


def test_mgmt_ports_map_fdb_to_geometry_and_link():
    raw = {("_rack", "switch-fdb"): run(fdb=[
               {"vlan": 1, "mac": "0c42a1e16b50", "iface": "Ethernet6"},
               {"vlan": 1, "mac": "0c42a1e16b52", "iface": "Ethernet6"},
               {"vlan": 1, "mac": "02faffff0001", "iface": "Ethernet1"},
               {"vlan": 1, "mac": "aaaaaaaaaaaa", "iface": "PortChannel1"}]),
           ("_rack", "slot-match"): run(unexpected=[{"port": 2, "mac": "02faffff0001"}]),
           ("L37", "switchportlink"): run(rows=[["oper", "up"]]),
           ("JBOD01.iom2", "switchportlink"): run("fail", rows=[["oper", "down"]])}
    rows = {r["port"]: r for r in mgmt_ports(G, SW["C"], raw)}
    l37 = G.device("L37").links["mezz0"].port
    assert rows[str(l37)]["seen"] == ["0c:42:a1:e1:6b:50", "0c:42:a1:e1:6b:52"]
    assert rows[str(l37)]["devices"] == [{"pos": "L37", "link": "mezz0", "sel": "L37"}]
    assert rows[str(l37)]["oper"] == "up"
    iom2 = rows[str(G.device("JBOD01").links["iom2"].port)]
    assert iom2["devices"][0]["sel"] == "JBOD01.iom2" and iom2["oper"] == "down" and iom2["seen"] == []
    assert rows["2"]["note"] == "unexpected MAC"
    for p in SW["C"].uplink_ports:
        assert rows[str(p)]["note"] == "uplink"
    assert all(r["iface"].startswith("Ethernet") for r in rows.values())   # PortChannel dropped


def test_tor_ports_lanes_and_lldp_neighbours():
    l05 = G.device("L05").links["pcie0"]                     # A:28/1
    from flax_rack.switches import wedge_iface
    ifn = wedge_iface(l05.port, l05.lane)
    raw = {("_rack", "lldp-A"): run(lldp=[
        {"local_iface": ifn, "remote_device": "localhost", "remote_port": "02:fb", "remote_descr": "ens3f0np0"},
        {"local_iface": "Ethernet120", "remote_device": "stray", "remote_port": "x", "remote_descr": ""}])}
    rows = tor_ports(G, SW["A"], raw)
    r = next(x for x in rows if x["iface"] == ifn)
    assert r["port"] == "28/1" and r["devices"][0]["pos"] == "L05" and r["seen"] == ["localhost ens3f0np0"]
    stray = next(x for x in rows if x["iface"] == "Ethernet120")
    assert stray["devices"] == [] and stray["note"].startswith("neighbour") and stray["seen"] == ["stray x"]
    nums = [int(x["iface"][8:]) for x in rows]
    assert nums == sorted(nums)


def test_switch_view_dell_has_no_bmc():
    v = switch_view(G, SW["C"], {"host-mac-seen": run(mac="02fc29000001"),
                                 "host-leased": run(ip="172.26.0.50")}, {})
    assert v["bmc"] is None and v["sonic"] == {"mac": "02:fc:29:00:00:01", "ip": "172.26.0.50"}
    assert v["serial"] is None and v["ports_from"].startswith("FDB")
    w = switch_view(G, SW["A"], {}, {})
    assert w["bmc"] == {"mac": None, "ip": None} and w["ports_from"].startswith("geometry")


# ---------------------------------------------------------------- Discover timeline
import time as _time
from datetime import datetime as _dt, timezone as _tz

from flax_rack.devview import attach_timeline, discover_timeline


def _at(hms):
    h, m, s = map(int, hms.split(":"))
    return _time.mktime((2026, 11, 4, h, m, s, 0, 0, -1))


def _iso(hms):
    return _dt.fromtimestamp(_at(hms), _tz.utc).isoformat(timespec="seconds")


def drun(verdict="pass", fin=None, rid=0, **detail):
    return {"id": rid, "stage": "discover", "status": "done", "verdict": verdict,
            "detail": {"rows": [], **detail}, "artifacts": {},
            "finished_at": _iso(fin) if fin else None}


def _ladder():
    return {"serial": drun(fin="15:34:40", rid=1),
            "power-on": drun(fin="15:34:55", rid=2, power_on_at=_at("15:34:51"),
                             power_action="cycle"),
            "tftp-seen": drun(fin="15:38:30", rid=3, seen_at=_at("15:38:29"),
                              rows=[["after power-on", "+218s"]]),
            "ipxe-seen": drun(fin="15:39:42", rid=4, seen_at=_at("15:39:41")),
            "host-leased": drun(fin="15:40:00", rid=5),
            "live-iso-seen": drun(fin="15:41:17", rid=6, seen_at=_at("15:41:16")),
            "qualify-thing": {**drun(fin="16:00:00", rid=9), "stage": "qualify"}}


def test_timeline_order_and_offsets():
    tl = discover_timeline(_ladder())
    assert tl["anchor"] == _at("15:34:51") and tl["anchor_kind"] == "power-on"
    assert [r[2] for r in tl["rows"]] == ["power-on", "TFTP request seen", "iPXE script fetched",
                                          "live ISO fetched", "Discover ended: pass at 15:41:17"]
    assert [r[:2] for r in tl["rows"][:4]] == [["15:34:51", "+0s"], ["15:38:29", "+3m 38s"],
                                               ["15:39:41", "+4m 50s"], ["15:41:16", "+6m 25s"]]
    assert tl["row_cls"] == ["", "", "", "", "now"]


def test_timeline_fault_row_and_rung_details():
    runs = _ladder()
    runs["live-iso-seen"] = drun("fail", fin="15:44:41", rid=6, error="no iso after 300s")
    runs["host-ssh"] = drun("skip", fin="15:44:41", rid=7, reason="live-iso-seen failed")
    attach_timeline(runs)
    t = runs["tftp-seen"]["detail"]["table"]
    assert t["cols"] == ["time", "since on", "event", ""]
    assert t["rows"][-2] == ["15:44:41", "+9m 50s", "✕ live-iso-seen", "no iso after 300s"]
    assert t["rows"][-1][2] == "Discover ended: fail at 15:44:41"
    assert t["row_cls"][-2:] == ["fault", "now"]
    rows = dict(map(tuple, runs["tftp-seen"]["detail"]["rows"]))
    assert rows["after power-on"] == "+3m 38s" and rows["budget"] == "600 s"
    assert dict(map(tuple, runs["ipxe-seen"]["detail"]["rows"]))["budget"] == "300 s"
    assert runs["host-ssh"]["detail"]["table"] is t                 # every ladder rung
    assert "table" not in runs["serial"]["detail"]                  # not a ladder rung
    assert "table" not in runs["qualify-thing"]["detail"]


def test_timeline_anchor_fallbacks():
    # power-on skipped: already in the live image -> anchored on the kernel boot
    runs = {"power-on": drun(fin="15:30:00", rid=1, already_live=True,
                             kernel_boot=_at("15:20:00")),
            "tftp-seen": drun(fin="15:30:01", rid=2, seen_at=_at("15:19:00")),
            "host-ssh": drun(fin="15:30:05", rid=3, seen_at=_at("15:30:04"))}
    tl = discover_timeline(runs)
    assert tl["anchor_kind"] == "kernel boot" and tl["rows"][0][:3] == ["15:20:00", "+0s",
                                                                        "power-on"]
    assert tl["rows"][1][:3] == ["15:19:00", "-1m 00s", "TFTP request seen"]  # before the boot
    assert tl["rows"][2][:3] == ["15:30:04", "+10m 04s", "host answered ssh"]
    # installed OS: markers skipped, kernel boot anchor, the skip row
    runs = {"power-on": drun(fin="15:30:00", rid=1, installed_os=True,
                             kernel_boot=_at("09:00:00")),
            "tftp-seen": drun("skip", fin="15:30:01", rid=2, na=True,
                              reason="up on its installed OS: no live boot to mark")}
    tl = discover_timeline(runs)
    assert tl["anchor_kind"] == "kernel boot"
    assert ["", "", "boot markers not observed",
            "up on its installed OS: no live boot to mark"] in tl["rows"]
    # neither (a row from before the epochs): absolute times, blank offsets, no anchor
    runs = {"power-on": drun(fin="15:30:00", rid=1, already_live=True),
            "host-pinged": drun(fin="15:31:02", rid=2),
            "bmc-ready": drun(fin="15:31:09", rid=3, seen_at=_at("15:31:08"))}
    tl = discover_timeline(runs)
    assert (tl["anchor"], tl["anchor_kind"]) == (None, None)
    assert [r[:3] for r in tl["rows"][:2]] == [["15:31:02", "", "host answered ping"],
                                               ["15:31:08", "", "BMC answered a data read"]]
    attach_timeline(runs)
    rows = dict(map(tuple, runs["bmc-ready"]["detail"]["rows"]))
    assert "after power-on" not in rows and rows["budget"] == "600 s"
    # an IOM roll-up (power-on n/a) has no timeline
    assert discover_timeline({"power-on": drun("skip", na=True, reason="an IOM has no host")}) \
        is None
    assert discover_timeline({}) is None
