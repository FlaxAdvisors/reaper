# tests/test_flax_post_app_settings.py
from starlette.testclient import TestClient
from flax_post import app as app_mod


def _set_header_spy(monkeypatch, calls):
    monkeypatch.setattr(app_mod.state, "set_header",
                        lambda o, f, **kw: calls.append((o, f, kw)) or [])


def test_settings_set_sends_all_four_in_one_call(monkeypatch):
    calls = []
    _set_header_spy(monkeypatch, calls)
    monkeypatch.setattr(app_mod.state, "read_settings", lambda: _hdr("RRORD-003"))
    r = TestClient(app_mod.app).post("/api/v1/settings", json={
        "order_no": "RRORD-003", "sales": "S-3", "customer": "acme", "population": "p3"})
    assert r.status_code == 200
    assert calls == [("RRORD-003", [], {"sales": "S-3", "customer": "acme", "population": "p3",
                                        "expect_order": "RRORD-003"})]


def test_settings_sales_alone_targets_header_order(monkeypatch):
    calls = []
    _set_header_spy(monkeypatch, calls)
    monkeypatch.setattr(app_mod.state, "read_settings", lambda: _hdr("RRORD-100"))
    r = TestClient(app_mod.app).post("/api/v1/settings", json={"sales": "S-100"})
    assert r.status_code == 200
    assert calls == [("RRORD-100", [], {"sales": "S-100", "expect_order": "RRORD-100"})]


def test_settings_rejects_bad_sales(monkeypatch):
    calls = []
    _set_header_spy(monkeypatch, calls)
    monkeypatch.setattr(app_mod.state, "read_settings", lambda: _hdr("RRORD-100"))
    r = TestClient(app_mod.app).post("/api/v1/settings", json={"sales": "S 100/x"})
    assert r.status_code == 400 and calls == []


def test_settings_409_lists_values_to_set(monkeypatch):
    monkeypatch.setattr(app_mod.state, "read_settings", lambda: _hdr())
    monkeypatch.setattr(app_mod.state, "read_state", lambda: {"et8b1": {"bmc_mac": "aa:bb:cc:00:00:01",
                                                                        "serial": "SN1", "order_no": "RRORD-1033"}})
    monkeypatch.setattr(app_mod.state, "read_nodes", lambda *a: {})
    monkeypatch.setattr(app_mod.state, "order_row", lambda o: {"sales": "S-100", "customer": "OVH",
                                                               "population": "ovh-bare-tp"})
    r = TestClient(app_mod.app).post("/api/v1/settings", json={"order_no": "RRORD-100", "customer": "ovh"})
    assert r.status_code == 409
    assert r.json()["set"] == {"order_no": "RRORD-100", "sales": "S-100", "customer": "ovh",
                               "population": "ovh-bare-tp"}


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
    monkeypatch.setattr(app_mod.state, "set_header", lambda o, f, **k: calls.append((o, f)) or [])
    r = TestClient(app_mod.app).post("/api/v1/settings", json={"order_no": "RRORD-100", "confirm": True})
    assert r.status_code == 200 and len(calls) == 1 and calls[0][0] == "RRORD-100"


def test_assign_refuses_tile_without_serial(monkeypatch):
    monkeypatch.setattr(app_mod, "_blade_slots", lambda: _slots(serial=None))
    r = TestClient(app_mod.app).post("/api/v1/assign", json={"port": "et8b1", "order_no": "RRORD-1033"})
    assert r.status_code == 400 and r.json()["reason"] == "no serial"


def test_assign_ok_returns_effective(monkeypatch):
    monkeypatch.setattr(app_mod, "_blade_slots", lambda: _slots())
    monkeypatch.setattr(app_mod.state, "read_settings", lambda: _hdr("RRORD-100"))
    monkeypatch.setattr(app_mod.state, "order_labels", lambda: {})
    monkeypatch.setattr(app_mod.state, "assign_node", lambda b, s, a, st, orders=None: {
        "effective": {"order_no": a["order_no"]}, "moved": {}})
    r = TestClient(app_mod.app).post("/api/v1/assign", json={"port": "et8b1", "order_no": "RRORD-1033",
                                                             "customer": "Acme"})
    assert r.status_code == 200 and r.json()["effective"]["order_no"] == "RRORD-1033"


