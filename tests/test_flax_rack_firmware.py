"""rackpost Firmware = loose-node Post's 6 firmware steps per device, DETECT ONLY (plan 2b Task 5)."""
import inspect
import json
import re
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("flax_rack.firmware")
from flax_post import blades, stepinfo
from flax_post.biosd import service as bios_service
from flax_post.fwd import config as fwd_config
from flax_post.fwd import flasher
from flax_post.fwd.redfish import RedfishClient
from flax_post.nicd import driver as nic_driver
from flax_post.nicd import service as nic_service
from flax_rack import firmware as fw
from flax_rack.fakes import FakeRungs
from flax_rack.stages import run_discover
from flax_rungs import fwcheck, identity, liveboot
from flax_rungs.fwsteps import FW_STEPS
from flax_rungs.result import RungResult
from tests.test_flax_rack_discover_ladder import HOSTS, make
from tests.test_flax_rungs_identity import IOM, Runner

PHASES = ("up_to_date", "needs_update", "unsupported", "unreachable", "oem", "fault", "done",
          "checking", "unknown", None)


def frows(ctx, key, backup=False):
    """[(rung, verdict, state)] of key's Firmware rows, in recording order (Post's six
    only unless backup: + bmc-backup-checked)."""
    import sqlite3
    c = sqlite3.connect(ctx.db.path)
    rows = c.execute("SELECT rung, verdict, detail FROM run WHERE pos=? AND stage='firmware' "
                     "ORDER BY id", (key,)).fetchall()
    c.close()
    return [(r, v, (json.loads(d) or {}).get("state")) for r, v, d in rows
            if backup or r != fw.BACKUP_STEP]


def det(ctx, key, rung):
    return ctx.db.latest_runs()[(key, rung)]["detail"]


def discovered(tmp_path, **kw):
    ctx = make(tmp_path, **kw)
    run_discover(ctx)
    return ctx


# ---------------------------------------------------------------- parity with Post
def test_evidence_and_constants_are_posts():
    for sl in ({}, {"phase": "up_to_date", "current": "a", "target": "a"},
               {"phase": "fault", "current_version": "x", "target_version": "y",
                "fault_reason": "boom", "mode": "enforce"}):
        assert fwcheck._fw_rows(sl, None) == stepinfo._fw_rows(sl, None)
    assert fwcheck._PHASE_MEANING == stepinfo._PHASE_MEANING
    assert fwcheck.DMI_SCRIPT == bios_service.DMI_SCRIPT
    assert fwcheck.CONFIG_DIR == fwd_config.CONFIG_DIR


def test_phases_map_to_steps_exactly_like_post():
    for p in PHASES:
        a, b = fwcheck.steps("bmc", {"phase": p})
        assert (a.detail["state"], b.detail["state"]) == blades._FW_BMC.get(p, ("pending", "pending")), p
        a, b = fwcheck.steps("bios", {"phase": p})
        assert (a.detail["state"], b.detail["state"]) == blades._FW_BIOS.get(p, ("pending", "pending")), p
    for sl in ({"phase": "unreachable", "devices": []}, {"phase": "fault", "devices": []},
               {"phase": "up_to_date", "devices": []},
               {"phase": "needs_update", "devices": [{"phase": "needs_update", "current": "1"}]},
               {"phase": "unsupported", "devices": [{"phase": "unsupported", "current": "1"}]},
               {"phase": "checking", "devices": [{"phase": "up_to_date", "current": None}]}):
        a, b = fwcheck.steps("nic", sl)
        assert (a.detail["state"], b.detail["state"]) == blades._nic_steps({"fw_nic": sl}), sl
    # Post state -> verdict: done pass, fault/cur fail, pending skip
    a, b = fwcheck.steps("bmc", {"phase": "needs_update", "current": "x", "target": "y"})
    assert (a.verdict, b.verdict) == ("pass", "fail")
    # unreachable (nothing read): Post's states kept, both steps unmeasured
    a, b = fwcheck.steps("bmc", {"phase": "unreachable", "fault_reason": "BMC unreachable: t/o"})
    assert (a.verdict, b.verdict) == ("unmeasured", "unmeasured")
    assert (a.detail["state"], b.detail["state"]) == ("cur", "pending")
    assert a.detail["headline"] == "BMC unreachable: BMC unreachable: t/o"
    assert b.detail["headline"] == "not measured: BMC unreachable: BMC unreachable: t/o"
    for key, sl in (("bios", {"phase": "unreachable", "current": None}),
                    ("nic", {"phase": "unreachable", "devices": []})):
        a, b = fwcheck.steps(key, sl)
        assert (a.verdict, b.verdict) == ("unmeasured", "unmeasured"), key
    assert fwcheck.steps("nic", {"phase": "unreachable", "devices": []})[0].detail["headline"] \
        == "host unreachable: cards not queried"
    # a rackpost slice whose device answered but the read failed stays a fail
    a, b = fwcheck.steps("bmc", {"phase": "unreachable", "ssh_unreachable": False,
                                 "fault_reason": "fruid-util: rc=1"})
    assert (a.verdict, b.verdict) == ("fail", "skip")
    a, b = fwcheck.steps("bmc", {"phase": "unsupported", "current": "fbttn-v2019.52.0"})
    assert (a.verdict, b.verdict) == ("pass", "pass")
    a, b = fwcheck.steps("bios", {"phase": "fault", "fault_reason": "platform not in the manifest"})
    assert (a.verdict, b.verdict) == ("pass", "fail")


