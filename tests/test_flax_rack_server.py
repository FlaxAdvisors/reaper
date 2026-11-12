import json, shutil, sqlite3, subprocess, threading, urllib.error, urllib.request
from pathlib import Path
import pytest
pytest.importorskip("flax_rack.server")
from flax_rack.creds import Creds
from flax_rack.geometry import load_geometry
from flax_rack.orderdb import OrderDB
from flax_rack.server import Registry, make_server, DEFAULT_STATIC
from flax_rack.stages import OrderContext
from tests.test_flax_rack_geometry import FIX
from flax_rack.fakes import FakeRungs
from tests.test_flax_rack_stages import CREDS, FakeKea, dell_fdb, fake_runner

RACK = {"confirm": "RRORD-101"}          # a rack-wide Discover takes the typed order id


class Resp:
    def __init__(self, status, body, headers):
        self.status, self.body, self.headers = status, body, headers


def _req(srv, path, method="GET", body=None):
    url = f"http://127.0.0.1:{srv.server_address[1]}{path}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=10) as f:
            return Resp(f.status, f.read(), f.headers)
    except urllib.error.HTTPError as e:
        return Resp(e.code, e.read(), e.headers)


def get(srv, path):
    return _req(srv, path)


def get_json(srv, path):
    r = get(srv, path)
    assert r.status == 200, r.body
    return json.loads(r.body)


def post(srv, path, body):
    return _req(srv, path, "POST", body)


def _order_dir(root, name="RRORD-101", **patch):
    d = root / name
    d.mkdir()
    geo = json.loads(Path(FIX).read_text())
    for k, v in patch.items():
        if k == "prefix":
            geo["network"]["prefix"] = v
        elif k == "vid":
            geo["network"].update(vid=v, cidr=f"172.{v}.0.0/16")
        else:
            geo[k] = v
    (d / "geometry.json").write_text(json.dumps(geo))
    (d / ".rackpost").touch()
    return d


class Factory:
    """Fake context factory; `gate` (an Event) makes discover block in its FDB read."""
    def __init__(self):
        self.gate = None
        self.kea = {}
        self.entered = threading.Event()

    def __call__(self, rack_dir, g, db):
        kea = self.kea.setdefault(g.url_prefix, FakeKea())

        def fdb(cmd):
            if self.gate is not None:
                self.entered.set()
                assert self.gate.wait(10)
            return dell_fdb(cmd)
        return OrderContext(rack_dir, g, db, kea, CREDS, fake_runner, FakeRungs(g, fdb_runner=fdb))


def _start(root, factory):
    s = make_server(root, port=0, static_dirs=DEFAULT_STATIC, context_factory=factory)
    t = threading.Thread(target=s.serve_forever, daemon=True)
    t.start()
    return s


@pytest.fixture
def factory():
    return Factory()


@pytest.fixture
def srv(tmp_path, factory):
    _order_dir(tmp_path)
    s = _start(tmp_path, factory)
    s.factory = factory
    yield s
    if factory.gate is not None:
        factory.gate.set()
    s.shutdown(); s.server_close()


@pytest.fixture
def srv2(tmp_path, factory):
    _order_dir(tmp_path)
    _order_dir(tmp_path, "RRORD-101b", rack="b", prefix="rr101b-", vid=33)
    s = _start(tmp_path, factory)
    s.factory = factory
    yield s
    if factory.gate is not None:
        factory.gate.set()
    s.shutdown(); s.server_close()


def _wait_idle(srv, prefix="rr101", t=10):
    import time
    end = time.time() + t
    while time.time() < end:
        if not get_json(srv, f"/{prefix}/api/state")["stage_running"]:
            return
        time.sleep(0.05)
    raise AssertionError("stage still running")


def test_only_enabled_orders_served(srv, tmp_path):
    assert get(srv, "/rr101/api/state").status == 200
    (tmp_path / "RRORD-101/.rackpost").unlink()
    assert get(srv, "/rr101/api/state").status == 404


def test_unknown_prefix_404(srv):
    assert get(srv, "/zz999/api/state").status == 404


def test_state_shape(srv):
    s = get_json(srv, "/rr101/api/state")
    assert (s["order"], s["customer"], s["vid"], s["closed_at"], s["stage_running"]) == \
        ("RRORD-101", "F4", 32, None, False)
    assert {x["id"] for x in s["switches"]} == {"A", "B", "C"}
    l05 = next(d for d in s["devices"] if d["pos"] == "L05")
    assert l05["pop"] == "f4-bc-head-25g" and l05["pop_overridden"] is False
    assert l05["addrs"]["host"] == "172.32.5.1"
    assert l05["addrs"]["bmc"] == "172.32.5.11"
    assert l05["links"]["mezz0"] == "C:5" and l05["links"]["pcie0"] == "A:28/1"
    r37 = next(d for d in s["devices"] if d["pos"] == "R37")
    assert r37["pop_default"] == "f4-compute-tp" and r37["runs"] == {}
    j = next(d for d in s["devices"] if d["pos"] == "JBOD01")
    assert j["addrs"] == {"iom1": "172.32.1.11", "iom2": "172.32.1.12"}
    assert "f4-compute-tp" in s["profiles"] and s["phases"][0] == "discover"


