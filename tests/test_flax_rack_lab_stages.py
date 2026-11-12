"""Lab racks across the stages: present devices only, no integration rungs (Task 6)."""
import pytest
pytest.importorskip("flax_rack")
from flax_rack.deploy import deploy_passed, deploy_rungs
from flax_rack.geometry import parse_geometry, with_placements
from flax_rack.orderdb import OrderDB
from flax_rack.qualify import device_rungs, qualify_passed
from flax_rack.stages import effective, OrderContext
from tests.test_flax_rack_geometry import base, dev
from tests.test_flax_rack_stages import CREDS, FakeKea, fake_runner

TP = dict(serial="QTFCKQ024030B", product="Tioga Pass Single Side", family="tiogapass",
          switch="H", port=18, lane=1, host_mac="0c42a1ecbc0e", bmc_mac="0c42a1ecbc11", who="op")


def _ctx(tmp_path, lab=True):
    g = parse_geometry(base(lab=lab, devices=[dev("C18", "bb-head", drives="BB20"),
                                              dev("BB20", "bigbasin", ou=20, expect={"gpu": 8}),
                                              dev("L31", "compute-tp")]))
    db = OrderDB(tmp_path / "order.db"); db.init(g)
    return OrderContext(tmp_path, g, db, FakeKea(), CREDS, fake_runner, None)


def test_lab_qualify_has_no_mesh_or_ha(tmp_path):
    c = _ctx(tmp_path)
    c.db.place("C18", **TP)
    d = effective(c).g.device("C18")
    rungs = device_rungs(d)
    assert "iperf-mesh" not in rungs and "ha-path" not in rungs and "pxe-live" in rungs


def test_lab_deploy_has_no_ha_or_iperf(tmp_path):
    c = _ctx(tmp_path)
    c.db.place("C18", **TP)
    rungs = deploy_rungs(effective(c).g.device("C18"))
    assert not {"ha-config", "ha-test", "deploy-iperf"} & set(rungs)
    assert rungs[-1] == "deploy-stress"


def test_an_order_keeps_its_integration_rungs(tmp_path):
    g = parse_geometry(base(devices=[dev("L31", "compute-tp")]))
    assert "iperf-mesh" in device_rungs(g.device("L31")) and "deploy-iperf" in deploy_rungs(g.device("L31"))


def test_stages_complete_with_zero_present(tmp_path):
    c = _ctx(tmp_path)
    e = effective(c)
    assert e.g.devices == []
    assert qualify_passed(c.db, e.g) == (True, []) and deploy_passed(c.db, e.g) == (True, [])


def test_effective_keeps_present_only(tmp_path):
    c = _ctx(tmp_path)
    c.db.place("C18", **TP)
    assert [d.pos for d in effective(c).g.devices] == ["C18", "BB20"]      # BB20 via its head
    (tmp_path / "o").mkdir()
    assert [d.pos for d in effective(_ctx(tmp_path / "o", lab=False)).g.devices] == ["C18", "BB20", "L31"]


# ---------------------------------------------------------------- Task 7: enclosure-check
from flax_rack.geometry import absent
from flax_rack.qualify import enclosure_rung


def test_head_qualify_has_enclosure_check(tmp_path):
    c = _ctx(tmp_path)
    c.db.place("C18", **TP)
    g = effective(c).g
    assert "enclosure-check" in device_rungs(g.device("C18"))
    assert "enclosure-check" not in device_rungs(g.device("C18").__class__(
        **{**g.device("C18").__dict__, "kind": "compute-tp", "drives": None}))


def test_enclosure_present_when_its_head_is_placed(tmp_path):
    c = _ctx(tmp_path)
    assert absent(c.g, {}) == {"C18", "BB20", "L31"}
    c.db.place("C18", **TP)
    assert absent(c.g, c.db.placements()) == {"L31"}          # BB20 is judged through C18


