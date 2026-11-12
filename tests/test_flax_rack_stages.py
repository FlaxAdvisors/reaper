import json, pathlib, subprocess, pytest
pytest.importorskip("flax_rack")
from flax_rack.creds import Creds, CredsError, load_creds
from flax_switch_sense.driver_sonic import SonicError
from flax_rack.fakes import FakeRungs
from flax_rack.geometry import load_geometry
from flax_rack.kea_client import KeaClient, KeaConflict, KeaError
from flax_rack.orderdb import OrderDB
from flax_rack.stages import OrderContext, run_discover, close_order, source
from tests.test_flax_rack_geometry import FIX
F = pathlib.Path(__file__).parent / "fixtures/rackpost"

class FakeKea:
    """Reservations as upserted; a reserved MAC's lease is its reservation (the device
    renewed) unless `leases_of` overrides it; `dyn` = dynamic leases (switches)."""
    def __init__(self):
        self.upserts, self.conflict_macs, self.fail_delete, self.deletes = [], set(), False, 0
        self.dyn, self.leases_of = {}, {}
        self.archives, self.fail_archive = [], False
    def archive(self, payload):
        if self.fail_archive:
            raise KeaError("flax-control: HTTP 503")
        self.archives.append(payload)
        return {k: len(payload[k]) for k in ("devices", "components", "macs", "rungs")}
    def upsert(self, source, mac, ipv4, hostname, vid):
        if mac in self.conflict_macs:
            raise KeaConflict("mac held by manual row")
        self.upserts.append(dict(source=source, mac=mac, ipv4=ipv4, hostname=hostname, vid=vid))
    def delete_source(self, source):
        if self.fail_delete:
            raise KeaError("flax-control unreachable")
        n, self.upserts = len(self.upserts), []
        return n
    def list(self, source):
        rows = {u["mac"]: u for u in self.upserts if u["source"] == source}
        return [dict(mac=m, ipv4=u["ipv4"], hostname=u["hostname"], vid=u["vid"])
                for m, u in rows.items()]
    def leases(self, macs):
        res = {u["mac"]: {"ip": u["ipv4"], "expire": None, "state": 0, "subnet_id": u["vid"],
                          "hostname": u["hostname"]} for u in self.upserts}
        res.update({m: {"ip": ip, "expire": None, "state": 0, "subnet_id": 26, "hostname": ""}
                    for m, ip in self.dyn.items()})
        res.update(self.leases_of)
        return {m: res[m] for m in macs if res.get(m)}

CREDS = Creds({"sonicuser": "u", "sonicpass": "p", "cisco_user": "cu", "cisco_pass": "cp"},
              [("bu", "bp")], ("flax", "hp"), [("ru", "rp")], ("ou", "op"))


def dell_fdb(cmd, drop=None, add=""):
    """The mgmt switch's FDB read (a SonicDriver runner): the RRORD-101 Dell fixture,
    lines containing `drop` removed, `add` appended."""
    assert "show mac" in cmd
    text = (F / "dell-show-mac.txt").read_text()
    if drop:
        text = "\n".join(l for l in text.splitlines() if drop not in l)
    return text + add

def fake_runner(argv, **kw):
    cp = lambda rc=0, out="": subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")
    if argv[0] == "sshpass":
        return cp(0, (F / "dell-show-mac.txt").read_text())
    if argv[0] == "ipmitool":
        assert "-P" not in argv
        return cp(0, "Chassis Power is off\n")
    return cp()                                    # ping

@pytest.fixture
def ctx(tmp_path):
    g = load_geometry(FIX)
    db = OrderDB(tmp_path / "order.db"); db.init(g)
    return OrderContext(tmp_path, g, db, FakeKea(), CREDS, fake_runner,
                        FakeRungs(g, fdb_runner=dell_fdb))

def test_discover_reserves_all_rr101(ctx):
    run_discover(ctx)
    ups = {(c["hostname"], c["ipv4"], c["mac"]) for c in ctx.kea.upserts}
    assert ("rr101-l37-bmc", "172.32.37.11", "0c42a1e16b52") in ups
    assert ("rr101-jbod01-iom1", "172.32.1.11", "98039b750996") in ups
    assert all(c["source"] == "order:RRORD-101" == source(ctx) for c in ctx.kea.upserts)
    lr = ctx.db.latest_runs()
    assert lr[("L37", "bmc-pinged")]["verdict"] == "pass"
    assert lr[("L37", "bmc-ready")]["verdict"] == "pass"
    assert lr[("_rack", "switch-fdb")]["verdict"] == "pass"
    assert ctx.db.devices()  # macs stored

