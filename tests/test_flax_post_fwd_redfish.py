# tests/test_flax_post_fwd_redfish.py
"""flax_post.fwd.redfish: request shapes + response parsing, via a fake HTTPSConnection."""
import json
import pytest


class _FakeResp:
    def __init__(self, status, body, headers=None):
        self.status = status
        self._b = body if isinstance(body, bytes) else body.encode()
        self._h = headers or {}
    def read(self): return self._b
    def getheader(self, name, default=None):
        for k, v in self._h.items():
            if k.lower() == name.lower():
                return v
        return default


class _FakeConn:
    """Scripts responses by (method, path-prefix). Records requests for assertions."""
    def __init__(self, script, sink):
        self._script = script; self._sink = sink
    def request(self, method, path, body=None, headers=None):
        self._sink.append({"method": method, "path": path, "body": body, "headers": headers or {}})
        self._pending = (method, path)
    def getresponse(self):
        method, path = self._pending
        for (m, prefix), resp in self._script:
            if m == method and path.startswith(prefix):
                return resp
        return _FakeResp(404, b"{}")
    def close(self): pass


def _patch(monkeypatch, script, sink):
    from flax_post.fwd import redfish
    monkeypatch.setattr(redfish.http.client, "HTTPSConnection",
                        lambda host, timeout=None, context=None: _FakeConn(script, sink))
    return redfish


_CREDS = [{"bmcuser": "root", "bmcpass": "test-obmc-pw"}]


def test_get_fw_version(monkeypatch):
    sink = []
    script = [(("GET", "/redfish/v1/UpdateService/FirmwareInventory/bmc_active"),
               _FakeResp(200, json.dumps({"Version": "flax-onetree-1.0.6-202606221107"})))]
    redfish = _patch(monkeypatch, script, sink)
    c = redfish.RedfishClient("172.24.6.101", _CREDS)
    ver, _ = c.get_fw_version("bmc_active")
    assert ver == "flax-onetree-1.0.6-202606221107"
    assert sink[0]["headers"]["Authorization"].startswith("Basic ")


def test_get_power_state_resolves_systems_member(monkeypatch):
    sink = []
    script = [
        (("GET", "/redfish/v1/Systems/system"), _FakeResp(200, json.dumps({"PowerState": "Off", "Model": "Tioga Pass"}))),
        (("GET", "/redfish/v1/Systems"), _FakeResp(200, json.dumps({"Members": [{"@odata.id": "/redfish/v1/Systems/system"}]}))),
    ]
    redfish = _patch(monkeypatch, script, sink)
    c = redfish.RedfishClient("172.24.6.101", _CREDS)
    state, _ = c.get_power_state()
    assert state == "Off"


def test_get_product_name(monkeypatch):
    sink = []
    script = [
        (("GET", "/redfish/v1/Systems/system"), _FakeResp(200, json.dumps({"Model": "Tioga Pass", "Manufacturer": "Wiwynn"}))),
        (("GET", "/redfish/v1/Systems"), _FakeResp(200, json.dumps({"Members": [{"@odata.id": "/redfish/v1/Systems/system"}]}))),
    ]
    redfish = _patch(monkeypatch, script, sink)
    name, _ = redfish.RedfishClient("172.24.6.101", _CREDS).get_product_name()
    assert "Tioga Pass" in name and "Wiwynn" in name


def test_post_flash_sends_octet_stream_and_extracts_task_from_location(monkeypatch):
    # flax-onetree bmcweb resets multipart/form-data; the working push is the raw
    # image as application/octet-stream (HttpPushUri), task id from the Location header.
    sink = []
    script = [(("POST", "/redfish/v1/UpdateService/update"),
               _FakeResp(202, b"", headers={"Location": "/redfish/v1/TaskService/Tasks/7"}))]
    redfish = _patch(monkeypatch, script, sink)
    c = redfish.RedfishClient("172.24.6.101", _CREDS)
    task, _ = c.post_flash(b"IMAGEBYTES", "fx-tp.mtd.tar")
    assert task == "7"
    req = sink[0]
    assert req["headers"]["Content-Type"] == "application/octet-stream"
    assert req["body"] == b"IMAGEBYTES"                 # raw image, not multipart
    assert b"UpdateParameters" not in (req["body"] or b"")