def test_assign_ignores_sales_and_customer(monkeypatch):
    seen = []
    monkeypatch.setattr(app_mod, "_blade_slots", lambda: _slots())
    monkeypatch.setattr(app_mod.state, "read_settings", lambda: _hdr("RRORD-100"))
    monkeypatch.setattr(app_mod.state, "order_labels", lambda: {})
    monkeypatch.setattr(app_mod.state, "assign_node", lambda b, s, a, st, orders=None: seen.append(a) or {
        "effective": {"order_no": a["order_no"]}, "moved": {}})
    r = TestClient(app_mod.app).post("/api/v1/assign", json={
        "port": "et8b1", "order_no": "RRORD-1033", "sales": "S-ORD101033", "customer": "Acme",
        "population": "barivia-leo"})
    assert r.status_code == 200
    assert seen == [{"order_no": "RRORD-1033", "population": "barivia-leo"}]


def _labels_spy(monkeypatch, finished=None):
    calls = []
    monkeypatch.setattr(app_mod.state, "set_order_labels",
                        lambda o, **kw: calls.append((o, kw)))
    monkeypatch.setattr(app_mod.state, "order_row",
                        lambda o: {"sales": None, "customer": None, "population": None,
                                   "finished_at": finished})
    monkeypatch.setattr(app_mod.state, "order_exists", lambda o: True)
    return calls


def test_order_labels_endpoint_validates_sales(monkeypatch):
    calls = _labels_spy(monkeypatch)
    c = TestClient(app_mod.app)
    assert c.post("/api/v1/orders/labels", json={"order_no": "RRORD-100", "sales": "bad sales!"}
                  ).status_code == 400
    assert c.post("/api/v1/orders/labels", json={"sales": "S-1"}).status_code == 400
    assert c.post("/api/v1/orders/labels", json={"order_no": "RRORD-100", "customer": "x" * 65}
                  ).status_code == 400
    assert calls == []
    r = c.post("/api/v1/orders/labels", json={"order_no": "RRORD-100", "sales": "S-1",
                                              "customer": "", "population": "ovh-bare-tp"})
    assert r.status_code == 200 and r.json() == {"ok": True, "finished": False}
    assert calls == [("RRORD-100", {"sales": "S-1", "customer": None, "population": "ovh-bare-tp"})]


def test_order_labels_on_finished_order_says_so(monkeypatch):
    _labels_spy(monkeypatch, finished="2026-11-12T00:00:00+00:00")
    r = TestClient(app_mod.app).post("/api/v1/orders/labels", json={"order_no": "RRORD-100",
                                                                     "customer": "OVH"})
    assert r.status_code == 200 and r.json()["finished"] is True


def test_order_labels_refuses_an_order_that_does_not_exist(monkeypatch):
    calls = _labels_spy(monkeypatch)
    monkeypatch.setattr(app_mod.state, "order_exists", lambda o: o == "RRORD-100")
    c = TestClient(app_mod.app)
    r = c.post("/api/v1/orders/labels", json={"order_no": "RRORD-1O0", "sales": "S-1"})
    assert r.status_code == 404 and "Assign or Set" in r.json()["reason"] and calls == []
    assert c.post("/api/v1/orders/labels", json={"order_no": "RRORD-100", "sales": "S-1"}
                  ).status_code == 200


def test_settings_header_moved_is_a_409(monkeypatch):
    def moved(o, f, **kw):
        assert kw.get("expect_order") == "RRORD-100"
        raise app_mod.state.HeaderMoved()
    monkeypatch.setattr(app_mod.state, "set_header", moved)
    monkeypatch.setattr(app_mod.state, "read_settings", lambda: _hdr("RRORD-100"))
    r = TestClient(app_mod.app).post("/api/v1/settings", json={"sales": "S-100"})
    assert r.status_code == 409 and r.json()["reason"] == "header changed elsewhere; reload"


def test_settings_runs_off_the_event_loop():
    import inspect
    assert not inspect.iscoroutinefunction(app_mod._apply_settings)