def test_conflict_fails_one_device_not_stage(ctx):
    ctx.kea.conflict_macs = {"b8599fac1ea8"}
    run_discover(ctx)
    lr = ctx.db.latest_runs()
    assert lr[("L05", "bmc-reserved")]["verdict"] == "fail"
    assert "manual" in lr[("L05", "bmc-reserved")]["detail"]["error"]
    assert lr[("L05", "bmc-leased")]["verdict"] == "skip"
    assert ("L05", "power_action", "on") not in [tuple(c) for c in ctx.rungs.calls]
    assert lr[("R37", "bmc-reserved")]["verdict"] == "pass"

def test_switch_failure_faults_every_device(ctx):
    def denied(cmd):
        raise SonicError("172.26.0.55: ssh exited 5: Permission denied", 5)
    ctx.rungs.fdb_runner = denied
    run_discover(ctx)
    lr = ctx.db.latest_runs()
    assert lr[("_rack", "switch-fdb")]["verdict"] == "fail"
    for pos in ("L37", "JBOD01"):
        assert lr[(pos, "switchportlink")]["verdict"] == "fail"
        assert "mgmt switch FDB not read" in lr[(pos, "switchportlink")]["detail"]["error"]
        assert lr[(pos, "bmc-reserved")]["verdict"] == "skip"
    assert ctx.kea.upserts == []
    assert not [c for c in ctx.rungs.calls if c[0] == "power_action"]

def test_missing_device_not_reserved(ctx):
    ctx.rungs.fdb_runner = lambda cmd: dell_fdb(cmd, drop="Ethernet40")
    run_discover(ctx)
    lr = ctx.db.latest_runs()
    assert lr[("R37", "bmc-mac-seen")]["verdict"] == "fail"
    assert lr[("R37", "bmc-mac-seen")]["detail"]["status"] == "missing"
    assert lr[("R37", "bmc-reserved")]["verdict"] == "skip"
    assert not any(c["hostname"].startswith("rr101-r37") for c in ctx.kea.upserts)

def test_close_failure_leaves_order_open_and_retry_works(ctx):
    run_discover(ctx)
    ctx.kea.fail_delete = True
    with pytest.raises(KeaError): close_order(ctx)
    assert ctx.db.closed_at() is None
    ctx.kea.fail_delete = False
    assert close_order(ctx)["released"] >= 0 and ctx.db.closed_at() is not None
    assert close_order(ctx)["released"] == 0

def test_discover_refused_after_close(ctx):
    close_order(ctx)
    with pytest.raises(RuntimeError): run_discover(ctx)
    assert ctx.kea.upserts == []

def test_load_creds_missing_sonic_keys_message(tmp_path):
    m, b = tmp_path / "c.json", tmp_path / "b.json"
    m.write_text(json.dumps({"x": 1})); b.write_text("[]")
    with pytest.raises(CredsError, match="add sonicuser/sonicpass to the vaulted credentials.json"):
        load_creds(str(m), str(b))

def test_load_creds_ok(tmp_path):
    m, b = tmp_path / "c.json", tmp_path / "b.json"
    m.write_text(json.dumps({"sonicuser": "u", "sonicpass": "p"}))
    b.write_text(json.dumps([{"bmcuser": "a", "bmcpass": "b"}]))
    assert load_creds(str(m), str(b)) == Creds({"sonicuser": "u", "sonicpass": "p"}, [("a", "b")])


def test_load_creds_requires_only_the_racks_noses(tmp_path):
    """lavergne: Cumulus/EOS rack switches, no sonic keys in its credentials.json."""
    m, b = tmp_path / "c.json", tmp_path / "b.json"
    m.write_text(json.dumps({"cumuser": "c", "cumpass": "cp", "cisco_user": "i", "cisco_pass": "ip"}))
    b.write_text("[]")
    c = load_creds(str(m), str(b), noses={"cumulus"})
    assert c.has_switch("cumulus") and c.has_switch("ios") and not c.has_switch("sonic")
    assert load_creds(str(m), str(b), noses=()).switch["cumuser"] == "c"
    with pytest.raises(CredsError, match="add eosuser/eospass"):
        load_creds(str(m), str(b), noses={"cumulus", "eos"})

def test_kea_client_maps_status_and_unreachable():
    import io, urllib.error
    def op(code, body):
        def f(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(body))
        return f
    with pytest.raises(KeaConflict, match="manual"):
        KeaClient(opener=op(409, b'{"error": "manual row"}')).upsert("order:A", "m", "i", "h", 1)
    with pytest.raises(KeaError) as e:
        KeaClient(opener=op(400, b'{"error": "bad"}')).delete_source("x")
    assert not isinstance(e.value, KeaConflict)
    def refused(req, timeout=None): raise ConnectionRefusedError("refused")
    with pytest.raises(KeaError): KeaClient(opener=refused).list("order:A")
    class R:
        def __enter__(s): return s
        def __exit__(s, *a): pass
        def read(s): return b'{"deleted": 4}'
    assert KeaClient(opener=lambda req, timeout=None: R()).delete_source("order:A") == 4