def test_state_and_index_carry_the_build(srv, tmp_path):
    from flax_post.version import __version__
    from flax_rack.version import BUILD
    assert get_json(srv, "/rr101/api/state")["build"] == BUILD
    assert get_json(srv, "/rackpost/api/racks")["build"] == BUILD
    # BUILD (the role writes it) is read at start; without it: <post version>·dev
    code = "import flax_rack.version as v; print(v.BUILD)"
    f = tmp_path / "BUILD"
    f.write_text("0.9.999·abc1234-dirty\n")
    for want in ("0.9.999·abc1234-dirty", f"{__version__}·dev"):
        r = subprocess.run(["python3", "-c", code], capture_output=True, text=True, check=True,
                           cwd=Path(__file__).resolve().parent.parent,
                           env={"RACKPOST_BUILD_FILE": str(f), "PATH": "/usr/bin:/bin"})
        assert r.stdout.strip() == want
        f.unlink(missing_ok=True)


def test_state_after_discover(srv):
    assert post(srv, "/rr101/api/stage/discover", RACK).status == 202
    _wait_idle(srv)
    s = get_json(srv, "/rr101/api/state")
    l37 = next(d for d in s["devices"] if d["pos"] == "L37")
    assert l37["host_mac"] == "0c:42:a1:e1:6b:50" and l37["bmc_mac"] == "0c:42:a1:e1:6b:52"
    assert l37["runs"]["bmc-reserved"]["verdict"] == "pass"
    assert [r for r in s["discover_rungs"] if r in l37["runs"]] == s["discover_rungs"]
    assert s["discover_old_rungs"] == ["slot-match", "reserve", "bmc-reach"]
    j = next(d for d in s["devices"] if d["pos"] == "JBOD01")
    assert j["endpoint_runs"]["iom2"]["serial"]["verdict"] == "pass"
    assert j["ioms"]["iom1"]["mac"] == "98:03:9b:75:09:96"
    sw = {x["id"]: x for x in s["switches"]}
    assert sw["A"]["endpoints"] == {"sonic": 1, "bmc": 1} and "switchportlink" in sw["A"]["runs"]
    assert s["rack_runs"]["switch-fdb"]["verdict"] == "pass"
    assert "fdb" not in (s["rack_runs"]["switch-fdb"]["detail"] or {})   # payload trimmed
    assert any(e["kind"] == "discover" for e in s["events"])


def test_pop_override_roundtrip(srv):
    r = post(srv, "/rr101/api/pop", {"pos": "R37", "profile": "ovh-bare-tp"})
    assert r.status == 200
    r37 = next(d for d in get_json(srv, "/rr101/api/state")["devices"] if d["pos"] == "R37")
    assert r37["pop"] == "ovh-bare-tp" and r37["pop_overridden"] is True
    assert post(srv, "/rr101/api/pop", {"pos": "R37", "profile": None}).status == 200
    r37 = next(d for d in get_json(srv, "/rr101/api/state")["devices"] if d["pos"] == "R37")
    assert r37["pop"] == "f4-compute-tp" and r37["pop_overridden"] is False


def test_pop_rejects_bad_input(srv):
    assert post(srv, "/rr101/api/pop", {"pos": "X99", "profile": None}).status == 404
    assert post(srv, "/rr101/api/pop", {"pos": "R37", "profile": "../etc"}).status == 400


def test_second_stage_start_is_409(srv):
    srv.factory.gate = threading.Event()
    assert post(srv, "/rr101/api/stage/discover", RACK).status == 202
    assert srv.factory.entered.wait(5)
    assert get_json(srv, "/rr101/api/state")["stage_running"] is True
    assert post(srv, "/rr101/api/stage/discover", RACK).status == 409
    srv.factory.gate.set()
    _wait_idle(srv)
    assert post(srv, "/rr101/api/stage/discover", RACK).status == 202   # free again


def test_stage_exception_is_an_event_not_a_crash(tmp_path):
    _order_dir(tmp_path)

    def boom(rack_dir, g, db):
        raise RuntimeError("no creds file")
    s = _start(tmp_path, boom)
    try:
        assert post(s, "/rr101/api/stage/discover", RACK).status == 202
        _wait_idle(s)
        st = get_json(s, "/rr101/api/state")
        assert any("no creds file" in e["text"] for e in st["events"])
        assert post(s, "/rr101/api/stage/discover", RACK).status == 202   # lock released
    finally:
        s.shutdown(); s.server_close()