def test_post_flash_falls_back_to_body_odata_id(monkeypatch):
    # Some bmcweb builds return the task in the body @odata.id without a Location header.
    sink = []
    script = [(("POST", "/redfish/v1/UpdateService/update"),
               _FakeResp(202, json.dumps({"@odata.id": "/redfish/v1/TaskService/Tasks/9"})))]
    redfish = _patch(monkeypatch, script, sink)
    task, _ = redfish.RedfishClient("172.24.6.101", _CREDS).post_flash(b"IMG", "x.tar")
    assert task == "9"


def test_post_flash_no_task_is_a_fault(monkeypatch):
    sink = []
    script = [(("POST", "/redfish/v1/UpdateService/update"), _FakeResp(202, b""))]
    redfish = _patch(monkeypatch, script, sink)
    task, detail = redfish.RedfishClient("172.24.6.101", _CREDS).post_flash(b"IMG", "x.tar")
    assert task is None and "task" in detail.lower()


def test_poll_task(monkeypatch):
    sink = []
    script = [(("GET", "/redfish/v1/TaskService/Tasks/7"),
               _FakeResp(200, json.dumps({"TaskState": "New", "PercentComplete": 42})))]
    redfish = _patch(monkeypatch, script, sink)
    state, pct, _ = redfish.RedfishClient("172.24.6.101", _CREDS).poll_task("7")
    assert state == "New" and pct == 42


def test_auth_failure_returns_none(monkeypatch):
    sink = []
    script = [(("GET", "/redfish/v1/UpdateService/FirmwareInventory/bmc_active"), _FakeResp(401, b"{}"))]
    redfish = _patch(monkeypatch, script, sink)
    ver, detail = redfish.RedfishClient("172.24.6.101", _CREDS).get_fw_version("bmc_active")
    assert ver is None and "401" in detail


def test_no_creds_returns_none(monkeypatch):
    from flax_post.fwd import redfish
    ver, detail = redfish.RedfishClient("172.24.6.101", []).get_fw_version("bmc_active")
    assert ver is None and "credential" in detail.lower()


def test_get_serial_from_systems_member(monkeypatch):
    sink = []
    script = [
        (("GET", "/redfish/v1/Systems/system"), _FakeResp(200, json.dumps({"SerialNumber": "WTH12345", "Model": ""}))),
        (("GET", "/redfish/v1/Systems"), _FakeResp(200, json.dumps({"Members": [{"@odata.id": "/redfish/v1/Systems/system"}]}))),
    ]
    redfish = _patch(monkeypatch, script, sink)
    serial, _ = redfish.RedfishClient("172.25.25.102", _CREDS).get_serial()
    assert serial == "WTH12345"


def test_get_serial_falls_back_to_chassis_when_systems_blank(monkeypatch):
    # SMBIOS-backed Systems.SerialNumber is blank when the host is off; the
    # BMC-resident Chassis.SerialNumber still answers.
    sink = []
    script = [
        (("GET", "/redfish/v1/Systems/system"), _FakeResp(200, json.dumps({"SerialNumber": "  "}))),
        (("GET", "/redfish/v1/Systems"), _FakeResp(200, json.dumps({"Members": [{"@odata.id": "/redfish/v1/Systems/system"}]}))),
        (("GET", "/redfish/v1/Chassis/chassis"), _FakeResp(200, json.dumps({"SerialNumber": "WTHCHAS9"}))),
        (("GET", "/redfish/v1/Chassis"), _FakeResp(200, json.dumps({"Members": [{"@odata.id": "/redfish/v1/Chassis/chassis"}]}))),
    ]
    redfish = _patch(monkeypatch, script, sink)
    serial, _ = redfish.RedfishClient("172.25.25.102", _CREDS).get_serial()
    assert serial == "WTHCHAS9"


