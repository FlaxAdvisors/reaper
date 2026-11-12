# tests/test_flax_post_app_settings.py
from starlette.testclient import TestClient
from flax_post import app as app_mod


def test_settings_endpoint_forwards_customer(monkeypatch):
    seen = {}
    monkeypatch.setattr(app_mod.state, "write_settings", lambda **kw: seen.update(kw))
    monkeypatch.setattr(app_mod.state, "read_settings",
                        lambda: {"order_no": "RRORD-003", "population": None, "customer": "acme", "updated_at": None})
    monkeypatch.setattr(app_mod.state, "touch_header_labels", lambda *a, **k: None)
    c = TestClient(app_mod.app)
    r = c.post("/api/v1/settings", json={"customer": "acme", "order_no": "RRORD-003"})
    assert r.status_code == 200
    assert seen == {"customer": "acme", "order_no": "RRORD-003"}


def _slots(serial="SN1"):
    return [{"port": "et8b1", "serial": serial, "bmc_mac": "aa:bb:cc:00:00:01"}]


def _hdr(order="RRORD-1033"):
    return {"order_no": order, "population": None, "customer": None, "sales": None, "updated_at": None}


def test_settings_order_change_needs_confirm(monkeypatch):
    monkeypatch.setattr(app_mod.state, "read_settings", lambda: _hdr())
    monkeypatch.setattr(app_mod.state, "read_state", lambda: {"et8b1": {"bmc_mac": "aa:bb:cc:00:00:01",
                                                                        "serial": "SN1", "order_no": "RRORD-1033"}})
    monkeypatch.setattr(app_mod.state, "read_nodes", lambda *a: {})
    r = TestClient(app_mod.app).post("/api/v1/settings", json={"order_no": "RRORD-100"})
    assert r.status_code == 409
    assert r.json()["confirm"] == [{"port": "et8b1", "serial": "SN1", "from": "RRORD-1033", "to": "RRORD-100"}]


def test_settings_order_change_confirmed_applies(monkeypatch):
    calls = []
    monkeypatch.setattr(app_mod.state, "read_settings", lambda: _hdr())
    monkeypatch.setattr(app_mod.state, "read_state", lambda: {"et8b1": {"bmc_mac": "aa:bb:cc:00:00:01",
                                                                        "serial": "SN1", "order_no": "RRORD-1033"}})
    monkeypatch.setattr(app_mod.state, "read_nodes", lambda *a: {})
    monkeypatch.setattr(app_mod.state, "apply_header_order", lambda o, f, **k: calls.append((o, f)) or [])
    r = TestClient(app_mod.app).post("/api/v1/settings", json={"order_no": "RRORD-100", "confirm": True})
    assert r.status_code == 200 and len(calls) == 1 and calls[0][0] == "RRORD-100"


def test_assign_refuses_tile_without_serial(monkeypatch):
    monkeypatch.setattr(app_mod, "_blade_slots", lambda: _slots(serial=None))
    r = TestClient(app_mod.app).post("/api/v1/assign", json={"port": "et8b1", "order_no": "RRORD-1033"})
    assert r.status_code == 400 and r.json()["reason"] == "no serial"


def test_assign_ok_returns_effective(monkeypatch):
    monkeypatch.setattr(app_mod, "_blade_slots", lambda: _slots())
    monkeypatch.setattr(app_mod.state, "read_settings", lambda: _hdr("RRORD-100"))
    monkeypatch.setattr(app_mod.state, "assign_node", lambda b, s, a, st: {
        "effective": {"order_no": a["order_no"]}, "moved": {}})
    r = TestClient(app_mod.app).post("/api/v1/assign", json={"port": "et8b1", "order_no": "RRORD-1033",
                                                             "customer": "Acme"})
    assert r.status_code == 200 and r.json()["effective"]["order_no"] == "RRORD-1033"