def test_enclosure_rung_reads_the_inventory_captures(tmp_path):
    import pathlib
    F = pathlib.Path(__file__).parent / "fixtures/rackpost/enclosure"
    c = _ctx(tmp_path)
    c.db.place("C18", **TP)
    g = effective(c).g
    arts = {"lspci-vv": (F / "howlin-53-lspci-vv.txt").read_text(),
            "lsscsi-c": (F / "howlin-53-lsscsi-c.txt").read_text()}
    r = enclosure_rung(g, g.device("C18"), arts)
    assert r.verdict == "fail" and r.detail["headline"] == "4/8 V100 (x16 4/4)"
    r = enclosure_rung(g, g.device("C18"), {})           # no capture: never a pass
    assert r.verdict == "fail" and "lspci" in r.detail["headline"]


def test_enclosure_qualify_follows_its_heads_check(tmp_path):
    from flax_rack.qualify import qualify_why
    from flax_rungs.result import RungResult
    c = _ctx(tmp_path)
    c.db.place("C18", **TP)
    g = effective(c).g
    assert qualify_why(c.db.latest_runs(), g)["BB20"] == "through C18: enclosure-check not run"
    rid = c.db.start_run("C18", "qualify", "enclosure-check")
    c.db.finish_run(rid, RungResult("pass", {"headline": "8/8 V100 (x16 8/8)"}))
    assert "BB20" not in qualify_why(c.db.latest_runs(), g)
    rid = c.db.start_run("C18", "qualify", "enclosure-check")
    c.db.finish_run(rid, RungResult("fail", {"headline": "4/8 V100 (x16 4/4)"}))
    assert qualify_why(c.db.latest_runs(), g)["BB20"].startswith("through C18: enclosure-check")


# ---------------------------------------------------------------- Task 8: lab Deploy
from flax_rack.deploy import plan, GateError, install_disk, FROM_PHASES, PHASES


def _installed_lr(pos):
    return {(pos, "host-ssh"): {"verdict": "pass", "detail": {"installed_os": True}}}


def test_from_checks_is_a_start_and_kind_check_a_phase():
    assert "checks" in FROM_PHASES
    assert PHASES.index("kind-check") == PHASES.index("user") + 1


def test_from_checks_refuses_without_installed_os(tmp_path):
    c = _ctx(tmp_path)
    c.db.place("C18", **TP)
    g = effective(c).g
    with pytest.raises(GateError, match="installed OS"):
        plan({}, g, positions=["C18"], from_phase="checks", installed=set())
    targets, _, _ = plan(_installed_lr("C18"), g, positions=["C18"], from_phase="checks",
                         installed={"C18"})
    assert [d.pos for d in targets] == ["C18"]


def test_bb_head_deploy_rungs_have_kind_check(tmp_path):
    c = _ctx(tmp_path)
    c.db.place("C18", **TP)
    assert "kind-check" in deploy_rungs(effective(c).g.device("C18"))
    assert "kind-check" not in deploy_rungs(parse_geometry(base(devices=[dev("L31", "compute-tp")])).device("L31"))


def test_install_disk_by_family(tmp_path):
    c = _ctx(tmp_path)
    c.db.place("C18", **{**TP, "family": "leopard"})
    g = effective(c).g
    assert install_disk(g.device("C18")) == "sda"
    assert install_disk(parse_geometry(base(devices=[dev("L31", "compute-tp")])).device("L31")) == "nvme0n1"
    assert install_disk(parse_geometry(base(devices=[dev("L35", "compute-leopard")])).device("L35")) == "sda"


def test_existing_os_kept_counts_as_deployed():
    from flax_rack.deploy import deploy_why
    g = parse_geometry(base(lab=True, devices=[dev("L31", "compute-tp")]))
    na = {"verdict": "skip", "detail": {"na": True, "reason": "existing OS kept"}}
    lr = {("L31", r): na for r in ("install", "firstboot", "install-console", "flax-user")}
    lr[("L31", "deploy-stress")] = {"verdict": "pass", "detail": {}}
    assert deploy_why(lr, g) == {}