def test_two_racks_run_concurrently(srv2):
    srv2.factory.gate = threading.Event()
    assert post(srv2, "/rr101/api/stage/discover", RACK).status == 202
    assert srv2.factory.entered.wait(5)
    assert post(srv2, "/rr101b/api/stage/discover", RACK).status == 202
    assert post(srv2, "/rr101/api/stage/discover", RACK).status == 409
    srv2.factory.gate.set()
    _wait_idle(srv2, "rr101"); _wait_idle(srv2, "rr101b")
    ups = srv2.factory.kea["rr101b"].upserts
    assert ups and all(u["source"] == "order:RRORD-101/b" for u in ups)
    assert any(u["hostname"] == "rr101b-l37" for u in ups)


def test_duplicate_prefix_is_conflict_not_served(tmp_path, factory):
    _order_dir(tmp_path, "RRORD-101")
    _order_dir(tmp_path, "RRORD-102", order="RRORD-102")       # same prefix rr101-
    s = _start(tmp_path, factory)
    try:
        assert get(s, "/rr101/api/state").status == 404
        idx = get_json(s, "/rackpost/api/racks")
        assert idx["orders"] == []
        c = [x for x in idx["conflicts"] if "rr101" in x["key"]]
        assert len(c) == 1 and len(c[0]["dirs"]) == 2
    finally:
        s.shutdown(); s.server_close()


def test_duplicate_order_rack_is_conflict(tmp_path):
    _order_dir(tmp_path, "A")
    _order_dir(tmp_path, "B", prefix="rr102-")                # same (RRORD-101, "")
    sc = Registry(tmp_path).scan()
    assert sc.racks == {}
    assert any(sorted(Path(p).name for p in c["dirs"]) == ["A", "B"] for c in sc.conflicts)


def test_bad_geometry_is_conflict_entry(tmp_path):
    d = _order_dir(tmp_path)
    (d / "geometry.json").write_text("{not json")
    sc = Registry(tmp_path).scan()
    assert sc.racks == {} and sc.conflicts and "error" in sc.conflicts[0]


def test_index_groups_racks_by_order(srv2):
    idx = get_json(srv2, "/rackpost/api/racks")
    o = next(x for x in idx["orders"] if x["order"] == "RRORD-101")
    assert sorted(r["rack"] for r in o["racks"]) == ["", "b"]
    assert {r["url"] for r in o["racks"]} == {"/rr101/", "/rr101b/"}
    page = get(srv2, "/rackpost/")
    assert page.status == 200 and b"RRORD-101" in page.body and b"/rr101b/" in page.body


def test_close_requires_typed_confirm(srv):
    assert post(srv, "/rr101/api/close", {"confirm": "rr101"}).status == 400
    assert get_json(srv, "/rr101/api/state")["closed_at"] is None
    r = post(srv, "/rr101/api/close", {"confirm": "RRORD-101"})
    assert r.status == 200
    assert get_json(srv, "/rr101/api/state")["closed_at"]
    assert post(srv, "/rr101/api/stage/discover", RACK).status == 409    # closed
    assert post(srv, "/rr101/api/close", {"confirm": "RRORD-101"}).status == 200  # idempotent


def test_close_kea_error_is_502_and_order_open(srv):
    get(srv, "/rr101/api/state")
    srv.factory.kea.setdefault("rr101", FakeKea()).fail_delete = True
    r = post(srv, "/rr101/api/close", {"confirm": "RRORD-101"})
    assert r.status == 502 and b"unreachable" in r.body
    assert get_json(srv, "/rr101/api/state")["closed_at"] is None
    srv.factory.kea["rr101"].fail_delete = False
    assert post(srv, "/rr101/api/close", {"confirm": "RRORD-101"}).status == 200


def test_restart_marks_interrupted(tmp_path, factory):
    d = _order_dir(tmp_path)
    db = OrderDB(d / "order.db"); db.init(load_geometry(d / "geometry.json"))
    db.start_run("C37", "discover", "slot-match")
    s = _start(tmp_path, factory)
    try:
        c37 = next(x for x in get_json(s, "/rr101/api/state")["devices"] if x["pos"] == "C37")
        assert c37["runs"]["slot-match"]["status"] == "interrupted"
        assert post(s, "/rr101/api/stage/discover", RACK).status == 202
    finally:
        s.shutdown(); s.server_close()


def test_static_rack_css_served_from_flax_post(srv):
    r = get(srv, "/rr101/static/rack.css")
    assert r.status == 200 and b".tile{" in r.body
    assert get(srv, "/rr101/static/rackpost.css").status == 200
    assert get(srv, "/rr101/static/petite-vue.iife.js").status == 200


def test_index_html_and_redirect(srv):
    r = get(srv, "/rr101/")
    assert r.status == 200 and b'v-scope="App()"' in r.body
    assert get(srv, "/rr101").status == 200            # followed the redirect


