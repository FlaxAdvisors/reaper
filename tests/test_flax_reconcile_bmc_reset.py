"""Tests for flax_reconcile.bmc_reset.reset_bmc_via_redfish + load_redfish_creds.

The HTTPS transport is stubbed by monkeypatching http.client.HTTPSConnection
(mirrors test_flax_reconcile_load_switches' BoomConn pattern) so no real BMC is
touched. No network, no DB.
"""
import json

from flax_reconcile import bmc_reset


# ---------------------------------------------------------------------------
# Fake HTTPS transport
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status, body=b""):
        self.status = status
        self._body = body

    def read(self):
        return self._body


class _FakeConn:
    """Records requests; returns scripted responses in order.

    `responses` is a list of (status, body) consumed one per .request()/.getresponse()
    pair. `requests` accumulates (method, path) for assertions.
    """
    def __init__(self, responses, sink):
        # Shared queue (no copy): GET pops on the first connection, POST pops the
        # next element on the second connection -- mirrors the real per-request
        # fresh-connection flow.
        self._responses = responses
        self._sink = sink
        self._last = None

    def request(self, method, path, body=None, headers=None):
        self._sink.append({"method": method, "path": path,
                           "body": body, "headers": headers or {}})
        self._last = self._responses.pop(0)

    def getresponse(self):
        status, body = self._last
        return _FakeResponse(status, body)

    def close(self):
        pass


def _install_fake(monkeypatch, responses):
    """Patch HTTPSConnection so every connection replays `responses` in order.

    Each new HTTPSConnection() instance pops from the shared `responses` queue,
    so a GET-then-POST flow needs [get_resp, post_resp].
    """
    sink = []

    def factory(host, timeout=None, context=None):
        return _FakeConn(responses, sink)

    monkeypatch.setattr(bmc_reset.http.client, "HTTPSConnection", factory)
    return sink


_MANAGERS_BODY = json.dumps(
    {"Members": [{"@odata.id": "/redfish/v1/Managers/Self"}]}).encode()
_CREDS = [{"bmcuser": "Administrator", "bmcpass": "superuser"}]


# ---------------------------------------------------------------------------
# reset_bmc_via_redfish — happy path
# ---------------------------------------------------------------------------

def test_reset_happy_path_204(monkeypatch):
    sink = _install_fake(monkeypatch, [(200, _MANAGERS_BODY), (204, b"")])

    ok, detail = bmc_reset.reset_bmc_via_redfish("192.168.88.50", _CREDS)

    assert ok is True
    assert "204" in detail
    # GET /Managers then POST Manager.Reset
    assert sink[0]["method"] == "GET"
    assert sink[0]["path"] == "/redfish/v1/Managers"
    assert sink[1]["method"] == "POST"
    assert sink[1]["path"] == "/redfish/v1/Managers/Self/Actions/Manager.Reset"
    assert json.loads(sink[1]["body"]) == {"ResetType": "ForceRestart"}
    assert sink[0]["headers"]["Authorization"].startswith("Basic ")


def test_reset_accepts_200_and_202(monkeypatch):
    for code in (200, 202):
        _install_fake(monkeypatch, [(200, _MANAGERS_BODY), (code, b"")])
        ok, _ = bmc_reset.reset_bmc_via_redfish("10.0.0.1", _CREDS)
        assert ok is True


# ---------------------------------------------------------------------------
# reset_bmc_via_redfish — failure / again-fail paths
# ---------------------------------------------------------------------------

def test_reset_auth_fail_returns_false(monkeypatch):
    # GET /Managers returns 401 for the only cred -> no POST, returns False.
    sink = _install_fake(monkeypatch, [(401, b"")])

    ok, detail = bmc_reset.reset_bmc_via_redfish("192.168.88.50", _CREDS)

    assert ok is False
    assert "401" in detail
    # only the GET happened; never POSTed
    assert all(r["method"] == "GET" for r in sink)