def test_lab_run_deploys_installed_hosts_from_checks(tmp_path, monkeypatch):
    from flax_rack import run as runmod
    c = _ctx(tmp_path)
    c.db.place("C18", **TP)
    g = effective(c).g
    row = {"run_after": 0, "options": {}}
    monkeypatch.setattr(runmod, "discover_blockers", lambda *a, **k: {})
    monkeypatch.setattr(runmod, "run_gate_why", lambda *a, **k: None)
    monkeypatch.setattr(runmod, "installed_positions", lambda db, lr: {"C18"})
    ch = runmod.deploy_choice(c.db, g, row)
    assert ch["targets"] == ["C18"] and ch["from_phase"] == "checks" and ch["firstboot"] == []


# ---------------------------------------------------------------- final review fixes
def test_run_on_an_empty_lab_rack_ends_done_not_held(tmp_path):
    from flax_rack import run as runmod
    from tests.test_flax_rack_unplaced import _lab_ctx
    c, _ = _lab_ctx(tmp_path)
    rid = runmod.run(c, who="op")
    p = c.db.run_plan(rid)
    assert p["state"] == "done", p
    assert any("0 present" in e["text"] for e in c.db.events_all())


# ---------------------------------------------------------------- as-built howlin (2026-11-02)
def test_gc_head_drives_a_grand_canyon_with_two_ioms():
    g = parse_geometry(base(lab=True, devices=[dev("R12", "gc-head", drives="GC05"),
                                               dev("GC05", "grandcanyon", ou=5)]))
    assert g.device("GC05").endpoints == {"hosts": 0, "bmc": 0, "ioms": 2}
    assert g.device("R12").drives == ("GC05",)
    assert "enclosure-check" in device_rungs(g.device("R12"))


def test_one_head_drives_two_lightning_jbofs():
    from flax_rack.qualify import enclosure_rung
    g = parse_geometry(base(lab=True, devices=[
        dev("L12", "lightning-head", drives=["J14", "J15"]),
        dev("J14", "lightning", ou=14, expect={"nvme": 4}), dev("J15", "lightning", ou=15, expect={"nvme": 4})]))
    assert g.device("L12").drives == ("J14", "J15")
    assert absent(g, {"L12": TP}) == set()                   # both JBOFs judged through L12
    from flax_rack.geometry import merged_expect
    assert merged_expect(g, g.device("L12")) == {"nvme": 8}       # the two JBOFs' BoMs summed
    r = enclosure_rung(g, g.device("L12"), {"lspci-vv": "00:00.0 Host bridge: Intel\n"})
    assert r.verdict == "fail" and r.detail["enclosure"] == "J14, J15"


def test_merged_expect_skips_an_absent_enclosure(tmp_path):
    from flax_rack.geometry import merged_expect
    c = _ctx(tmp_path)
    g = parse_geometry(base(lab=True, devices=[dev("R12", "gc-head", drives="GC05"),
                                               dev("GC05", "grandcanyon", ou=5)]))
    db = OrderDB(tmp_path / "o.db"); db.init(g)
    db.place("R12", **TP)
    e = effective(OrderContext(tmp_path, g, db, FakeKea(), CREDS, fake_runner, None)).g
    assert [d.pos for d in e.devices] == ["R12"] and merged_expect(e, e.device("R12")) == {}


def test_a_failed_kind_check_does_not_skip_stress(tmp_path):
    from types import SimpleNamespace
    from flax_rack.deploy import _kind_check
    from flax_rungs.result import RungResult
    c = _ctx(tmp_path)
    c.db.place("C18", **TP)
    e = effective(c)
    x = SimpleNamespace(pos="C18", d=e.g.device("C18"), ips={"host": "172.31.18.51"}, failed=None, unmeasured=False)
    R = SimpleNamespace(host_run=lambda *a, **k: (1, "", ""),
                        kind_check=lambda kind, run, exp: RungResult("fail", {"headline": "GRUB lacks pci="}))
    e = e.__class__(**{**e.__dict__, "creds": CREDS})
    _kind_check(e, R, CREDS, x)
    assert x.failed is None                       # recorded red, but stress still runs
    assert c.db.latest_runs()[("C18", "kind-check")]["verdict"] == "fail"