def test_static_path_traversal_rejected(srv):
    for p in ("/rr101/static/../server.py", "/rr101/static/%2e%2e/server.py",
              "/rr101/static//etc/passwd", "/rr101/static/..", "/rr101/static/vendor/x.js"):
        assert get(srv, p).status == 404, p


# ---------------------------------------------------------------- fix wave
def _edit_geo(d, fn):
    import os
    p = d / "geometry.json"
    geo = json.loads(p.read_text())
    fn(geo)
    st = p.stat()
    p.write_text(json.dumps(geo))
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))   # a distinct mtime


def test_geometry_edit_served_and_used_by_discover_without_restart(srv, tmp_path):
    s = get_json(srv, "/rr101/api/state")
    l37 = next(d for d in s["devices"] if d["pos"] == "L37")
    assert l37["addrs"]["host"] == "172.32.37.1"

    def edit(geo):          # new vid (IPs) and new prefix (hostnames)
        geo["network"].update(vid=33, cidr="172.33.0.0/16", prefix="rr102-")
    _edit_geo(tmp_path / "RRORD-101", edit)
    assert get(srv, "/rr101/api/state").status == 404          # old prefix gone at once
    s = get_json(srv, "/rr102/api/state")
    l37 = next(d for d in s["devices"] if d["pos"] == "L37")
    assert (s["vid"], l37["addrs"]["host"], l37["names"]["host"]) == (33, "172.33.37.1", "rr102-l37")
    assert post(srv, "/rr102/api/stage/discover", RACK).status == 202
    _wait_idle(srv, "rr102")
    ups = {(u["hostname"], u["ipv4"], u["vid"]) for u in srv.factory.kea["rr102"].upserts}
    assert ("rr102-l37", "172.33.37.1", 33) in ups
    assert not any(u[0].startswith("rr101") or u[2] == 32 for u in ups)


def test_geometry_edit_same_prefix_is_adopted(srv, tmp_path):
    get_json(srv, "/rr101/api/state")
    _edit_geo(tmp_path / "RRORD-101",
              lambda geo: geo["network"].update(vid=34, cidr="172.34.0.0/16"))
    s = get_json(srv, "/rr101/api/state")
    l05 = next(d for d in s["devices"] if d["pos"] == "L05")
    assert s["vid"] == 34 and l05["addrs"]["bmc"] == "172.34.5.11"


def test_geometry_edit_to_bad_file_stops_serving(srv, tmp_path):
    get_json(srv, "/rr101/api/state")
    d = tmp_path / "RRORD-101"
    good = (d / "geometry.json").read_text()
    _edit_geo(d, lambda geo: geo["devices"][1]["links"].update(mezz0=geo["devices"][0]["links"]["mezz0"]))
    assert get(srv, "/rr101/api/state").status == 404
    assert any("share switch port" in c["error"] for c in get_json(srv, "/rackpost/api/racks")["conflicts"])
    (d / "geometry.json").write_text(good)
    assert get(srv, "/rr101/api/state").status == 200


def _no_creds(monkeypatch):
    """The real default factory, with Kea faked and every creds read failing."""
    import flax_rack.creds, flax_rack.kea_client
    from flax_rack.creds import CredsError
    kea = FakeKea()
    kea.upserts = [{"source": "order:RRORD-101"}]
    monkeypatch.setattr(flax_rack.kea_client, "KeaClient", lambda *a, **k: kea)

    def boom(*a, **k):
        raise CredsError("cannot read /etc/flax/credentials.json: missing")
    monkeypatch.setattr(flax_rack.creds, "load_creds", boom)
    return kea


def test_close_needs_kea_only_server(tmp_path, monkeypatch):
    from flax_rack.server import default_context_factory
    _no_creds(monkeypatch)
    _order_dir(tmp_path)
    s = _start(tmp_path, default_context_factory)
    try:
        r = post(s, "/rr101/api/close", {"confirm": "RRORD-101"})
        assert r.status == 200, r.body
        assert json.loads(r.body)["released"] == 1
        assert get_json(s, "/rr101/api/state")["closed_at"]
    finally:
        s.shutdown(); s.server_close()


def test_discover_without_creds_is_an_event(tmp_path, monkeypatch):
    from flax_rack.server import default_context_factory
    _no_creds(monkeypatch)
    _order_dir(tmp_path)
    s = _start(tmp_path, default_context_factory)
    try:
        assert post(s, "/rr101/api/stage/discover", RACK).status == 202
        _wait_idle(s)
        assert any("CredsError" in e["text"] for e in get_json(s, "/rr101/api/state")["events"])
    finally:
        s.shutdown(); s.server_close()


def test_close_needs_kea_only_cli(tmp_path, monkeypatch, capsys):
    from flax_rack.__main__ import main
    _no_creds(monkeypatch)
    d = _order_dir(tmp_path)
    monkeypatch.chdir(d)
    assert main(["close", "--confirm", "RRORD-101"]) == 0
    assert "1 reservation(s) released" in capsys.readouterr().out
    assert OrderDB(d / "order.db").closed_at()