# ---------------------------------------------------------------- BMC: fwd's probe_one
class Client:
    """A RedfishClient stand-in answering probe_one's reads."""

    def __init__(self, version=None, product="Wiwynn Tioga Pass", root=(False, None, None)):
        self.version, self.product, self.root = version, product, root

    def get_product_name(self):
        return self.product, "ok"

    def get_fw_version(self, fw_id):
        return (self.version, "ok") if self.version else (None, "GET ... transport error")

    def get_redfish_root(self):
        return self.root


def _manifest():
    return FakeRungs.fw_manifests(SimpleNamespace(
        FW_BMC_TARGET="flax-onetree-1.1.4", FW_BIOS_TARGET="TPC_P26F",
        FW_NIC=("MT_2420110034", "14.32.1912")))


@pytest.mark.parametrize("client,phase", [
    (Client("flax-onetree-1.1.4"), "up_to_date"),
    (Client("flax-onetree-1.1.3"), "needs_update"),
    (Client(None), "unreachable"),
    (Client(None, root=(True, "1.8.0", "AMI Redfish Server")), "needs_update"),   # vendor stock
])
def test_tp_bmc_is_probe_one(client, phase):
    M = _manifest()
    sl, arts = fwcheck.tp_bmc("10.0.0.1", [("u", "p")], M.bmc, client_factory=lambda ip, c: client)
    post = {}
    assert flasher.probe_one("10.0.0.1", client, M.bmc, lambda port, **f: post.update(f)) == phase
    assert sl["phase"] == phase
    assert {k: sl[k] for k in post} == post                 # the row Post's fwd would write
    assert "get_product_name() -> " in arts["redfish.txt"]


def test_tp_bmc_passes_credentials_bmc_pairs_and_none_leak():
    seen = {}

    def factory(ip, creds):
        seen.update(ip=ip, creds=creds)
        return Client("flax-onetree-1.1.4")
    sl, arts = fwcheck.tp_bmc("10.0.0.1", [("bu", "secretpw")], _manifest().bmc,
                              client_factory=factory)
    assert seen["creds"] == [{"bmcuser": "bu", "bmcpass": "secretpw"}]
    assert "secretpw" not in json.dumps(sl) + arts["redfish.txt"]


def test_tp_bmc_over_the_real_redfish_client(monkeypatch):
    """fwd's own RedfishClient (transport faked at _request) behind the read-only proxy."""
    paths = []
    body = {"/redfish/v1/Systems": {"Members": [{"@odata.id": "/redfish/v1/Systems/system"}]},
            "/redfish/v1/Systems/system": {"Manufacturer": "Wiwynn", "Model": "Tioga Pass"},
            "/redfish/v1/UpdateService/FirmwareInventory/bmc_active": {"Version": "flax-onetree-1.1.4"}}

    def req(self, method, path, body_=None, content_type=None, timeout=5):
        paths.append((method, path))
        assert method == "GET"
        return 200, json.dumps(body[path]).encode(), "HTTP 200", None
    monkeypatch.setattr(RedfishClient, "_request", req)
    sl, _ = fwcheck.tp_bmc("10.0.0.1", [("u", "p")], _manifest().bmc)
    assert sl["phase"] == "up_to_date" and sl["current_version"] == "flax-onetree-1.1.4"
    assert {m for m, _ in paths} == {"GET"}


# ---------------------------------------------------------------- BIOS / NIC: biosd/nicd parity
_DMI_SYS = "System Information\n\tManufacturer: Wiwynn\n\tProduct Name: {p}\n"
_DMI_BIOS = "BIOS Information\n\tVendor: AMI\n\tVersion: {v}\n"


def _host(rc=0, product="SV7220G3", version="TPC_P26F", query=None):
    def run(script):
        if rc:
            return rc, ""
        if script.startswith("dmidecode -t system"):
            return 0, _DMI_SYS.format(p=product)
        if script.startswith("dmidecode -t bios"):
            return 0, _DMI_BIOS.format(v=version)
        if script == nic_driver.QUERY_SCRIPT:
            return 0, query or ""
        raise AssertionError(script)
    return run


def _card(pci, ver, psid, uefi="True(1)", sec="N/A"):
    return (f"===CARD {pci}===\nFW Version:            {ver}\nPSID:                  {psid}\n"
            f"Security Attributes:   {sec}\n===UEFI {pci}===\n  EXP_ROM_UEFI_x86_ENABLE  {uefi}\n")