def test_get_redfish_root_identifies_bmc_unauth(monkeypatch):
    # The OEM-detection probe is UNAUTHENTICATED (host-power independent) and reads
    # RedfishVersion + a best-effort Product identifier from the service root.
    sink = []
    script = [(("GET", "/redfish/v1/"),
               _FakeResp(200, json.dumps({"@odata.type": "#ServiceRoot.v1_5_0.ServiceRoot",
                                          "RedfishVersion": "1.5.0", "Product": "AMI Redfish Server"})))]
    redfish = _patch(monkeypatch, script, sink)
    is_bmc, ver, product = redfish.RedfishClient("172.25.25.102", []).get_redfish_root()
    assert is_bmc is True and ver == "1.5.0" and product == "AMI Redfish Server"
    assert "Authorization" not in sink[0]["headers"]      # unauthenticated


def test_get_redfish_root_synthesizes_product_from_oem(monkeypatch):
    # v1.17.0 AMI builds expose no Product, only an Oem vendor block -> '<vendor> Redfish'.
    sink = []
    script = [(("GET", "/redfish/v1/"),
               _FakeResp(200, json.dumps({"Managers": {"@odata.id": "/redfish/v1/Managers"},
                                          "RedfishVersion": "1.17.0", "Oem": {"Ami": {}}})))]
    redfish = _patch(monkeypatch, script, sink)
    is_bmc, ver, product = redfish.RedfishClient("172.25.25.102", []).get_redfish_root()
    assert is_bmc is True and ver == "1.17.0" and product == "Ami Redfish"


def test_get_redfish_root_not_a_bmc(monkeypatch):
    sink = []
    script = [(("GET", "/redfish/v1/"), _FakeResp(404, b"{}"))]
    redfish = _patch(monkeypatch, script, sink)
    assert redfish.RedfishClient("10.0.0.9", []).get_redfish_root() == (False, None, None)


def test_manager_reset_posts_force_restart(monkeypatch):
    sink = []
    script = [
        (("GET", "/redfish/v1/Managers"),
         _FakeResp(200, json.dumps({"Members": [{"@odata.id": "/redfish/v1/Managers/bmc"}]}))),
        (("POST", "/redfish/v1/Managers/bmc/Actions/Manager.Reset"), _FakeResp(204, b"")),
    ]
    redfish = _patch(monkeypatch, script, sink)
    ok, detail = redfish.RedfishClient("10.0.0.1", _CREDS).manager_reset()
    assert ok is True
    post = [r for r in sink if r["method"] == "POST"][0]
    assert post["path"] == "/redfish/v1/Managers/bmc/Actions/Manager.Reset"
    assert b"ForceRestart" in post["body"]


def test_manager_reset_rejected_is_false(monkeypatch):
    sink = []
    script = [
        (("GET", "/redfish/v1/Managers"),
         _FakeResp(200, json.dumps({"Members": [{"@odata.id": "/redfish/v1/Managers/bmc"}]}))),
        (("POST", "/redfish/v1/Managers/bmc/Actions/Manager.Reset"), _FakeResp(500, b"{}")),
    ]
    redfish = _patch(monkeypatch, script, sink)
    ok, detail = redfish.RedfishClient("10.0.0.1", _CREDS).manager_reset()
    assert ok is False and "500" in detail