def test_discover_without_creds_cli_exits_with_message(tmp_path, monkeypatch):
    from flax_rack.__main__ import main
    _no_creds(monkeypatch)
    monkeypatch.chdir(_order_dir(tmp_path))
    with pytest.raises(SystemExit, match="cannot read"):
        main(["discover", "--confirm", "RRORD-101"])


def test_unexpected_macs_in_state(srv):
    import tests.test_flax_rack_stages as st

    base = srv.app.factory

    def factory(rack_dir, g, db):
        c = base(rack_dir, g, db)
        c.rungs.fdb_runner = lambda cmd: st.dell_fdb(
            cmd, add="  999    1000  AA:BB:CC:DD:EE:FF  Ethernet19  Dynamic\n")
        return c
    srv.app.factory = factory
    assert post(srv, "/rr101/api/stage/discover", RACK).status == 202
    _wait_idle(srv)
    s = get_json(srv, "/rr101/api/state")
    assert s["unexpected"] == [{"port": 20, "mac": "aa:bb:cc:dd:ee:ff"}]
    assert s["rack_runs"]["slot-match"]["detail"]["unexpected"] == [{"port": 20, "mac": "aabbccddeeff"}]
    assert b"s.unexpected" in get(srv, "/rr101/").body           # the UI renders the list


def test_unexpected_empty_when_clean(srv):
    assert post(srv, "/rr101/api/stage/discover", RACK).status == 202
    _wait_idle(srv)
    s = get_json(srv, "/rr101/api/state")
    assert s["unexpected"] == [] and s["rack_runs"]["slot-match"]["detail"]["count"] == 0


def _raw_post(srv, path, data, headers):
    url = f"http://127.0.0.1:{srv.server_address[1]}{path}"
    r = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=10) as f:
            return f.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.mark.parametrize("ctype", ["text/plain", "application/x-www-form-urlencoded",
                                   "multipart/form-data; boundary=x", None])
def test_post_requires_json_content_type(srv, ctype):
    h = {"Content-Type": ctype} if ctype else {}
    body = json.dumps({"confirm": "RRORD-101"}).encode()
    assert _raw_post(srv, "/rr101/api/close", body, h) == 415
    assert _raw_post(srv, "/rr101/api/stage/discover", b"{}", h) == 415
    assert _raw_post(srv, "/rr101/api/pop", json.dumps({"pos": "R37", "profile": "ovh-bare-tp"}).encode(), h) == 415
    s = get_json(srv, "/rr101/api/state")
    assert s["closed_at"] is None and s["stage_running"] is False and not s["events"]
    assert _raw_post(srv, "/rr101/api/pop", json.dumps({"pos": "R37", "profile": None}).encode(),
                     {"Content-Type": "application/json; charset=utf-8"}) == 200


def test_bad_content_length_is_400(srv):
    import http.client
    for cl in ("-1", "abc"):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        c.putrequest("POST", "/rr101/api/pop")
        c.putheader("Content-Type", "application/json")
        c.putheader("Content-Length", cl)
        c.endheaders()
        assert c.getresponse().status == 400, cl
        c.close()


def test_shared_vid_is_conflict(tmp_path):
    _order_dir(tmp_path, "A")
    _order_dir(tmp_path, "B", rack="b", prefix="rr101b-")         # same vid 32
    sc = Registry(tmp_path).scan()
    assert sc.racks == {}
    assert any(c["key"] == "vid 32" for c in sc.conflicts)


class SiteFactory(Factory):
    """The fake-serve setup: switches on turtle ports with dynamic leases."""
    def __call__(self, rack_dir, g, db):
        from flax_rack.fakes import with_site_ports
        g = with_site_ports(g)
        kea = self.kea.setdefault(g.url_prefix, FakeKea())
        rungs = FakeRungs(g, fdb_runner=dell_fdb)
        kea.dyn.update(rungs.switch_leases())
        return OrderContext(rack_dir, g, db, kea, CREDS, fake_runner, rungs)


@pytest.fixture
def site_srv(tmp_path):
    _order_dir(tmp_path)
    s = _start(tmp_path, SiteFactory())
    yield s
    s.shutdown(); s.server_close()