@pytest.mark.parametrize("host", [
    _host(), _host(version="TPC_P26C"), _host(rc=255), _host(product="Leopard"),
    _host(version="null")])
def test_bios_is_biosd_probe(host):
    M = _manifest()
    sent = []

    def run(ip, user, pw, script, *, timeout=None, runner=None):
        sent.append(script)
        rc, out = host(script)
        return rc, out, ""
    sl, arts = fwcheck.bios("10.0.0.2", "flax", "p", M.bios, run=run)
    rows = []
    deps = SimpleNamespace(run=lambda ip, s: host(s), matcher=M.bios,
                           set_row=lambda port, **f: rows.append(f) or f)
    bios_service._probe_host(deps, None, {"port": "x", "host_ip": "10.0.0.2"})
    post = rows[0]
    for k in ("phase", "current", "target", "fault_reason"):
        assert sl[k] == post[k], k
    assert all(s.startswith("dmidecode -t ") for s in sent) and set(arts) <= {
        "dmidecode-system", "dmidecode-bios"}


@pytest.mark.parametrize("query", [
    "", _card("5e:00.0", "14.32.1912", "MT_2420110034"),
    _card("5e:00.0", "14.32.1912", "MT_2420110034", uefi="False(0)"),
    _card("5e:00.0", "14.20.1010", "MT_2420110034") + _card("af:00.0", "16.35.1012", "MT_0000000080"),
    _card("5e:00.0", "14.20.1010", "MT_2420110034", sec="secure-fw"),
    None])
def test_nic_is_nicd_classify_row(query):
    M = _manifest()
    host = _host(rc=255) if query is None else _host(query=query)
    sl, arts = fwcheck.nic("10.0.0.2", "flax", "p", M.nic,
                           run=lambda ip, u, p, s, **k: (*host(s), ""))
    deps = SimpleNamespace(run=lambda ip, s: host(s), matcher=M.nic)
    post = nic_service._classify_row(deps, {"host_ip": "10.0.0.2"})
    assert sl == {**post, **({"error": sl["error"]} if "error" in sl else {})}
    a, b = fwcheck.steps("nic", sl, arts)
    assert (a.detail["state"], b.detail["state"]) == blades._nic_steps({"fw_nic": post})
    assert list(arts) == ["mstflint-query"]


# ---------------------------------------------------------------- IOM fw-util -> rows
def test_iom_fw_util_parsed_to_rows_na_not_present():
    r = identity.iom("172.32.1.11", "u", "p", runner=Runner(IOM))
    sl, arts = fwcheck.report_only(r)
    assert sl["phase"] == "unsupported" and sl["current"] == "fbttn-v2019.52.0"
    chk, upd = fwcheck.steps("bmc", sl, arts)
    rows = dict(chk.detail["rows"])
    assert rows["BMC Version"] == "fbttn-v2019.52.0" and rows["ROM Version"] == "fbttn-v2.0"
    assert rows["TPM Version"] == "133.33" and rows["Fan Speed Controller Version"] == "fbttn-type5-mp-v3"
    assert rows["Mellanox NIC firmware version"] == "14.20.1202"
    assert rows["Expander Version"] == "0x1b0e0016"
    assert rows["SCC IOC Version"] == "not present (Server Power-off)"
    assert rows["BIOS Version"] == "not present (server is empty)"
    assert rows["MCU Version"] == "not present (NA)"
    assert rows["daemon phase"] == "unsupported" and "manifest" not in rows
    assert (chk.verdict, chk.detail["state"], upd.verdict, upd.detail["state"]) == \
        ("pass", "done", "pass", "done")
    assert upd.detail["headline"] == "report only: no firmware manifest for this BMC"
    assert set(chk.artifacts) == {"fruid-util-iom.txt", "fw-util-bmc.txt", "fw-util-all.txt",
                                  "check_pal_sku.txt"}
    # an IOM that does not answer is Post's unreachable (cur/pending), not unsupported
    sl, _ = fwcheck.report_only(identity.iom("ip", "u", "p", runner=Runner({}, rc={
        identity.FRUID_IOM: 255})))
    chk, upd = fwcheck.steps("bmc", sl)
    assert (chk.detail["state"], upd.detail["state"]) == ("cur", "pending")