def test_reset_post_rejected_returns_false(monkeypatch):
    # auth OK, but Manager.Reset POST returns 500.
    _install_fake(monkeypatch, [(200, _MANAGERS_BODY), (500, b"")])

    ok, detail = bmc_reset.reset_bmc_via_redfish("192.168.88.50", _CREDS)

    assert ok is False
    assert "500" in detail


def test_reset_no_members_returns_false(monkeypatch):
    _install_fake(monkeypatch, [(200, json.dumps({"Members": []}).encode())])

    ok, detail = bmc_reset.reset_bmc_via_redfish("192.168.88.50", _CREDS)

    assert ok is False
    assert "member" in detail.lower()


def test_reset_transport_error_tries_next_cred(monkeypatch):
    """First cred's GET raises (transport error); second cred succeeds."""
    creds = [{"bmcuser": "wrong", "bmcpass": "x"},
             {"bmcuser": "Administrator", "bmcpass": "superuser"}]

    calls = {"n": 0}
    second_cred_queue = [(200, _MANAGERS_BODY), (204, b"")]

    def factory(host, timeout=None, context=None):
        calls["n"] += 1
        if calls["n"] == 1:
            class Boom:
                def request(self, *a, **k):
                    raise OSError("no route to host")
            return Boom()
        # GET + POST for the second cred share one queue (no copy).
        return _FakeConn(second_cred_queue, [])

    monkeypatch.setattr(bmc_reset.http.client, "HTTPSConnection", factory)
    ok, _ = bmc_reset.reset_bmc_via_redfish("192.168.88.50", creds)
    assert ok is True


def test_reset_no_creds_returns_false():
    ok, detail = bmc_reset.reset_bmc_via_redfish("192.168.88.50", [])
    assert ok is False
    assert "credential" in detail.lower()


def test_reset_no_bmc_ip_returns_false():
    ok, detail = bmc_reset.reset_bmc_via_redfish("", _CREDS)
    assert ok is False
    assert "bmc_ip" in detail


# ---------------------------------------------------------------------------
# load_redfish_creds
# ---------------------------------------------------------------------------

def test_load_redfish_creds_reads_list(tmp_path):
    p = tmp_path / "credentials-redfish.json"
    p.write_text(json.dumps(
        [{"bmcuser": "Administrator", "bmcpass": "superuser"},
         {"bmcuser": "root", "bmcpass": "test-obmc-pw"}]))
    creds = bmc_reset.load_redfish_creds(str(p))
    assert creds == [{"bmcuser": "Administrator", "bmcpass": "superuser"},
                     {"bmcuser": "root", "bmcpass": "test-obmc-pw"}]


def test_load_redfish_creds_missing_file_returns_empty(tmp_path):
    assert bmc_reset.load_redfish_creds(str(tmp_path / "nope.json")) == []


def test_load_redfish_creds_missing_as_directory_returns_empty(tmp_path):
    """A docker bind-mount of a missing host file creates an empty DIR;
    load must tolerate IsADirectoryError -> []."""
    d = tmp_path / "credentials-redfish.json"
    d.mkdir()
    assert bmc_reset.load_redfish_creds(str(d)) == []


def test_load_redfish_creds_vault_encrypted_returns_empty(tmp_path):
    p = tmp_path / "credentials-redfish.json"
    p.write_text("$ANSIBLE_VAULT;1.1;AES256\nabcdef\n")
    assert bmc_reset.load_redfish_creds(str(p)) == []


def test_load_redfish_creds_filters_malformed_entries(tmp_path):
    p = tmp_path / "credentials-redfish.json"
    p.write_text(json.dumps(
        [{"bmcuser": "a", "bmcpass": "b"}, {"nope": 1}]))
    assert bmc_reset.load_redfish_creds(str(p)) == [{"bmcuser": "a", "bmcpass": "b"}]