def test_state_drawer_views_after_discover_and_firmware(site_srv):
    """Every switch and BMC endpoint gets its own identity / versions / addresses."""
    srv = site_srv
    assert post(srv, "/rr101/api/stage/discover", RACK).status == 202
    _wait_idle(srv)
    assert post(srv, "/rr101/api/stage/firmware", {}).status == 202
    _wait_idle(srv)
    s = get_json(srv, "/rr101/api/state")
    l37 = next(d for d in s["devices"] if d["pos"] == "L37")
    assert l37["serial"] and l37["identity"]["serial_field"] == "Product Serial"
    r05 = next(d for d in s["devices"] if d["pos"] == "R05")
    bmc = next(p for p in r05["fw"]["parts"] if p["part"] == "BMC active")
    assert (bmc["current"], bmc["phase"]) == ("flax-onetree-1.1.3", "needs_update") and bmc["target"]
    j = next(d for d in s["devices"] if d["pos"] == "JBOD01")
    assert j["serial"] is None                          # the roll-up row reads no serial
    i2 = j["ioms"]["iom2"]
    assert i2["serial"] == "J49FAKE2" and i2["identity"]["serial_field"] == "Board Serial"
    assert i2["mac"] and i2["mac"] != j["ioms"]["iom1"]["mac"]
    assert i2["fw"]["phase"] == "unsupported"
    assert [(p["part"], p["current"]) for p in i2["fw"]["parts"]] == [
        ("BMC", "fbttn-v2019.52.0"), ("ROM", "fbttn-v2.0")]
    assert "BMC Version" not in i2["fw"]["versions"] and i2["fw"]["versions"]["TPM Version"]
    assert i2["fw"]["not_present"] == {"SCC Version": "Server Power-off"}
    assert "fruid-util-iom.txt" not in i2["identity"]["artifacts"] or \
        all(v.startswith("artifacts/") for v in i2["identity"]["artifacts"].values())
    sw = {x["id"]: x for x in s["switches"]}
    a, c = sw["A"], sw["C"]
    assert a["serial"] == "AIFAKEA" and a["identity"]["asset_tag"] == "FAKEASSETA"
    assert a["bmc"]["mac"] and a["bmc"]["ip"] and a["sonic"]["ip"] and a["bmc"]["ip"] != a["sonic"]["ip"]
    assert a["fw"]["versions"] == {"OpenBMC": "wedge100-v29", "CPLD": "6.101"}
    assert c["bmc"] is None and c["serial"] == "CNFAKE0C" and c["identity"]["part_number"]
    assert c["fw"]["versions"] == {} and c["fw"]["headline"]       # a Dell has no BMC: skip
    # the mgmt switch's port table: FDB MACs -> geometry device, link state
    l37p = next(p for p in c["ports"] if any(x["pos"] == "L37" for x in p["devices"]))
    assert l37p["seen"] == ["0c:42:a1:e1:6b:50", "0c:42:a1:e1:6b:52"]
    assert l37p["devices"][0]["link"] == "mezz0" and l37p["oper"] == "up"
    iomp = next(p for p in c["ports"] if any(x["link"] == "iom2" for x in p["devices"]))
    assert iomp["devices"][0]["sel"] == "JBOD01.iom2" and iomp["seen"]
    assert c["ports_from"].startswith("FDB")
    # a wedge's table is its geometry lanes (LLDP is Qualify's, not run here)
    assert any(p["port"] == "28/1" and p["devices"][0]["pos"] == "L05" for p in a["ports"])
    assert a["ports_from"].startswith("geometry lanes")


# ---------------------------------------------------------------- fix wave: Discover confirm, cancel
def test_discover_route_needs_confirm(srv):
    for body in ({}, {"confirm": "rrord-101"}, {"confirm": True}):
        r = post(srv, "/rr101/api/stage/discover", body)
        assert r.status == 400 and b"order id" in r.body, body
    for body in ({"positions": ["L37"]}, {"positions": ["L37"], "confirm": "RRORD-101"}):
        r = post(srv, "/rr101/api/stage/discover", body)
        assert r.status == 400 and b"confirm the positions" in r.body, body
    for pos in (["NOPE"], "L37", []):
        assert post(srv, "/rr101/api/stage/discover",
                    {"positions": pos, "confirm": True}).status == 400, pos
    s = get_json(srv, "/rr101/api/state")
    assert not any(d["runs"] for d in s["devices"])               # nothing started


def test_discover_positions_run_only_that_device(srv):
    r = post(srv, "/rr101/api/stage/discover", {"positions": ["L37"], "confirm": True})
    assert r.status == 202 and json.loads(r.body) == {"started": "discover", "positions": ["L37"]}
    _wait_idle(srv)
    s = get_json(srv, "/rr101/api/state")
    ran = {d["pos"] for d in s["devices"] if d["runs"]}
    assert ran == {"L37"}