def test_powered_off_node_reserves_bmc_and_derived_host(ctx):
    ctx.rungs.fdb_runner = lambda cmd: dell_fdb(cmd, drop="0C:42:A1:E1:6B:50")
    run_discover(ctx)
    lr = ctx.db.latest_runs()
    assert lr[("L37", "bmc-mac-seen")]["detail"]["note"] == "host derived (host MAC not seen)"
    ups = {(c["hostname"], c["mac"]) for c in ctx.kea.upserts}
    assert {("rr101-l37", "0c42a1e16b50"), ("rr101-l37-bmc", "0c42a1e16b52")} <= ups
    assert lr[("L37", "bmc-reserved")]["verdict"] == "pass"
    # still not on the port after the boot (the fixture never shows it): a fault, not a stop
    assert lr[("L37", "host-mac-seen")]["verdict"] == "fail"
    assert lr[("L37", "host-ssh")]["verdict"] == "pass"

def test_failed_slot_match_clears_previous_macs(ctx):
    run_discover(ctx)
    assert {d["pos"]: d for d in ctx.db.devices()}["R37"]["host_mac"]
    ctx.rungs.fdb_runner = lambda cmd: dell_fdb(cmd, drop="Ethernet40")
    run_discover(ctx)
    r37 = {d["pos"]: d for d in ctx.db.devices()}["R37"]
    assert r37["host_mac"] is None and r37["bmc_mac"] is None

def test_close_needs_no_creds(ctx):
    def boom():
        raise CredsError("cannot read /etc/flax/credentials.json")
    ctx.creds = boom
    assert close_order(ctx)["released"] == 0 and ctx.db.closed_at()

def test_unexpected_recorded_as_rack_slot_match(ctx):
    ctx.rungs.fdb_runner = lambda cmd: dell_fdb(
        cmd, add="  999    1000  AA:BB:CC:DD:EE:FF  Ethernet19  Dynamic\n")
    run_discover(ctx)
    d = ctx.db.latest_runs()[("_rack", "slot-match")]["detail"]
    assert d["unexpected"] == [{"port": 20, "mac": "aabbccddeeff"}] and d["count"] == 1
    assert d["headline"] == "1 unexpected MAC(s)" and d["rows"] == [["port 20", "aa:bb:cc:dd:ee:ff"]]

def test_multi_host_kind_without_matcher_fails_slot_match(tmp_path):
    from flax_rack.geometry import parse_geometry
    d = json.loads(FIX.read_text())
    d["devices"][0]["endpoints"] = {"bmc": 1, "hosts": 4}      # L37 as a 4-card sled
    g = parse_geometry(d)
    db = OrderDB(tmp_path / "order.db"); db.init(g)
    c = OrderContext(tmp_path, g, db, FakeKea(), CREDS, fake_runner, FakeRungs(g, fdb_runner=dell_fdb))
    run_discover(c)
    lr = c.db.latest_runs()
    sm = lr[("L37", "switchportlink")]
    assert sm["verdict"] == "fail" and "no MAC matcher for kind compute-tp" in sm["detail"]["note"]
    assert lr[("L37", "bmc-reserved")]["verdict"] == "skip"
    assert not any(u["hostname"].startswith("rr101-l37") for u in c.kea.upserts)
    assert lr[("C37", "bmc-reserved")]["verdict"] == "pass"

def test_iom_without_matched_mac_fails_reserve_not_crash(ctx):
    """endpoints say 2 IOMs but only iom1 is cabled to the mgmt switch."""
    from flax_rack.geometry import parse_geometry
    d = json.loads(FIX.read_text())
    j = next(x for x in d["devices"] if x["pos"] == "JBOD01")
    del j["links"]["iom2"]
    ctx.g = parse_geometry(d)
    run_discover(ctx)
    lr = ctx.db.latest_runs()
    assert lr[("JBOD01.iom2", "switchportlink")]["detail"]["error"] == "no C iom2 link in geometry"
    assert lr[("JBOD01", "switchportlink")]["verdict"] == "fail"
    assert lr[("JBOD01.iom1", "bmc-reserved")]["verdict"] == "pass"
    assert lr[("JBOD01", "bmc-reserved")]["verdict"] == "pass"     # iom2's is a skip