# ---------------------------------------------------------------- the stage
def test_firmware_stage_six_steps_per_device(tmp_path):
    ctx = discovered(tmp_path)
    fw.run_firmware(ctx)
    for key in (*HOSTS, "JBOD01", "JBOD01.iom1", "JBOD01.iom2", "_sw-A", "_sw-B", "_sw-C"):
        assert [r for r, _, _ in frows(ctx, key, backup=True)] == list(fw.FIRMWARE_RUNGS), key
        for s in fw.FIRMWARE_RUNGS:
            d = det(ctx, key, s)
            assert d["headline"] and isinstance(d["rows"], list) and isinstance(d["notes"], list)
    # L05/C37 everything at target
    for pos in ("L05", "C37"):
        assert frows(ctx, pos) == [(s, "pass", "done") for s in FW_STEPS], pos
    # R05: BMC off target -> needs_update (Post: done/cur), never flashed
    assert frows(ctx, "R05")[:2] == [("bmc-checked", "pass", "done"), ("bmc-updated", "fail", "cur")]
    d = det(ctx, "R05", "bmc-updated")
    assert d["phase"] == "needs_update" and "triage flashes" in d["headline"]
    assert dict(d["rows"])["current"] == "flax-onetree-1.1.3"
    assert dict(d["rows"])["target"] == "flax-onetree-1.1.4"
    assert stepinfo._PHASE_MEANING["needs_update"] in d["notes"]
    # R37's BIOS, L37's NIC UEFI ROM
    assert frows(ctx, "R37")[3] == ("bios-updated", "fail", "cur")
    assert det(ctx, "R37", "bios-checked")["headline"] == "BIOS TPC_P26C"
    assert frows(ctx, "L37")[5] == ("mlx-updated", "fail", "cur")
    assert "UEFI ROM off" in det(ctx, "L37", "mlx-updated")["headline"]
    # artifacts = raw outputs, under f<run>
    lr = ctx.db.latest_runs()
    a = lr[("L37", "bios-checked")]["artifacts"]
    assert set(a) == {"dmidecode-system", "dmidecode-bios"} and a["dmidecode-bios"].startswith(
        "artifacts/L37/f")
    assert "Version: TPC_P26F" in (tmp_path / a["dmidecode-bios"]).read_text()
    assert "mstflint-query" in lr[("L37", "mlx-checked")]["artifacts"]
    assert "redfish" in lr[("R05", "bmc-checked")]["artifacts"]
    ev = ctx.db.events(1)[0]["text"]
    assert ev.startswith("firmware done (detect only); needs update:") and "R05" in ev


def test_jbod_iom_bmc_checked_report_only_bios_mlx_skip(tmp_path):
    ctx = discovered(tmp_path)
    fw.run_firmware(ctx)
    for n in (1, 2):
        key = f"JBOD01.iom{n}"
        assert frows(ctx, key)[:2] == [("bmc-checked", "pass", "done"), ("bmc-updated", "pass", "done")]
        assert [v for _, v, _ in frows(ctx, key)[2:]] == ["skip"] * 4
        assert det(ctx, key, "bios-checked")["reason"] == "an IOM has no host"
        rows = dict(det(ctx, key, "bmc-checked")["rows"])
        assert rows["BMC Version"] == "fbttn-v2019.52.0" and rows["daemon phase"] == "unsupported"
        assert "fw-util-bmc" in ctx.db.latest_runs()[(key, "bmc-checked")]["artifacts"]
    # the device's rows roll its IOMs up (with Post's worst state)
    assert frows(ctx, "JBOD01")[:2] == [("bmc-checked", "pass", "done"), ("bmc-updated", "pass", "done")]
    assert [v for _, v, _ in frows(ctx, "JBOD01")[2:]] == ["skip"] * 4


def test_jbod_one_iom_unreachable_rolls_up_cur(tmp_path):
    ctx = discovered(tmp_path)
    real = ctx.rungs.iom
    ctx.rungs.iom = lambda ip, u, p, runner=None: (
        RungResult("unmeasured", {"error": "fruid-util: rc=255 no route",
                                  "headline": f"IOM {ip} did not answer ssh"})
        if ip.endswith(".12") else real(ip, u, p, runner=runner))
    fw.run_firmware(ctx)
    # the IOM did not answer ssh: not measured (grey), never a fault; the device rolls up
    assert frows(ctx, "JBOD01.iom2")[:2] == [("bmc-checked", "unmeasured", "cur"),
                                             ("bmc-updated", "unmeasured", "pending")]
    assert frows(ctx, "JBOD01")[0] == ("bmc-checked", "unmeasured", "cur")
    assert det(ctx, "JBOD01", "bmc-checked")["headline"].startswith("iom2: ")
    assert "not measured: JBOD01" in ctx.db.events(5)[0]["text"]


def test_jbod_iom_answering_but_failing_stays_fail(tmp_path):
    ctx = discovered(tmp_path)
    real = ctx.rungs.iom
    ctx.rungs.iom = lambda ip, u, p, runner=None: (
        RungResult("fail", {"error": "fruid-util: rc=1 not found"}) if ip.endswith(".12")
        else real(ip, u, p, runner=runner))
    fw.run_firmware(ctx)
    assert frows(ctx, "JBOD01.iom2")[:2] == [("bmc-checked", "fail", "cur"),
                                             ("bmc-updated", "skip", "pending")]
    assert frows(ctx, "JBOD01")[0] == ("bmc-checked", "fail", "cur")