def test_cancel_route(srv):
    assert post(srv, "/rr101/api/stage/cancel", {}).status == 400
    assert post(srv, "/rr101/api/stage/cancel", {"confirm": True}).status == 409   # nothing runs
    srv.factory.gate = threading.Event()
    assert post(srv, "/rr101/api/stage/discover", RACK).status == 202
    assert srv.factory.entered.wait(5)
    r = post(srv, "/rr101/api/stage/cancel", {"confirm": True, "stage": "qualify"})   # stale view
    assert r.status == 409 and b"running stage is discover" in r.body
    assert get_json(srv, "/rr101/api/state")["cancelling"] is False
    r = post(srv, "/rr101/api/stage/cancel", {"confirm": True, "stage": "discover"})
    assert r.status == 202 and json.loads(r.body) == {"cancelling": "discover"}
    assert get_json(srv, "/rr101/api/state")["cancelling"] is True
    srv.factory.gate.set()
    _wait_idle(srv)
    s = get_json(srv, "/rr101/api/state")
    assert s["cancelling"] is False
    texts = [e["text"] for e in s["events"]]
    assert "discover cancel requested" in texts and any(t.startswith("discover cancelled") for t in texts)
    l37 = next(d for d in s["devices"] if d["pos"] == "L37")
    assert l37["runs"]["power-on"]["verdict"] == "skip"
    assert l37["runs"]["power-on"]["detail"]["reason"] == "cancelled"
    # the next stage starts with a fresh flag
    assert post(srv, "/rr101/api/stage/discover", {"positions": ["L37"], "confirm": True}).status == 202
    _wait_idle(srv)
    l37 = next(d for d in get_json(srv, "/rr101/api/state")["devices"] if d["pos"] == "L37")
    assert l37["runs"]["power-on"]["detail"].get("reason") != "cancelled"


def test_cli_discover_needs_confirm_or_pos(tmp_path, monkeypatch):
    from flax_rack.__main__ import main
    monkeypatch.chdir(_order_dir(tmp_path))
    with pytest.raises(SystemExit, match="--confirm <order id>"):
        main(["discover"])
    with pytest.raises(SystemExit, match="must be the order id RRORD-101"):
        main(["discover", "--confirm", "RRORD-102"])
    assert not (tmp_path / "RRORD-101" / "order.db").exists()


def test_state_exposes_artifact_paths_per_run(site_srv):
    """The step modal lists a run's artifacts from /api/state: every run row carries an
    `artifacts` map of name -> relative order-dir path, and each one is served."""
    srv = site_srv
    assert post(srv, "/rr101/api/stage/discover", RACK).status == 202
    _wait_idle(srv)
    assert post(srv, "/rr101/api/stage/firmware", {}).status == 202
    _wait_idle(srv)
    s = get_json(srv, "/rr101/api/state")
    holders = [d["runs"] for d in s["devices"]]
    holders += [eps for d in s["devices"] for eps in d["endpoint_runs"].values()]
    holders += [sw["runs"] for sw in s["switches"]] + [s["rack_runs"]]
    runs = [run for h in holders for run in h.values()]
    assert runs and all(isinstance(run["artifacts"], dict) for run in runs)
    paths = [p for run in runs for p in run["artifacts"].values()]
    l37 = next(d for d in s["devices"] if d["pos"] == "L37")
    assert l37["runs"]["serial"]["artifacts"]                   # a Discover step's raw read
    sw_a = next(x for x in s["switches"] if x["id"] == "A")
    assert any(sw_a["runs"][k]["artifacts"] for k in sw_a["runs"])
    for p in paths:
        assert p.startswith("artifacts/") and p.endswith(".txt"), p
        assert get(srv, f"/rr101/{p}").status == 200, p
    assert sw_a["identity"]["artifacts"]                        # the drawer's "raw" line too


# ---------------------------------------------------------------- SOL relay (Plan 5 Task 5)
def _bmc_ip(prefix_dir, pos):
    from flax_rack.addressing import plan_addresses
    g = load_geometry(prefix_dir / "geometry.json")
    return next(a.ip for a in plan_addresses(g) if a.pos == pos and a.role == "bmc")


def test_sol_targets_list_seen_host_bmcs_in_the_relay_shape(srv2, tmp_path):
    get_json(srv2, "/rr101/api/state"); get_json(srv2, "/rr101b/api/state")   # order.db made
    db = OrderDB(tmp_path / "RRORD-101" / "order.db")
    db.set_macs("L37", host_mac="02:00:00:00:37:01", bmc_mac="02:00:00:00:37:03", iom_macs=[])
    db.set_bmc_live("L37", True, "on", "2026-10-30T12:00:00+00:00")
    db.set_macs("R37", host_mac=None, bmc_mac="02:00:00:00:37:33", iom_macs=[])
    slots = get_json(srv2, "/rackpost/api/sol-targets")["slots"]
    ip = _bmc_ip(tmp_path / "RRORD-101", "L37")
    assert slots == [
        {"port": "rackpost:rr101:L37", "bmc_mac": "020000003703", "bmc_ip": ip, "bmc_pinged": True},
        {"port": "rackpost:rr101:R37", "bmc_mac": "020000003733",
         "bmc_ip": _bmc_ip(tmp_path / "RRORD-101", "R37"), "bmc_pinged": False}]


