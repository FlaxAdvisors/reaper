"""flax_post.app renders the post device array and serves JSON + healthz."""
import pytest
from fastapi.testclient import TestClient

_DEV = {"mac": "1c:34:da:7f:b3:a4", "reservation_ip": "172.24.6.101",
        "lease_ip": "172.24.6.101", "hostname": "rr003et6b1-bmc", "vid": 24,
        "switch": "rabbit-edam", "port": "et6b1", "kind": "bmc",
        "leased": True, "lease_expires": None}


@pytest.fixture
def client(monkeypatch):
    from flax_post import app as app_module
    monkeypatch.setattr(app_module.queries, "post_devices", lambda: [dict(_DEV)])
    monkeypatch.setattr(app_module, "_rackname", lambda: "rr003")
    monkeypatch.setattr(app_module.state, "read_settings",
                        lambda: {"order_no": None, "population": None})
    return TestClient(app_module.app)



def test_healthz_ok(monkeypatch):
    from flax_post import app as app_module, db as db_module
    from unittest.mock import MagicMock
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.execute.return_value.fetchone.return_value = (1,)
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(db_module, "get_pool", lambda: pool)
    r = TestClient(app_module.app).get("/api/v1/healthz")
    assert r.status_code == 200
    assert r.json()["db"] == "ok"


def test_healthz_fail(monkeypatch):
    from flax_post import app as app_module, db as db_module

    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(db_module, "get_pool", boom)
    r = TestClient(app_module.app).get("/api/v1/healthz")
    assert r.status_code == 503
    assert r.json()["db"] == "fail"



def test_api_blades_full_grid(monkeypatch):
    from fastapi.testclient import TestClient
    from flax_post import app as app_module
    dev = {"mac": "aa:bb:cc:00:00:01", "reservation_ip": "172.24.6.101",
           "lease_ip": "172.24.6.101", "hostname": "rr003et6b1-bmc", "vid": 24,
           "switch": "rabbit-edam", "port": "et6b1", "kind": "bmc",
           "leased": True, "lease_expires": None}
    geo = {"racks": {"rabbit-edam": {"tag": "", "label": "Post"}},
           "slots": [{"port": "et6b1", "switch": "rabbit-edam", "bottom_ou": 12, "height": 2,
                      "width": 3, "pos": "1:3", "chassis": 2, "ou": 12, "col": "L", "group": 2}]}
    monkeypatch.setattr(app_module.geometry, "load_geometry", lambda: geo)
    monkeypatch.setattr(app_module.queries, "post_devices", lambda: [dev])
    monkeypatch.setattr(app_module.consume, "switch_ports", lambda sw: {})
    monkeypatch.setattr(app_module.state, "read_state", lambda: {})
    monkeypatch.setattr(app_module.state, "read_settings",
                        lambda: {"order_no": "RRORD-003", "population": None})
    body = TestClient(app_module.app).get("/api/v1/blades").json()
    assert body["switch"] == "rabbit-edam"
    blade = next(s for s in body["slots"] if s["port"] == "et6b1")
    assert blade["bmc_mac"] == "aa:bb:cc:00:00:01" and blade["order_no"] == "RRORD-003"
    # the shared operator context rides on every poll (ruling 2026-09-12)
    assert body["settings"] == {"order_no": "RRORD-003", "population": None}
    # the build rides on every poll so an open page can reload itself after a deploy
    assert body["version"] == app_module.__version__