def test_switches_wedge_bmc_versions_dell_no_bmc(tmp_path):
    ctx = discovered(tmp_path)
    fw.run_firmware(ctx)
    for sw in ("_sw-A", "_sw-B"):
        assert frows(ctx, sw)[:2] == [("bmc-checked", "pass", "done"), ("bmc-updated", "pass", "done")]
        assert dict(det(ctx, sw, "bmc-checked")["rows"])["OpenBMC"] == "wedge100-v29"
        assert det(ctx, sw, "bios-checked")["reason"] == "a switch has no host BIOS/NIC firmware manifest"
    assert [v for _, v, _ in frows(ctx, "_sw-C")] == ["skip"] * 6
    assert det(ctx, "_sw-C", "bmc-checked")["reason"] == "no BMC (dell-n3248te endpoints: sonic)"
    # the wedge BMC was read at the IP Discover's bmc-leased found
    ip = det(ctx, "_sw-A", "bmc-leased")["ip"]
    assert any(n.startswith(f"wedge BMC at {ip}:") for n in det(ctx, "_sw-A", "bmc-checked")["notes"])


def test_wedge_without_discover_is_not_logged_into(tmp_path):
    ctx = make(tmp_path)
    seen = []
    ctx.rungs.wedge_bmc = lambda ip, *a, **k: seen.append(ip)
    fw.run_firmware(ctx, positions=["A"])
    assert [v for _, v, _ in frows(ctx, "_sw-A")] == ["skip"] * 6
    assert det(ctx, "_sw-A", "bmc-checked")["reason"] == \
        "discover first: bmc-leased not run, bmc-pinged not run"
    assert seen == []


def test_host_not_in_live_image_skips_bios_and_mlx(tmp_path):
    ctx = make(tmp_path)                 # no Discover: host-ssh never passed
    fw.run_firmware(ctx, positions=["L37"])
    got = frows(ctx, "L37")
    assert [v for _, v, _ in got] == ["skip"] * 6
    assert det(ctx, "L37", "bmc-checked")["reason"].startswith("discover first: bmc-leased not run")
    assert not [c for c in ctx.rungs.calls if c[0] == "fw_bmc"]
    assert det(ctx, "L37", "bios-checked")["reason"].startswith(
        "host not in live image (Discover host-ssh: not run)")
    assert not [c for c in ctx.rungs.calls if c[0] == "host_run" and c[1] == "L37"]


def test_live_image_of_another_host_skips(tmp_path):
    ctx = discovered(tmp_path)
    ctx.rungs.wrong_host.add("R37")
    n0 = len(ctx.rungs.calls)
    fw.run_firmware(ctx, positions=["R37"])
    assert [v for _, v, _ in frows(ctx, "R37")[2:]] == ["skip"] * 4
    assert "host not in live image of this device (BOOTIF 02faffff0099" in \
        det(ctx, "R37", "mlx-checked")["reason"]
    # only the liveness probe went to R37: no dmidecode / mstflint on a foreign host
    sent = [c[2] for c in ctx.rungs.calls[n0:] if c[0] == "host_run" and c[1] == "R37"]
    assert sent == [liveboot._PROBE.split("\n")[0][:40]]


def test_live_host_gone_is_posts_unreachable(tmp_path):
    ctx = discovered(tmp_path)
    ctx.rungs.boots.pop("R05")           # the host stopped answering ssh after Discover
    fw.run_firmware(ctx, positions=["R05"])
    got = frows(ctx, "R05")
    assert got[2:] == [("bios-checked", "unmeasured", "cur"),
                       ("bios-updated", "unmeasured", "pending"),
                       ("mlx-checked", "unmeasured", "cur"),
                       ("mlx-updated", "unmeasured", "pending")]
    assert "ssh 172.32.5.101 rc=255" in det(ctx, "R05", "bios-checked")["headline"]
    assert det(ctx, "R05", "mlx-checked")["headline"] == "host unreachable: cards not queried"


def test_bmc_down_is_unreachable_not_fault(tmp_path):
    ctx = discovered(tmp_path)
    ctx.rungs.down.add("172.32.37.11")
    fw.run_firmware(ctx, positions=["L37"])
    assert frows(ctx, "L37")[:2] == [("bmc-checked", "unmeasured", "cur"),
                                     ("bmc-updated", "unmeasured", "pending")]
    d = det(ctx, "L37", "bmc-checked")
    assert d["phase"] == "unreachable" and "BMC unreachable" in d["headline"]
    assert dict(d["rows"])["target"] == "flax-onetree-1.1.4"


def test_positions_and_unknown_position(tmp_path):
    ctx = discovered(tmp_path)
    fw.run_firmware(ctx, positions=["L05", "_sw-B"])
    keys = {k for k, r in ctx.db.latest_runs() if r in FW_STEPS}
    assert keys == {"L05", "_sw-B"}
    with pytest.raises(ValueError, match="not a device or switch of this rack: ZZ9"):
        fw.run_firmware(ctx, positions=["ZZ9"])


