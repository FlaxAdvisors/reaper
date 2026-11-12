"""Task 2 (rackpost 5): the UI state carries no stale verdicts -- live pills, n/a vs
upstream skips, last-known IOM identity, the site link read from eth0 LLDP, and no
MLAG wording anywhere (old order.db rows are served in today's)."""
import dataclasses
import json
from types import SimpleNamespace

from flax_rack import linkmon
from flax_rack.fakes import FakeRungs, with_site_ports, with_wedge_ips
from flax_rack.geometry import load_geometry
from flax_rack.linkmon import device_names, evaluate
from flax_rack.orderdb import OrderDB
from flax_rack.server import Rack, rack_state
from flax_rungs.result import RungResult
from flax_rack.switches import IfStatus, LldpEntry
from tests.test_flax_rack_geometry import FIX

NOW = 1_800_000_000.0


def _rack(tmp_path, linkmon_=None):
    g = load_geometry(FIX)
    r = Rack.__new__(Rack)
    r.g, r.dir, r.stage, r.linkmon = g, tmp_path, None, linkmon_
    r.db = OrderDB(tmp_path / "order.db")
    r.db.init(g)
    r.lock = SimpleNamespace(locked=lambda: False, busy=lambda: False)
    return r


def _run(db, pos, stage, rung, verdict, detail=None):
    db.finish_run(db.start_run(pos, stage, rung), RungResult(verdict, detail or {}))


def _dev(s, pos):
    return next(d for d in s["devices"] if d["pos"] == pos)


def test_skip_reason_na_vs_upstream(tmp_path):
    r = _rack(tmp_path)
    _run(r.db, "JBOD01", "discover", "power-on", "skip", {"reason": "an IOM has no host"})
    _run(r.db, "_sw-A", "discover", "tftp-seen", "skip", {"reason": "a switch has no PXE live boot"})
    _run(r.db, "_sw-A", "firmware", "bios-checked", "skip",
         {"reason": "a switch has no host BIOS/NIC firmware manifest"})
    _run(r.db, "L37", "deploy", "ha-config", "skip", {"reason": "failed in phase install"})
    _run(r.db, "L37", "qualify", "inventory", "skip", {"reason": "agent-stages failed"})
    _run(r.db, "L37", "discover", "host-ssh", "skip", {"headline": "cancelled", "reason": "cancelled"})
    s = rack_state(r)
    jb, l37 = _dev(s, "JBOD01"), _dev(s, "L37")
    sw = next(x for x in s["switches"] if x["id"] == "A")
    assert jb["runs"]["power-on"]["skip_reason"] == "n/a"
    assert sw["runs"]["tftp-seen"]["skip_reason"] == sw["runs"]["bios-checked"]["skip_reason"] == "n/a"
    assert [l37["runs"][k]["skip_reason"] for k in ("ha-config", "inventory", "host-ssh")] == \
        ["upstream"] * 3
    assert "skip_reason" not in jb["runs"].get("serial", {})


def test_skip_reason_is_the_producers_na_flag(tmp_path):
    r = _rack(tmp_path)
    _run(r.db, "JBOD01", "discover", "power-on", "skip", {"reason": "reworded someday", "na": True})
    _run(r.db, "L37", "discover", "tftp-seen", "skip",
         {"reason": "an IOM has no host", "na": False})          # the flag wins over the text
    s = rack_state(r)
    assert _dev(s, "JBOD01")["runs"]["power-on"]["skip_reason"] == "n/a"
    assert _dev(s, "L37")["runs"]["tftp-seen"]["skip_reason"] == "upstream"


class _LM:
    """What rack_state reads from a LinkPoller."""
    interval_s = 3

    def __init__(self, snaps):
        self._s = snaps

    def snapshots(self):
        return self._s

    def switch_live(self, sid, now=None):
        return {"at": None, "ok": sid in self._s, "error": None}


def test_pills_read_live_links_only(tmp_path):
    import time
    up = {f"Ethernet{i}": IfStatus("up", "up", "25G") for i in range(128)}
    down = {**up, "Ethernet31": IfStatus("down", "up", "25G")}      # L37 pcie0 (A 8/4)
    lm = _LM({"A": {"at": time.time(), "ifaces": down, "lldp": [], "fdb": []}})
    r = _rack(tmp_path, lm)
    # an old Qualify ha-path that saw L37's fabric cables wrong: never a pill any more
    _run(r.db, "L37", "qualify", "ha-path", "fail",
         {"links": [{"link": "pcie0", "seen": "B Ethernet9", "expected": "A Ethernet31 x"}]})
    s = rack_state(r)
    l37 = _dev(s, "L37")
    assert "link_seen" not in l37
    assert l37["live_links"]["pcie0"]["state"] == "down"
    assert l37["live_links"]["pcie1"]["state"] == "unknown"           # B not read
    assert l37["presence"] == "unknown" and s["link_interval_s"] == 3
    assert _dev(s, "R05")["live_links"]["pcie0"]["state"] == "up"     # Ethernet109


def test_iom_keeps_last_known_mac_and_serial(tmp_path):
    r = _rack(tmp_path)
    _run(r.db, "JBOD01.iom2", "discover", "bmc-mac-seen", "pass",
         {"status": "found", "iom_mac": "98039b750997"})
    _run(r.db, "JBOD01.iom2", "discover", "serial", "pass", {"serial": "J49X2"})
    # the IOM goes silent: the next Discover finds no MAC and no serial
    _run(r.db, "JBOD01.iom2", "discover", "bmc-mac-seen", "fail", {"status": "missing"})
    _run(r.db, "JBOD01.iom2", "discover", "serial", "fail", {"error": "no answer"})
    io = _dev(rack_state(r), "JBOD01")["ioms"]
    assert io["iom2"]["mac"] == "98:03:9b:75:09:97" and io["iom2"]["serial"] == "J49X2"
    assert io["iom1"]["mac"] is None and io["iom1"]["serial"] is None      # never seen
    assert "98039b750997" in linkmon.device_macs(r.db)["JBOD01"]


