"""GET /api/v1/leases: read-only latest kea.lease4 row per MAC (rackpost)."""
import datetime
import io
import json

import pytest
pytest.importorskip("flax_control.order_leases")
from fastapi.testclient import TestClient  # noqa: E402

from flax_control import order_leases  # noqa: E402
from flax_control.app import app  # noqa: E402
from flax_rack.kea_client import KeaClient, KeaError  # noqa: E402

_EXP = datetime.datetime(2026, 10, 30, 12, 0, tzinfo=datetime.timezone.utc)


class _Res:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _Pool:
    def __init__(self, rows=()):
        self.rows, self.calls = list(rows), []

    def connection(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return _Res(self.rows)


def _client(monkeypatch, pool):
    monkeypatch.setattr(order_leases, "get_pool", lambda: pool)
    return TestClient(app)


def test_bad_mac_400_no_db(monkeypatch):
    monkeypatch.setattr(order_leases, "get_pool", lambda: pytest.fail("touched db"))
    c = TestClient(app)
    assert c.get("/api/v1/leases?mac=nothex").status_code == 400
    assert c.get("/api/v1/leases?mac=b8599fac1ea8&mac=zz").status_code == 400


def test_more_than_64_400(monkeypatch):
    monkeypatch.setattr(order_leases, "get_pool", lambda: pytest.fail("touched db"))
    q = "&".join(f"mac={i:012x}" for i in range(65))
    assert TestClient(app).get("/api/v1/leases?" + q).status_code == 400


def test_exactly_64_ok(monkeypatch):
    pool = _Pool()
    q = "&".join(f"mac={i:012x}" for i in range(64))
    r = _client(monkeypatch, pool).get("/api/v1/leases?" + q)
    assert r.status_code == 200 and r.json() == {"leases": {}}


def test_shape_and_select_only(monkeypatch):
    pool = _Pool([("b8599fac1ea8", "172.32.5.101", _EXP, 0, 32, "rr101-bmc"),
                  ("aabbccddeeff", "172.26.0.9", _EXP, 0, 26, None)])
    r = _client(monkeypatch, pool).get(
        "/api/v1/leases?mac=B8:59:9F:AC:1E:A8&mac=b8599fac1ea8&mac=aabbccddeeff")
    assert r.status_code == 200
    assert r.json()["leases"]["b8599fac1ea8"] == {
        "ip": "172.32.5.101", "expire": _EXP.isoformat(), "state": 0,
        "subnet_id": 32, "hostname": "rr101-bmc"}
    assert r.json()["leases"]["aabbccddeeff"]["hostname"] == ""
    sql, params = pool.calls[0]
    s = " ".join(sql.split())
    assert s.startswith("SELECT") and "hwaddr = ANY(%(macs)s)" in s
    for bad in ("INSERT", "UPDATE", "DELETE"):
        assert bad not in s.upper()
    # deduped, normalised, as bytes
    assert params["macs"] == [bytes.fromhex("aabbccddeeff"), bytes.fromhex("b8599fac1ea8")]


def test_no_macs_no_db(monkeypatch):
    monkeypatch.setattr(order_leases, "get_pool", lambda: pytest.fail("touched db"))
    assert TestClient(app).get("/api/v1/leases").json() == {"leases": {}}


class _Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def test_client_leases_url_and_parse():
    seen = []

    def opener(req, timeout=None):
        seen.append((req.full_url, req.get_method()))
        return _Resp(json.dumps({"leases": {"aa": {"ip": "1.2.3.4"}}}).encode())
    k = KeaClient("http://x:1", opener=opener)
    assert k.leases(["aa", "bb"]) == {"aa": {"ip": "1.2.3.4"}}
    assert seen == [("http://x:1/api/v1/leases?mac=aa&mac=bb", "GET")]
    assert k.leases([]) == {} and len(seen) == 1


def test_client_leases_error():
    import urllib.error

    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "x", {}, io.BytesIO(b'{"error": "bad mac"}'))
    with pytest.raises(KeaError, match="bad mac"):
        KeaClient(opener=opener).leases(["zz"])