def test_per_device_parallel_at_most_eight(tmp_path, monkeypatch):
    ctx = discovered(tmp_path)
    monkeypatch.setattr(fw, "MAX_PARALLEL", 2)
    live, peak, mu = [0], [0], threading.Lock()
    real = ctx.rungs.fw_bmc

    def slow(*a, **k):
        with mu:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with mu:
            live[0] -= 1
        return real(*a, **k)
    ctx.rungs.fw_bmc = slow
    fw.run_firmware(ctx)
    assert peak[0] == 2
    assert fw.MAX_PARALLEL == 2 and inspect.getsource(fw).count("MAX_PARALLEL = 8") == 1


def test_a_raising_reader_is_a_fault_row_not_a_crash(tmp_path):
    ctx = discovered(tmp_path)
    ctx.rungs.fw_bmc = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
    fw.run_firmware(ctx, positions=["L05"])
    assert frows(ctx, "L05")[:2] == [("bmc-checked", "fail", "fault"), ("bmc-updated", "skip", None)]
    assert det(ctx, "L05", "bmc-checked")["error"] == "OSError: boom"


def test_closed_order_refused(tmp_path):
    ctx = make(tmp_path)
    ctx.db.set_closed()
    with pytest.raises(RuntimeError, match="closed"):
        fw.run_firmware(ctx)


# ---------------------------------------------------------------- server + CLI
def test_firmware_route_no_confirm_and_positions_checked(tmp_path):
    from tests.test_flax_rack_server import Factory, _order_dir, _start, _wait_idle, get_json, post
    _order_dir(tmp_path)
    s = _start(tmp_path, Factory())
    try:
        assert post(s, "/rr101/api/stage/firmware", {"positions": ["NOPE"]}).status == 400
        assert post(s, "/rr101/api/stage/firmware", {"positions": "L37"}).status == 400
        r = post(s, "/rr101/api/stage/firmware", {"positions": ["L37"]})
        assert r.status == 202 and json.loads(r.body) == {"started": "firmware", "positions": ["L37"]}
        _wait_idle(s)
        st = get_json(s, "/rr101/api/state")
        assert st["firmware_rungs"] == list(fw.FIRMWARE_RUNGS)
        l37 = next(d for d in st["devices"] if d["pos"] == "L37")
        assert l37["runs"]["bmc-checked"]["detail"]["reason"].startswith("discover first")
        assert post(s, "/rr101/api/stage/firmware", {}).status == 202
        _wait_idle(s)
        jb = next(d for d in get_json(s, "/rr101/api/state")["devices"] if d["pos"] == "JBOD01")
        assert "bmc-checked" in jb["endpoint_runs"]["iom1"]
        sw = next(x for x in get_json(s, "/rr101/api/state")["switches"] if x["id"] == "C")
        assert sw["runs"]["bmc-checked"]["verdict"] == "skip"
    finally:
        s.shutdown(); s.server_close()