def test_no_mlag_wording_anywhere_in_state(tmp_path):
    r = _rack(tmp_path)
    old = "active-backup (no MLAG peer link)"
    notes = ["whole-wedge failover: ...", "single-node failover needs an A↔B interconnect "
             "(MLAG peer link)", f"HA mode delivered: {old}"]
    for pos in ("L37", "C37"):
        _run(r.db, pos, "deploy", "ha-config", "pass", {"ha_mode": old})
    _run(r.db, "L37", "deploy", "ha-test", "pass", {"ha_mode": old, "notes": notes})
    _run(r.db, "C37", "deploy", "ha-test", "fail", {"ha_mode": old, "notes": notes})
    # a stale stored record: every host missing, old text
    r.db.set_ha_mode({"mode": old, "devices": ["R37"], "missing": ["L37", "C37"],
                      "at": "2026-10-29T10:00:00+00:00", "notes": notes[:2]})
    s = rack_state(r)
    txt = json.dumps(s, ensure_ascii=False)
    assert "MLAG" not in txt and "single-node" not in txt
    ha = s["deploy"]["ha_mode"]
    assert ha["mode"] == "active-backup" and ha["devices"] == ["L37"]     # from the runs
    assert "C37" in ha["missing"] and ha["at"] == "2026-10-29T10:00:00+00:00"
    assert _dev(s, "L37")["runs"]["ha-test"]["detail"]["notes"][-1] == \
        "HA mode delivered: active-backup"


def _site_g():
    g = load_geometry(FIX)
    sw = [dataclasses.replace(s, site_port={"switch": "turtle-gouda", "port": "Gi1/0/19"})
          if s.id == "C" else s for s in g.switches]
    return dataclasses.replace(g, switches=sw)


def _eth0(*nbs, at=NOW):
    return {"at": at, "ifaces": {"Ethernet0": IfStatus("up", "up", "1G")}, "fdb": [],
            "lldp": [LldpEntry("eth0", n, p, d) for n, p, d in nbs]}


def test_site_link_read_from_eth0_lldp():
    g = _site_g()
    site = lambda snaps: evaluate(g, snaps, {}, device_names(g), NOW)["_sw-C"]["site"]  # noqa: E731
    ok = site({"C": _eth0(("turtle-gouda.ehv.lab", "Gi1/0/19", "GigabitEthernet1/0/19"))})
    assert ok["state"] == "ok" and ok["iface"] == "eth0" and "turtle-gouda" in ok["text"]
    # the long ifname in the port id still matches the geometry's short one
    assert site({"C": _eth0(("turtle-gouda", "GigabitEthernet1/0/19", ""))})["state"] == "ok"
    wrong = site({"C": _eth0(("turtle-gouda.ehv.lab", "Gi1/0/20", "GigabitEthernet1/0/20"))})
    assert wrong["state"] == "wrong" and "Gi1/0/20" in wrong["text"]
    assert site({"C": _eth0(("turtle-edam.ehv.lab", "Gi1/0/19", ""))})["state"] == "wrong"
    assert site({"C": _eth0()})["state"] == "up"                      # answers, no eth0 LLDP
    assert site({"C": None})["state"] == "unknown"                    # did not answer
    assert site({})["state"] == "unknown"
    assert site({"C": _eth0(("turtle-gouda", "Gi1/0/19", ""), at=NOW - 200)})["state"] == "unknown"


def test_fake_cable_pull_turns_the_pill_red():
    g = with_wedge_ips(with_site_ports(load_geometry(FIX)))
    fr = FakeRungs(g)
    fr.boots = {"L37": {"kernel": 0}}
    fr.t = 1
    a = next(s for s in g.switches if s.id == "A")

    def pcie0():
        snap = {**fr.read_live(a.mgmt_ip, "sonic", {}).detail, "at": NOW}
        return evaluate(g, {"A": snap}, {}, device_names(g), NOW)["L37"]["pcie0"]["state"]
    assert pcie0() == "ok"
    fr.pulled = {("A", "Ethernet31")}
    assert pcie0() == "down"
    c = next(s for s in g.switches if s.id == "C")
    snap = {**fr.read_live(c.mgmt_ip, "sonic", {}).detail, "at": NOW}
    assert evaluate(g, {"C": snap}, {}, device_names(g), NOW)["_sw-C"]["site"]["state"] == "ok"


def test_fio_row_from_before_the_summary_gets_it_from_its_artifact(tmp_path):
    from flax_rack.fakes import _ALLDISKS, _FIO
    r = _rack(tmp_path)
    for rung, name, text in (("inventory", "alldisks", _ALLDISKS), ("fio", "fio", _FIO)):
        p = tmp_path / "artifacts" / "L37" / "q1" / rung / f"{name}.txt"
        p.parent.mkdir(parents=True)
        p.write_text(text)
        r.db.finish_run(r.db.start_run("L37", "qualify", rung), RungResult(
            "pass", {"stage": rung, "summary": {}}, {name: str(p.relative_to(tmp_path))}))
    f = _dev(rack_state(r), "L37")["runs"]["fio"]["detail"]
    assert f["headline"].startswith("nvme0n1 812k IOPS") and len(f["table"]["rows"]) == 6
    assert "SAMSUNG" in f["table"]["rows"][0][1]