def test_sol_targets_keep_an_unparsed_rack_and_report_closed_disabled(srv2, tmp_path):
    get_json(srv2, "/rr101/api/state"); get_json(srv2, "/rr101b/api/state")
    OrderDB(tmp_path / "RRORD-101" / "order.db").set_macs(
        "L37", host_mac=None, bmc_mac="02:00:00:00:37:03", iom_macs=[])
    geo = tmp_path / "RRORD-101" / "geometry.json"
    good = geo.read_text()
    geo.write_text(good[:-20])                                  # a hand edit left broken
    t = get_json(srv2, "/rackpost/api/sol-targets")
    assert [x["port"] for x in t["slots"]] == ["rackpost:rr101:L37"]   # last good targets
    assert t["racks"] == [{"prefix": "rr101b", "state": "open"},
                          {"prefix": "rr101", "state": "unparsed"}]
    geo.write_text(good)
    (tmp_path / "RRORD-101" / ".rackpost").unlink()
    OrderDB(tmp_path / "RRORD-101b" / "order.db").set_closed()
    t = get_json(srv2, "/rackpost/api/sol-targets")
    assert t["slots"] == []
    assert t["racks"] == [{"prefix": "rr101b", "state": "closed"},
                          {"prefix": "rr101", "state": "disabled"}]


def test_console_tail_reads_the_relay_file_from_an_offset(srv, tmp_path, monkeypatch):
    from flax_rungs import sol
    monkeypatch.setattr(sol, "CAPTURE_DIR", str(tmp_path / "post-sol"))
    (tmp_path / "post-sol").mkdir()
    f = tmp_path / "post-sol" / f"{_bmc_ip(tmp_path / 'RRORD-101', 'L37')}.txt"
    assert get_json(srv, "/rr101/api/console/L37")["ino"] is None      # no capture yet
    f.write_text("=== flax-post mark rackpost-deploy ===\nBooting\n")
    t = get_json(srv, "/rr101/api/console/L37")
    assert t["reset"] and t["text"].endswith("Booting\n")
    with open(f, "a") as o:
        o.write("curtin command install\n")
    t2 = get_json(srv, f"/rr101/api/console/L37?offset={t['offset']}&ino={t['ino']}")
    assert not t2["reset"] and t2["text"] == "curtin command install\n"
    assert get(srv, "/rr101/api/console/JBOD01").status == 404              # no host BMC


def test_post_terminal_assets_served(srv):
    for p in ("sol.js", "vendor/xterm.js", "vendor/xterm.css", "vendor/socket.io.min.js"):
        assert get(srv, f"/rr101/static/{p}").status == 200, p


def test_server_attaches_timeline_to_each_ladder_rung(srv):
    assert post(srv, "/rr101/api/stage/discover", RACK).status == 202
    _wait_idle(srv)
    s = get_json(srv, "/rr101/api/state")
    l37 = next(d for d in s["devices"] if d["pos"] == "L37")
    ladder = ("power-on", "tftp-seen", "ipxe-seen", "live-iso-seen", "host-leased",
              "host-pinged", "host-ssh", "bmc-ready")
    tables = [l37["runs"][k]["detail"]["table"] for k in ladder]
    assert all(t == tables[0] for t in tables)
    t = tables[0]
    assert t["cols"] == ["time", "since on", "event", ""]
    assert t["rows"][0][2] == "power-on" and t["rows"][-1][2].startswith("Discover ended: ")
    assert len(t["row_cls"]) == len(t["rows"]) and t["row_cls"][-1] == "now"
    rows = dict(map(tuple, l37["runs"]["tftp-seen"]["detail"]["rows"]))
    assert rows["budget"] == "600 s"
    assert "table" not in l37["runs"]["serial"]["detail"]
    jbod = next(d for d in s["devices"] if d["pos"] == "JBOD01")
    assert not any("table" in (r.get("detail") or {}) for r in jbod["runs"].values())
    assert not any("table" in (r.get("detail") or {})
                   for eps in jbod["endpoint_runs"].values() for r in eps.values())
    assert not any("table" in (r.get("detail") or {})
                   for sw in s["switches"] for r in sw["runs"].values())


def test_a_malformed_row_degrades_to_its_stored_detail_not_a_500(srv, tmp_path, capsys):
    from flax_rungs.result import RungResult
    get_json(srv, "/rr101/api/state")                                   # order.db made
    d = tmp_path / "RRORD-101"
    (d / "artifacts").mkdir(exist_ok=True)
    (d / "artifacts" / "fio.txt").write_text("")
    db = OrderDB(d / "order.db")
    rid = db.start_run("R37", "qualify", "fio")
    db.finish_run(rid, RungResult("pass", {"summary": "not a dict"}, {"fio": "artifacts/fio.txt"}))
    s = get_json(srv, "/rr101/api/state")                               # still 200
    r37 = next(x for x in s["devices"] if x["pos"] == "R37")
    assert r37["runs"]["fio"]["detail"] == {"summary": "not a dict"}
    get_json(srv, "/rr101/api/state")
    assert capsys.readouterr().err.count("R37: view hook failed") == 1    # logged once
