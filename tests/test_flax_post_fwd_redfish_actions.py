# tests/test_flax_post_fwd_redfish_actions.py
from flax_post.fwd.redfish import RedfishClient


class _FakeConn:
    """Records requests; replies per a scripted {(method, path): (status, body)} map."""
    def __init__(self, script):
        self.script = script
        self.calls = []
    def request(self, method, path, body=None, headers=None):
        self._m, self._p, self._body = method, path, body
        self.calls.append((method, path, body))
    def getresponse(self):
        status, body = self.script.get((self._m, self._p), (404, b"{}"))
        class _R:
            def __init__(self): self.status = status
            def read(self): return body if isinstance(body, bytes) else body.encode()
            def getheader(self, k): return None
        return _R()
    def close(self): pass


def _client(script, monkeypatch):
    c = RedfishClient("1.2.3.4", [{"bmcuser": "USERID", "bmcpass": "pw"}])
    import flax_post.fwd.redfish as rf
    monkeypatch.setattr(rf.http.client, "HTTPSConnection",
                        lambda *a, **k: _FakeConn(script))
    return c


_SYS = b'{"Members":[{"@odata.id":"/redfish/v1/Systems/system"}]}'


def test_set_boot_pxe_patches_system_member(monkeypatch):
    script = {
        ("GET", "/redfish/v1/Systems"): (200, _SYS),
        ("PATCH", "/redfish/v1/Systems/system"): (200, b"{}"),
    }
    c = _client(script, monkeypatch)
    ok, detail = c.set_boot_pxe()
    assert ok is True


def test_set_boot_pxe_none_when_patch_rejected(monkeypatch):
    script = {
        ("GET", "/redfish/v1/Systems"): (200, _SYS),
        ("PATCH", "/redfish/v1/Systems/system"): (500, b"{}"),
    }
    c = _client(script, monkeypatch)
    ok, detail = c.set_boot_pxe()
    assert ok is None


def test_set_boot_pxe_none_when_no_system_member(monkeypatch):
    script = {("GET", "/redfish/v1/Systems"): (200, b'{"Members":[]}')}
    c = _client(script, monkeypatch)
    ok, detail = c.set_boot_pxe()
    assert ok is None


def test_power_on_posts_reset_action(monkeypatch):
    script = {
        ("GET", "/redfish/v1/Systems"): (200, _SYS),
        ("POST", "/redfish/v1/Systems/system/Actions/ComputerSystem.Reset"): (204, b""),
    }
    c = _client(script, monkeypatch)
    ok, detail = c.power_on()
    assert ok is True