def test_get_power_watts_tries_next_chassis_when_first_lacks_the_sensor(monkeypatch):
    """Real eindhoven shape, discovered live 2026-09-10 (the first version of this
    method got this wrong): 'Cpld' is listed before the baseboard AND has its OWN
    Sensors collection (just not the HSC one) -- so the walk can't stop at the
    first chassis member that merely HAS a Sensors link, only at the one whose
    Sensors collection actually contains the target sensor.

    _FakeConn matches by path prefix in script order, so (like the rest of this
    file) more specific paths must be listed before the shorter collection prefix
    they'd otherwise shadow."""
    sink = []
    script = [
        (("GET", "/redfish/v1/Chassis/Cpld/Sensors"), _FakeResp(200, json.dumps({
            "Members": [
                {"@odata.id": "/redfish/v1/Chassis/Cpld/Sensors/temperature_Cpld_Temperature"},
            ]}))),
        (("GET", "/redfish/v1/Chassis/Cpld"), _FakeResp(200, json.dumps({
            "Id": "Cpld", "Sensors": {"@odata.id": "/redfish/v1/Chassis/Cpld/Sensors"},
        }))),
        (("GET", "/redfish/v1/Chassis/TiogaPass_Baseboard/Sensors/power_MB_HSC_Input_Power"),
         _FakeResp(200, json.dumps({"Reading": 80.07, "ReadingUnits": "W"}))),
        (("GET", "/redfish/v1/Chassis/TiogaPass_Baseboard/Sensors"), _FakeResp(200, json.dumps({
            "Members": [
                {"@odata.id": "/redfish/v1/Chassis/TiogaPass_Baseboard/Sensors/temperature_MB_HSC_Temperature"},
                {"@odata.id": "/redfish/v1/Chassis/TiogaPass_Baseboard/Sensors/power_MB_HSC_Input_Power"},
            ]}))),
        (("GET", "/redfish/v1/Chassis/TiogaPass_Baseboard"), _FakeResp(200, json.dumps({
            "Id": "TiogaPass_Baseboard",
            "Sensors": {"@odata.id": "/redfish/v1/Chassis/TiogaPass_Baseboard/Sensors"},
        }))),
        (("GET", "/redfish/v1/Chassis"), _FakeResp(200, json.dumps({"Members": [
            {"@odata.id": "/redfish/v1/Chassis/Cpld"},
            {"@odata.id": "/redfish/v1/Chassis/TiogaPass_Baseboard"},
        ]}))),
    ]
    redfish = _patch(monkeypatch, script, sink)
    watts, detail = redfish.RedfishClient("172.25.26.102", _CREDS).get_power_watts()
    assert watts == 80.07 and detail == "ok"


def test_get_power_watts_no_hsc_sensor_returns_none(monkeypatch):
    sink = []
    script = [
        (("GET", "/redfish/v1/Chassis/TiogaPass_Baseboard/Sensors"), _FakeResp(200, json.dumps({
            "Members": [
                {"@odata.id": "/redfish/v1/Chassis/TiogaPass_Baseboard/Sensors/temperature_MB_HSC_Temperature"},
            ]}))),
        (("GET", "/redfish/v1/Chassis/TiogaPass_Baseboard"), _FakeResp(200, json.dumps({
            "Sensors": {"@odata.id": "/redfish/v1/Chassis/TiogaPass_Baseboard/Sensors"},
        }))),
        (("GET", "/redfish/v1/Chassis"), _FakeResp(200, json.dumps({"Members": [
            {"@odata.id": "/redfish/v1/Chassis/TiogaPass_Baseboard"},
        ]}))),
    ]
    redfish = _patch(monkeypatch, script, sink)
    watts, detail = redfish.RedfishClient("172.25.26.102", _CREDS).get_power_watts()
    assert watts is None and "no HSC input-power sensor" in detail


def test_get_power_watts_no_chassis_exposes_sensors(monkeypatch):
    sink = []
    script = [
        (("GET", "/redfish/v1/Chassis/Cpld"), _FakeResp(200, json.dumps({"Id": "Cpld"}))),
        (("GET", "/redfish/v1/Chassis"), _FakeResp(200, json.dumps({"Members": [
            {"@odata.id": "/redfish/v1/Chassis/Cpld"},
        ]}))),
    ]
    redfish = _patch(monkeypatch, script, sink)
    watts, detail = redfish.RedfishClient("172.25.26.102", _CREDS).get_power_watts()
    assert watts is None and "no Chassis member exposes Sensors" in detail