def test_firmware_cli(tmp_path, monkeypatch, capsys):
    import flax_rack.server as server
    from flax_rack.__main__ import main
    from tests.test_flax_rack_server import Factory, _order_dir
    monkeypatch.setattr(server, "default_context_factory", Factory())
    monkeypatch.chdir(_order_dir(tmp_path))
    assert main(["firmware", "--pos", "L37", "A"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"L37\s+bmc-checked\s+\w+", out) and "_sw-A" in out
    assert "JBOD01" not in out
    with pytest.raises(SystemExit, match="not a device or switch"):
        main(["firmware", "--pos", "ZZ9"])


def test_ui_wires_firmware():
    src = open(fw.__file__.rsplit("/", 1)[0] + "/web/rackpost.js").read()
    html = open(fw.__file__.rsplit("/", 1)[0] + "/web/index.html").read()
    assert "api/stage/firmware" in src and "firmware_old_rungs" in src and "endpointFw" in src
    assert "detail || {}).state" in src
    assert "this.firmware()" in src and "stageGo(b.key)" in html and "firmware(selDev?.pos)" in html


# ---------------------------------------------------------------- fix wave: discover gate, cancel
def _fail_step(ctx, key, step):
    rid = ctx.db.start_run(key, "discover", step)
    ctx.db.finish_run(rid, RungResult("fail", {"headline": "no ping", "error": "no ping"}))


def test_tp_bmc_read_needs_discovers_bmc_pinged(tmp_path):
    ctx = discovered(tmp_path)
    _fail_step(ctx, "L37", "bmc-pinged")
    fw.run_firmware(ctx, positions=["L37", "C37"])
    assert det(ctx, "L37", "bmc-checked")["reason"] == "discover first: bmc-pinged fail"
    assert ("fw_bmc", "L37") not in ctx.rungs.calls and ("fw_bmc", "C37") in ctx.rungs.calls
    assert frows(ctx, "C37")[0] == ("bmc-checked", "pass", "done")


def test_iom_read_needs_discovers_bmc_leased(tmp_path):
    ctx = discovered(tmp_path)
    _fail_step(ctx, "JBOD01.iom1", "bmc-leased")
    seen, real = [], ctx.rungs.iom
    ctx.rungs.iom = lambda ip, *a, **k: (seen.append(ip), real(ip, *a, **k))[1]
    fw.run_firmware(ctx, positions=["JBOD01"])
    assert det(ctx, "JBOD01.iom1", "bmc-checked")["reason"] == "discover first: bmc-leased fail"
    assert len(seen) == 1 and seen[0] == det(ctx, "JBOD01.iom2", "bmc-leased")["ip"]


def test_cancel_skips_the_remaining_reads(tmp_path):
    ctx = discovered(tmp_path)
    real = ctx.rungs.fw_bmc

    def bmc(*a, **k):                    # Cancel pressed during the BMC read
        ctx.cancel.set()
        return real(*a, **k)
    ctx.rungs.fw_bmc = bmc
    n = len([c for c in ctx.rungs.calls if c[0] == "host_run"])
    fw.run_firmware(ctx, positions=["L37"])
    got = frows(ctx, "L37")
    assert got[0][:2] == ("bmc-checked", "pass")
    assert [v for _, v, _ in got[2:]] == ["skip"] * 4
    assert det(ctx, "L37", "bios-checked")["reason"] == "cancelled"
    assert len([c for c in ctx.rungs.calls if c[0] == "host_run"]) == n
    assert any(e["text"].startswith("firmware cancelled") for e in ctx.db.events(30))


# ---------------------------------------------------------------- Plan 6: backup BMC + installed OS
LIVE_BACKUP = ('{"backup_version":"flax-onetree-1.1.4","active_version":"flax-onetree-1.1.4",'
               '"net_file":"present","net_pin":"none","net_mtime":1793119281}\n')


def _bin(rc, out):
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, rc, out, "")
    return run, seen


def test_bmc_backup_reads_the_version_line_only():
    run, seen = _bin(0, LIVE_BACKUP)
    r = fwcheck.bmc_backup("172.32.37.61", runner=run)
    assert seen == [["/opt/flax/bin/bmc-backup-flash", "version", "172.32.37.61"]]
    assert r.verdict == "pass" and r.detail["backup_version"] == "flax-onetree-1.1.4"
    assert r.detail["headline"] == "backup BMC flax-onetree-1.1.4"
    off = LIVE_BACKUP.replace('"backup_version":"flax-onetree-1.1.4"',
                              '"backup_version":"flax-onetree-1.1.3"')
    assert fwcheck.bmc_backup("x", runner=_bin(0, off)[0]).detail["headline"] == \
        "backup BMC flax-onetree-1.1.3 (active flax-onetree-1.1.4)"


@pytest.mark.parametrize("rc,out", [
    (1, '{"backup_version":null,"active_version":"flax-onetree-1.1.4","net_file":"present",'
        '"net_pin":"none","error":"reader_canary_failed"}\n'),
    (1, ""), (124, "")])
def test_bmc_backup_unread_is_unmeasured_without_a_version(rc, out):
    r = fwcheck.bmc_backup("172.32.37.61", runner=_bin(rc, out)[0])
    assert r.verdict == "unmeasured" and r.detail["backup_version"] is None
    assert r.detail["headline"].startswith("backup BMC not read")
    if out:
        assert "reader_canary_failed" in r.detail["headline"]


def test_backup_step_tp_iom_switch(tmp_path):
    from flax_rack import report
    ctx = discovered(tmp_path)
    ctx.rungs.fw_backup_err = {"L37": "chip_absent"}
    fw.run_firmware(ctx)
    bk = fw.BACKUP_STEP
    assert frows(ctx, "L05", backup=True)[2][:2] == (bk, "pass")
    assert det(ctx, "L05", bk)["backup_version"] == "flax-onetree-1.1.4"
    # R05: active 1.1.3, backup at target: the headline says they differ
    assert det(ctx, "R05", bk)["headline"] == \
        "backup BMC flax-onetree-1.1.4 (active flax-onetree-1.1.3)"
    assert frows(ctx, "L37", backup=True)[2][:2] == (bk, "unmeasured")
    for n in (1, 2):
        assert det(ctx, f"JBOD01.iom{n}", bk)["backup_version"] == "fbttn-v2.0"
    assert frows(ctx, "JBOD01", backup=True)[2][:2] == (bk, "pass")
    assert [v for r, v, _ in frows(ctx, "_sw-A", backup=True) if r == bk] == ["skip"]
    rom = {r["pos"]: (r["bmc_rom_version"], r["bmc_version"])
           for r in report.customer_rows(ctx.dir, ctx.g, ctx.db)}
    assert rom["L05"][0] == "flax-onetree-1.1.4"
    assert rom["JBOD01.iom1"][0] == "fbttn-v2.0"
    # unread backup: blank, never the active version the bin also printed
    assert rom["L37"][0] == "" and rom["L37"][1]
    assert rom["_sw-A"][0] == ""