def test_post_settings_writes(monkeypatch):
    from fastapi.testclient import TestClient
    from flax_post import app as app_module
    captured = {}
    monkeypatch.setattr(app_module.state, "set_header",
                        lambda o, f, **kw: captured.update(kw, order_no=o) or [])
    monkeypatch.setattr(app_module.state, "read_settings",
                        lambda: {"order_no": "RRORD-004", "population": None, "customer": "OVH",
                                 "updated_at": "2026-09-12T03:47:40+00:00"})
    r = TestClient(app_module.app).post("/api/v1/settings", json={"order_no": "RRORD-004"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert captured == {"order_no": "RRORD-004"}
    assert r.json()["settings"]["order_no"] == "RRORD-004" and r.json()["settings"]["customer"] == "OVH"


def test_api_profiles(monkeypatch):
    from fastapi.testclient import TestClient
    from flax_post import app as app_module
    monkeypatch.setattr(app_module.population, "list_profiles", lambda: ["ovh-bare-tp", "acme-production"])
    body = TestClient(app_module.app).get("/api/v1/profiles").json()
    assert body["profiles"] == ["ovh-bare-tp", "acme-production"]


def test_index_renders_rack_shell(client):
    body = client.get("/").content
    assert b"Post Servers" in body
    assert b"/web-static/app.js" in body and b'id="app"' in body
    assert b"unpkg.com" not in body                 # vendored, not CDN



def test_rack_shell_vendored_and_controls(client):
    body = client.get("/").content
    assert b"/web-static/petite-vue.iife.js" in body
    assert b"window.BOOT" in body                  # server context island
    # the page knows the build it was rendered by (compared with the poll's)
    assert b'class="live"' in body                 # header connection dot + page build
    from flax_post.version import __version__
    assert ('version: "%s"' % __version__).encode() in body
    assert b'class="ctx"' in body                  # order + population cluster
    assert b'v-model="hdr.order_no"' in body and b'v-model="hdr.population"' in body
    assert b'class="metrics"' in body              # dashboard chips
    assert b'class="title"' in body                # <site> Post Servers (right)


# --- POST /api/v1/power ------------------------------------------------

def test_api_power_happy_path_calls_actions_run_power(monkeypatch):
    from flax_post import app as app_module
    record = {"port": "et6b1", "bmc_ip": "10.0.0.5", "fw": {}}
    monkeypatch.setattr(app_module, "_blade_slots", lambda: [record])
    captured = {}

    def fake_run_power(bmc_ip, action, *, blocked, runner=None):
        captured.update(bmc_ip=bmc_ip, action=action, blocked=blocked)
        return {"ok": True, "action": action, "ip": bmc_ip, "output": "on"}

    monkeypatch.setattr(app_module.actions, "run_power", fake_run_power)
    r = TestClient(app_module.app).post("/api/v1/power", json={"port": "et6b1", "action": "on"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "action": "on", "ip": "10.0.0.5", "output": "on"}
    assert captured == {"bmc_ip": "10.0.0.5", "action": "on", "blocked": False}


def test_api_power_off_blocked_during_flash_returns_409(monkeypatch):
    from flax_post import app as app_module
    record = {"port": "et6b1", "bmc_ip": "10.0.0.5", "fw": {"bmc": {"phase": "flashing"}}}
    monkeypatch.setattr(app_module, "_blade_slots", lambda: [record])
    captured = {}

    def fake_run_power(bmc_ip, action, *, blocked, runner=None):
        captured.update(blocked=blocked)
        return {"ok": False, "blocked": True, "reason": "firmware flash in progress"}

    monkeypatch.setattr(app_module.actions, "run_power", fake_run_power)
    r = TestClient(app_module.app).post("/api/v1/power", json={"port": "et6b1", "action": "off"})
    assert r.status_code == 409
    assert r.json() == {"ok": False, "blocked": True, "reason": "firmware flash in progress"}
    assert captured == {"blocked": True}


def test_api_power_unknown_port_returns_400(monkeypatch):
    from flax_post import app as app_module
    monkeypatch.setattr(app_module, "_blade_slots", lambda: [])
    r = TestClient(app_module.app).post("/api/v1/power", json={"port": "et9z9", "action": "on"})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_api_power_no_bmc_ip_returns_400(monkeypatch):
    from flax_post import app as app_module
    record = {"port": "et6b1", "bmc_ip": None, "fw": {}}
    monkeypatch.setattr(app_module, "_blade_slots", lambda: [record])
    r = TestClient(app_module.app).post("/api/v1/power", json={"port": "et6b1", "action": "on"})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_api_power_invalid_action_returns_400(monkeypatch):
    from flax_post import app as app_module
    record = {"port": "et6b1", "bmc_ip": "10.0.0.5", "fw": {}}
    monkeypatch.setattr(app_module, "_blade_slots", lambda: [record])
    r = TestClient(app_module.app).post("/api/v1/power", json={"port": "et6b1", "action": "nuke"})
    assert r.status_code == 400
    assert r.json()["ok"] is False


# --- POST /api/v1/identify ----------------------------------------------

def test_api_identify_happy_path_calls_actions_run_identify(monkeypatch):
    from flax_post import app as app_module
    record = {"port": "et6b1", "bmc_ip": "10.0.0.5", "fw": {}}
    monkeypatch.setattr(app_module, "_blade_slots", lambda: [record])
    captured = {}

    def fake_run_identify(bmc_ip, mode, *, runner=None):
        captured.update(bmc_ip=bmc_ip, mode=mode)
        return {"ok": True, "mode": mode, "ip": bmc_ip, "output": "identify on"}

    monkeypatch.setattr(app_module.actions, "run_identify", fake_run_identify)
    r = TestClient(app_module.app).post("/api/v1/identify", json={"port": "et6b1", "mode": "on"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "mode": "on", "ip": "10.0.0.5", "output": "identify on"}
    assert captured == {"bmc_ip": "10.0.0.5", "mode": "on"}


def test_api_identify_unknown_port_returns_400(monkeypatch):
    from flax_post import app as app_module
    monkeypatch.setattr(app_module, "_blade_slots", lambda: [])
    r = TestClient(app_module.app).post("/api/v1/identify", json={"port": "et9z9", "mode": "on"})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_api_identify_invalid_mode_returns_400(monkeypatch):
    from flax_post import app as app_module
    record = {"port": "et6b1", "bmc_ip": "10.0.0.5", "fw": {}}
    monkeypatch.setattr(app_module, "_blade_slots", lambda: [record])
    r = TestClient(app_module.app).post("/api/v1/identify", json={"port": "et6b1", "mode": "blink"})
    assert r.status_code == 400
    assert r.json()["ok"] is False


# --- GET /api/v1/inventory/{port} ---------------------------------------
# The INV/POP modal renders from the blade's CURRENT RUN: the post agent runs
# ghost `macinv -p` on the node and uploads the detail form (`macinv-v`) and
# the count form (`macinv`) as the run's `inventory` artifacts. Never triage's
# /export/nodes recon tree: it is keyed by the mezz NIC MAC, which moves
# between blades, so on et28b4 (2026-09-25) its `latest` was another blade.

import datetime as _dt
import os as _os

_RUN_REC = {"port": "et6b1", "host_mac": "50:6b:4b:cd:5e:9a", "bmc_mac": "aa:bb", "run_id": "r9abcdef0123"}
_T1 = _dt.datetime(2026, 9, 24, 10, 0, 0, tzinfo=_dt.timezone.utc)
_T2 = _dt.datetime(2026, 9, 24, 10, 2, 30, tzinfo=_dt.timezone.utc)
_DIMMS = ("  64 GB  DIMM A0  _Node0_Channel0_Dimm0  2666   Samsung  39F0A232  M386A8K40BM2-CTD\n"
          "  64 GB  DIMM A2  _Node0_Channel2_Dimm0  2666   Samsung  39E16042  M386A4G40DM0-CPB\n")
_NO_FULL = "no full inventory in this run (re-run the blade)"


def _macinv_fixture(name):
    with open(_os.path.join(_os.path.dirname(__file__), "fixtures", "macinv", name)) as f:
        return f.read()


def _arts(**content):
    return {name.replace("_", "-"): {"content": text, "captured_at": _T2 if name == "macinv" else _T1}
            for name, text in content.items()}


def _run_blade(monkeypatch, arts, record=None, population="prof"):
    from flax_post import app as app_module
    rec = dict(record or _RUN_REC)
    monkeypatch.setattr(app_module, "_blade_slots", lambda: [rec])
    monkeypatch.setattr(app_module.state, "read_settings", lambda: {"population": population})
    monkeypatch.setattr(app_module.inventory._fm_live, "current", lambda path=None: {})
    calls = []

    def fake_stage(mac, run, stage, names=None):
        calls.append((mac, run, stage, tuple(names or ())))
        return arts
    monkeypatch.setattr(app_module.state, "get_stage_artifacts", fake_stage)
    return app_module, calls


def _green(seen=None):
    return lambda t, p: (seen.append((t, p)) if seen is not None else None) or \
        {"state": "green", "profile": p, "results": [], "missing": []}


def test_api_inventory_sections_parse_the_runs_macinv_v(monkeypatch):
    """A real `macinv -p -v` capture (node-a) as the run's macinv-v artifact:
    the existing parse() renders it, unchanged."""
    app_module, calls = _run_blade(monkeypatch, _arts(**{"macinv_v": _macinv_fixture("node-a.verbose.txt"),
                                                         "macinv": "COUNT", "dimmsum": _DIMMS}))
    seen = []
    monkeypatch.setattr(app_module.inventory, "verdict", _green(seen))
    j = TestClient(app_module.app).get("/api/v1/inventory/et6b1").json()
    assert calls == [("aa:bb", "r9abcdef0123", "inventory", ("macinv-v", "macinv", "dimmsum"))]
    assert j["present"] is True and j["port"] == "et6b1" and "reason" not in j
    assert j["run_id"] == "r9abcdef0123"
    assert j["captured_at"] == "2026-09-24 10:02:30 UTC"      # the newest of the run's rows
    assert j["sections"] == app_module.inventory.parse(_macinv_fixture("node-a.verbose.txt"))
    assert j["sections"]["bmc"]["mac"] == "50:6b:4b:cd:5e:9c"
    assert j["sections"]["fru"]["serial"] == "WTH4908267M1A"
    assert j["pop"] == {"state": "green", "profile": "prof", "results": [], "missing": [],
                        "source": "run", "run_id": "r9abcdef0123"}
    assert seen == [("COUNT", "prof")]                           # POP judges the count form, as before
    assert "pop_export" not in j and "dir" not in j



def _clone_blocklist(monkeypatch, tmp_path):
    """The shared blocklist-parts.json (no built-in list since 0.9.174),
    holding the cloned-SPD signature these tests judge against."""
    f = tmp_path / "blocklist-parts.json"
    f.write_text('{"dimms": [{"serial": "39E16042", "part": "M386A4G40DM0-CPB", "mfg": "Samsung", "reason": "cloned SPD"}]}')
    monkeypatch.setenv("FLAX_POST_BLOCKLIST", str(f))

def test_api_inventory_run_without_macinv_v_says_rerun_and_never_falls_back(monkeypatch, tmp_path):
    _clone_blocklist(monkeypatch, tmp_path)
    """Every run before the agent uploads macinv-v: empty tables and a reason,
    never a fallback to /export/nodes and never a rebuild."""
    app_module, _ = _run_blade(monkeypatch, _arts(macinv="COUNT", dimmsum=_DIMMS))
    monkeypatch.setattr(app_module.inventory, "verdict", _green())
    j = TestClient(app_module.app).get("/api/v1/inventory/et6b1").json()
    assert j["present"] is True and j["reason"] == _NO_FULL
    assert j["sections"] == app_module.inventory.parse("")
    assert j["pop"]["state"] == "green"                          # the count form still judges POP
    assert [b["slot"] for b in j["blocked"]] == ["DIMM A2"]      # dimmsum still judges blocked


def test_api_inventory_blocked_banner_reads_the_runs_dimmsum(monkeypatch, tmp_path):
    _clone_blocklist(monkeypatch, tmp_path)
    app_module, _ = _run_blade(monkeypatch, _arts(**{"macinv_v": _macinv_fixture("node-a.verbose.txt"),
                                                     "macinv": "COUNT", "dimmsum": _DIMMS}))
    monkeypatch.setattr(app_module.inventory, "verdict", _green())
    j = TestClient(app_module.app).get("/api/v1/inventory/et6b1").json()
    assert [b["slot"] for b in j["blocked"]] == ["DIMM A2"]


def test_api_inventory_blocked_without_dimmsum_judges_the_runs_memory_table(monkeypatch, tmp_path):
    _clone_blocklist(monkeypatch, tmp_path)
    app_module, _ = _run_blade(monkeypatch, _arts(**{"macinv_v": "V", "macinv": "COUNT"}))
    monkeypatch.setattr(app_module.inventory, "parse", lambda text: {"memory": [
        {"slot": "DIMM A0", "serial": "39E16042", "part": "M386A4G40DM0-CPB", "mfg": "Samsung", "size": "64 GB"},
        {"slot": "DIMM A1", "serial": "39F0A232", "part": "M386A8K40BM2-CTD", "mfg": "Samsung", "size": "64 GB"}]})
    monkeypatch.setattr(app_module.inventory, "verdict", _green())
    j = TestClient(app_module.app).get("/api/v1/inventory/et6b1").json()
    assert [b["slot"] for b in j["blocked"]] == ["DIMM A0"]


def test_api_inventory_missing_count_form_is_grey_pop(monkeypatch):
    app_module, _ = _run_blade(monkeypatch, _arts(**{"macinv_v": "V"}))
    monkeypatch.setattr(app_module.inventory, "verdict",
                        lambda t, p: (_ for _ in ()).throw(AssertionError("no count form -> no verdict")))
    j = TestClient(app_module.app).get("/api/v1/inventory/et6b1").json()
    assert j["pop"]["state"] == "grey" and j["pop"]["reason"] == "no population digest in this run"


def test_api_inventory_no_run_says_so(monkeypatch):
    rec = dict(_RUN_REC, run_id=None)
    app_module, calls = _run_blade(monkeypatch, {}, record=rec)
    r = TestClient(app_module.app).get("/api/v1/inventory/et6b1")
    assert r.status_code == 200
    assert r.json() == {"present": False, "port": "et6b1", "reason": "no run"}
    assert calls == []


def test_api_inventory_artifact_read_failure_is_not_a_500(monkeypatch):
    app_module, _ = _run_blade(monkeypatch, {})

    def boom(*a, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(app_module.state, "get_stage_artifacts", boom)
    r = TestClient(app_module.app).get("/api/v1/inventory/et6b1")
    assert r.status_code == 200
    assert r.json() == {"present": False, "port": "et6b1", "reason": "artifact read failed",
                        "run_id": "r9abcdef0123"}


def test_api_inventory_query_profile_overrides_settings(monkeypatch):
    app_module, _ = _run_blade(monkeypatch, _arts(macinv="RUN-COUNT"), population="settings-profile")
    seen = []
    monkeypatch.setattr(app_module.inventory, "verdict", _green(seen))
    TestClient(app_module.app).get("/api/v1/inventory/et6b1?profile=what-if")
    assert seen == [("RUN-COUNT", "what-if")]


def test_api_inventory_never_reads_the_export_dump(monkeypatch, tmp_path):
    """A recon dump on disk for this blade's host_mac is ignored: no run ->
    'no run'; a run without macinv-v -> empty tables, never the dump's."""
    dump = tmp_path / "post-506b4bcd5e9a" / "latest"
    dump.mkdir(parents=True)
    (dump / "dmidecode.txt").write_text("Memory Device\n")
    monkeypatch.setenv("FLAX_POST_NODES_ROOT", str(tmp_path))
    app_module, _ = _run_blade(monkeypatch, {}, record=dict(_RUN_REC, run_id=None))
    assert not hasattr(app_module.inventory, "capture")
    assert not hasattr(app_module, "_dump_stamp")
    j = TestClient(app_module.app).get("/api/v1/inventory/et6b1").json()
    assert j == {"present": False, "port": "et6b1", "reason": "no run"}
    app_module, _ = _run_blade(monkeypatch, _arts(macinv="COUNT"))
    monkeypatch.setattr(app_module.inventory, "verdict", _green())
    j = TestClient(app_module.app).get("/api/v1/inventory/et6b1").json()
    assert j["sections"]["memory"] == [] and j["reason"] == _NO_FULL


def test_api_inventory_unknown_port_returns_404(monkeypatch):
    from flax_post import app as app_module
    monkeypatch.setattr(app_module, "_blade_slots", lambda: [])
    r = TestClient(app_module.app).get("/api/v1/inventory/et9z9")
    assert r.status_code == 404


def test_rack_template_drops_pop_export_and_shows_the_run(client):
    html = client.get("/").text
    assert "pop_export" not in html and "/export dump" not in html and "inv.dir" not in html
    assert "inv.run_id" in html and "inv.captured_at" in html and "inv.reason" in html
    assert "no run yet" in html


def test_api_step_returns_the_stepinfo_block(monkeypatch):
    from flax_post import app as app_module
    record = {"port": "et6b1", "steps": {"Discover": {"serial": "done"}}, "serial": "SN1",
              "ladder": {}, "ladder_view": {}, "fault_notes": {}, "step_notes": {}}
    monkeypatch.setattr(app_module, "_blade_slots", lambda: [record])
    monkeypatch.setattr(app_module.state, "read_state", lambda: {"et6b1": {}})
    j = TestClient(app_module.app).get("/api/v1/step?port=et6b1&phase=Discover&step=serial").json()
    assert j["status"] == "done" and j["headline"] == "serial: passed"
    assert ["serial", "SN1"] in j["rows"]
    assert TestClient(app_module.app).get("/api/v1/step?port=zz&phase=Discover&step=serial").status_code == 404


def test_static_assets_revalidate_but_vendor_does_not():
    from flax_post import app as app_module
    c = TestClient(app_module.app)
    assert c.get("/web-static/app.js").headers.get("cache-control") == "no-cache"
    assert c.get("/web-static/vendor/xterm.css").headers.get("cache-control") != "no-cache"




def test_api_step_fio_reads_this_runs_artifacts_for_the_table(monkeypatch):
    from flax_post import app as app_module
    from test_flax_post_stepinfo import _fio_case
    rec, row, get = _fio_case()
    rec = dict(rec, bmc_mac="aa:bb", run_id="r1")
    seen = []
    monkeypatch.setattr(app_module, "_blade_slots", lambda: [rec])
    monkeypatch.setattr(app_module.state, "read_state", lambda: {"et6b1": row})
    monkeypatch.setattr(app_module.state, "get_artifact",
                        lambda mac, run, stage, name: seen.append((mac, run)) or get(stage, name))
    j = TestClient(app_module.app).get("/api/v1/step?port=et6b1&phase=Qualify&step=fio").json()
    assert j["flag"] is True and j["table"]["cols"][-1] == "check"
    assert set(seen) == {("aa:bb", "r1")}


# --- per-node order UI (2026-11-06) -------------------------------------------
def test_orders_endpoint_lists_open_orders(client, monkeypatch):
    from flax_post import app as app_module
    monkeypatch.setattr(app_module.state, "list_orders",
                        lambda **k: [{"order_no": "RRORD-1033", "sales": None, "customer": "Acme",
                                      "opened_at": None, "finished_at": None}])
    monkeypatch.setattr(app_module.state, "read_settings", lambda: {"order_no": "RRORD-100"})
    r = client.get("/api/v1/orders")
    assert r.status_code == 200
    assert r.json() == {"orders": [{"order_no": "RRORD-1033", "sales": None, "customer": "Acme",
                                    "opened_at": None, "finished_at": None}], "header": "RRORD-100"}


def test_orders_include_finished(client, monkeypatch):
    from flax_post import app as app_module
    seen = []
    monkeypatch.setattr(app_module.state, "list_orders",
                        lambda **k: seen.append(k) or [])
    monkeypatch.setattr(app_module.state, "read_settings", lambda: {"order_no": None})
    client.get("/api/v1/orders")
    client.get("/api/v1/orders?include_finished=1")
    assert seen == [{"include_finished": False}, {"include_finished": True}]


def _finish_ctx_world(monkeypatch, row):
    from flax_post import app as app_module
    run = {"bmc_mac": "aa:01", "serial": "SN1", "run_id": "r1", "verdict": "pass",
           "finished_at": 1, "port": "et7b1", "compact": False,
           "result": {"verdict": "pass", "sales": "S-ORD101033", "customer": "Barivia"}}
    monkeypatch.setattr(app_module.state, "runs_under", lambda o: [run])
    monkeypatch.setattr(app_module, "_blade_slots", lambda: [])
    monkeypatch.setattr(app_module.state, "order_row", lambda o: row)
    monkeypatch.setattr(app_module.state, "read_settings",
                        lambda: {"order_no": "RRORD-100", "sales": "S-HDR", "customer": "hdr"})


def test_finish_preview_reads_record_only(client, monkeypatch):
    _finish_ctx_world(monkeypatch, {})
    j = client.get("/api/v1/orders/finish-preview?order=RRORD-100").json()
    assert (j["sales"], j["customer"]) == (None, None)


def test_finish_csv_names_follow_record_after_label_edit(client, monkeypatch):
    _finish_ctx_world(monkeypatch, {"sales": "S-100", "customer": "OVH", "population": None,
                                    "finished_at": None})
    j = client.get("/api/v1/orders/finish-preview?order=RRORD-100").json()
    assert (j["sales"], j["customer"]) == ("S-100", "OVH")


def test_index_boot_has_sales(client, monkeypatch):
    from flax_post import app as app_module
    monkeypatch.setattr(app_module.state, "read_settings",
                        lambda: {"order_no": "RRORD-100", "population": None, "sales": "S-1"})
    html = client.get("/").text
    for s in ("sales:", 'name="post-sales"', 'name="assign-order"', 'id="post-open-orders"'):
        assert s in html, s


def _ui(client, monkeypatch):
    from flax_post import app as app_module
    monkeypatch.setattr(app_module.state, "read_settings",
                        lambda: {"order_no": "RRORD-100", "population": None, "sales": "S-1"})
    return client.get("/web-static/app.js").text, client.get("/").text


def test_header_has_no_per_field_save(client, monkeypatch):
    js, html = _ui(client, monkeypatch)
    import re
    for name in ("saveSales", "saveCustomer", "savePopulation", "saveOrder", "clearCustomer",
                 "clearOrder"):
        pat = re.compile(r"\b" + name + r"\b")
        assert not pat.search(js) and not pat.search(html), name


def test_header_set_posts_settings_once(client, monkeypatch):
    js, html = _ui(client, monkeypatch)
    assert "setHeader(" in js and "cancelHeader(" in js and "onHdrOrder(" in js
    assert '@click="setHeader()"' in html and '@click="cancelHeader()"' in html
    assert 'v-model="hdr.order_no"' in html and '@change="onHdrOrder()"' in html


def test_dirty_form_survives_poll(client, monkeypatch):
    js, _ = _ui(client, monkeypatch)
    body = js[js.index("applySettings(s) {"):]
    body = body[:body.index("\n    },")]
    assert "hdrDirty()" in body and "hdrElsewhere" in body
    assert "include_finished=1" in client.get("/web-static/api.js").text


def test_static_app_js_wires_assign(client):
    js = client.get("/web-static/app.js").text
    assert "postAssign(" in js and "'move'" in js


def test_assign_posts_order_and_population_only(client):
    js = client.get("/web-static/app.js").text
    call = js[js.index("postAssign({ port: this.sel.port, order_no"):]
    call = call[:call.index("})")]
    assert "population" in call and "sales" not in call and "customer" not in call


def test_drawer_labels_read_only_with_edit(client, monkeypatch):
    js, html = _ui(client, monkeypatch)
    assert "postOrderLabels(" in js and "saveOrderLabels(" in js
    assert 'name="assign-sales"' not in html and 'name="assign-customer"' not in html
    assert '@click="saveOrderLabels()"' in html and "all nodes on this order" in html
    assert "re-Finish to update the archive" in js


def _fin(monkeypatch, left, send=None, mems=None):
    from flax_post import app as app_module
    marked = []
    m = {"serial": "SN1", "bmc_mac": "aa:01", "run_id": "r1", "verdict": "pass", "finished_at": 1}
    monkeypatch.setattr(app_module, "_finish_context",
                        lambda o: ([m] if mems is None else mems, left, [], "Acme", None))
    monkeypatch.setattr(app_module.finish, "build_payload", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(app_module.finish, "send", send or (lambda p: {"devices": 1}))
    monkeypatch.setattr(app_module.state, "mark_finished", lambda *a, **k: marked.append(a))
    return marked


def test_finish_archives_done_nodes_and_never_asks_about_left_out(client, monkeypatch):
    marked = _fin(monkeypatch, [{"serial": "SN2", "why": "fail"}])
    r = client.post("/api/v1/orders/finish", json={"order_no": "RRORD-1033", "customer": "Acme"})
    assert r.status_code == 200 and r.json()["left_out"] == 1 and len(marked) == 1


def test_finish_refuses_when_no_node_is_done(client, monkeypatch):
    marked = _fin(monkeypatch, [{"serial": "SN2", "why": "fail"}], mems=[])
    r = client.post("/api/v1/orders/finish", json={"order_no": "RRORD-1033", "customer": "Acme"})
    assert r.status_code == 400 and marked == []


def test_finish_marks_finished_only_after_send_ok(client, monkeypatch):
    from flax_post import finish

    def boom(p):
        raise finish.FinishError(502, "down")
    marked = _fin(monkeypatch, [], send=boom)
    r = client.post("/api/v1/orders/finish", json={"order_no": "RRORD-1033", "customer": "Acme"})
    assert r.status_code == 502 and marked == []
    marked = _fin(monkeypatch, [])
    r = client.post("/api/v1/orders/finish", json={"order_no": "RRORD-1033", "customer": "Acme"})
    assert r.status_code == 200 and len(marked) == 1


def test_finish_requires_customer(client, monkeypatch):
    _fin(monkeypatch, [])
    r = client.post("/api/v1/orders/finish", json={"order_no": "RRORD-1033", "customer": " "})
    assert r.status_code == 400