def test_iom_rom_missing_is_unmeasured():
    r = fwcheck.iom_rom(RungResult("pass", {"versions": {"BMC Version": "fbttn-v2019.52.0"}}))
    assert r.verdict == "unmeasured" and r.detail["backup_version"] is None


ETHTOOL_OUT = """===IF enp94s0np0===
address: 02:fa:25:01:00:10
Permanent address: 02:fa:25:01:00:10
driver: mlx5_core
version: 6.8.0-45-generic
firmware-version: 14.32.1912 (MT_2420110034)
bus-info: 0000:5e:00.0
===IF ens3f0np0===
address: 7e:00:00:00:00:01
Permanent address: 02:fb:25:01:00:01
driver: mlx5_core
firmware-version: 14.27.1016 (MT_2420110034)
bus-info: 0000:3b:00.0
===IF eno1===
address: 02:00:00:00:00:09
driver: igb
firmware-version: 3.25, 0x80000681
bus-info: 0000:02:00.0
"""


def test_parse_ethtool_version_psid_permanent_mac():
    p = fwcheck.parse_ethtool(ETHTOOL_OUT)
    assert [(x["ifname"], x["mac"], x["current"], x["psid"], x["pci"]) for x in p] == [
        ("enp94s0np0", "02fa25010010", "14.32.1912", "MT_2420110034", "5e:00.0"),
        ("ens3f0np0", "02fb25010001", "14.27.1016", "MT_2420110034", "3b:00.0"),
        ("eno1", "020000000009", "3.25, 0x80000681", None, "02:00.0")]


def test_nic_installed_by_mac_version_psid_uefi_unread():
    M = FakeRungs.fw_manifests(FakeRungs.__new__(FakeRungs))
    run = lambda *a, **k: (0, ETHTOOL_OUT, "")          # noqa: E731
    sl, arts = fwcheck.nic_installed("h", "flax", "p", M.nic,
                                     {"mezz": "02:fa:25:01:00:10", "fabric A": "02fb25010001"},
                                     run=run)
    assert "ethtool-i" in arts and sl["source"].startswith("installed OS")
    assert [(d["port"], d["phase"]) for d in sl["devices"]] == [
        ("mezz enp94s0np0", "up_to_date"), ("fabric A ens3f0np0", "needs_update")]
    chk, upd = fwcheck.steps("nic", sl)
    assert (chk.verdict, upd.verdict) == ("pass", "fail")
    assert "UEFI" not in upd.detail["headline"]          # never judged from ethtool
    assert dict(chk.detail["rows"])["card 5e:00.0 UEFI"].startswith("not read")
    sl, _ = fwcheck.nic_installed("h", "flax", "p", M.nic, {"fabric B": "02:fb:25:01:00:02"},
                                  run=run)
    assert sl["phase"] == "fault" and "fabric B 02:fb:25:01:00:02" in sl["fault_reason"]


def test_installed_os_sudo_refused_is_named_not_unreachable():
    M = _manifest()
    run = lambda *a, **k: (1, "", "sudo: a password is required\n")   # noqa: E731
    sl, _ = fwcheck.bios("h", "flax", "p", M.bios, run=run)
    chk, _ = fwcheck.steps("bios", sl)
    assert chk.verdict == "unmeasured"
    assert chk.detail["headline"] == "BIOS sudo -n refused for flax (no NOPASSWD): not read"
    sl, _ = fwcheck.nic_installed("h", "flax", "p", M.nic, {"mezz": "02fa25010010"}, run=run)
    assert fwcheck.steps("nic", sl)[0].detail["headline"].startswith(
        "cards not queried: sudo -n refused for flax")


def test_installed_os_falls_back_to_dmidecode_and_ethtool(tmp_path):
    ctx = discovered(tmp_path)
    ctx.rungs.installed.add("C37")           # Deploy got it to its installed OS
    fw.run_firmware(ctx, positions=["C37"])
    assert frows(ctx, "C37") == [(s, "pass", "done") for s in FW_STEPS]
    bios = dict(det(ctx, "C37", "bios-checked")["rows"])
    assert bios["current"] == "TPC_P26F" and bios["source"].startswith("installed OS")
    mlx = det(ctx, "C37", "mlx-checked")
    assert dict(mlx["rows"])["source"].startswith("installed OS: ethtool -i")
    assert [d["port"].split()[0] for d in mlx["slice"]["devices"]] == ["mezz"]
    # a live-image read says so too
    fw.run_firmware(ctx, positions=["L37"])
    assert dict(det(ctx, "L37", "bios-checked")["rows"])["source"] == fw.LIVE_SRC
