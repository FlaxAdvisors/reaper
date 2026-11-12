"""Tests for flax_control.order_reservations (rackpost: order:* kea.hosts rows).

flax-control may write kea.hosts ONLY for `order:<ORDER>[/<rack>]` sources. A
MAC already owned by another source, or an IP held by another MAC in the same
subnet, is a 409 and nothing is written. Delete/list are equality-scoped on the
source so closing rack "" never touches rack "/b".
"""
import pytest
pytest.importorskip("flax_control.order_reservations")
from fastapi.testclient import TestClient  # noqa: E402

from flax_control import order_reservations as orr  # noqa: E402
from flax_control.app import app  # noqa: E402


class _Cur:
    def __init__(self, rows=(), rowcount=0):
        self._rows = list(rows)
        self.rowcount = rowcount

    def fetchall(self):
        return self._rows


class _Conn:
    """Answers the two conflict SELECTs from fixed data; records writes."""

    def __init__(self, existing_sources, ip_holders, list_rows, delete_rowcount,
                 insert_rowcount):
        self.existing_sources = existing_sources
        self.ip_holders = ip_holders
        self.list_rows = list_rows
        self.delete_rowcount = delete_rowcount
        self.insert_rowcount = insert_rowcount
        self.calls = []
        self.tx_flags = []
        self.inserted = []
        self.deleted = []
        self.in_tx = False
        self.tx_exits = []          # exception type (or None) each transaction ended with

    def transaction(self):
        conn = self

        class _Tx:
            def __enter__(self):
                conn.in_tx = True

            def __exit__(self, et, *a):
                conn.in_tx = False
                conn.tx_exits.append(et)
                return False
        return _Tx()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self.tx_flags.append(self.in_tx)
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO kea.hosts"):
            self.inserted.append(params)
            return _Cur(rowcount=self.insert_rowcount)
        if s.startswith("DELETE FROM kea.hosts"):
            if isinstance(params, dict):
                self.deleted.append(params)
            return _Cur(rowcount=self.delete_rowcount)
        if "AS owner_source" in s:
            return _Cur([(x,) for x in self.existing_sources])
        if "AS holder_mac" in s:
            # a bare MAC is held under "manual"; (mac, source) names the owner
            return _Cur([(x, "manual") if isinstance(x, str) else tuple(x)
                         for x in self.ip_holders])
        if "AS ipv4" in s:
            return _Cur(self.list_rows)
        raise AssertionError(f"unexpected SQL: {s}")


class _Pool:
    def __init__(self, existing_sources=(), ip_holders=(), list_rows=(),
                 delete_rowcount=0, insert_rowcount=1):
        self.conn = _Conn(list(existing_sources), list(ip_holders),
                          list(list_rows), delete_rowcount, insert_rowcount)

    def connection(self):
        return self.conn

    @property
    def inserted(self):
        return self.conn.inserted


def _pool(existing_sources=(), ip_holders=(), **kw):
    return _Pool(existing_sources, ip_holders, **kw)


_GOOD = dict(mac="b8599fac1ea8", ipv4="172.32.5.101", hostname="rr101-u5l-bmc", vid=32)


@pytest.mark.parametrize("src", ["triage", "post", "manual", "order:", "order:rr101",
                                 "order:X;--", "order:RRORD-101/bb", "order:RRORD-101/B",
                                 "order:-RRORD", "order:RRORD-101/", " order:RRORD-101",
                                 "order:RRORD-101\n", None, 101])
def test_bad_sources_rejected_before_db(src, monkeypatch):
    monkeypatch.setattr(orr, "get_pool", lambda: pytest.fail("touched db"))
    with pytest.raises(orr.BadSource):
        orr.upsert(source=src, **_GOOD)
    with pytest.raises(orr.BadSource):
        orr.delete_source(src)
    with pytest.raises(orr.BadSource):
        orr.list_source(src)


@pytest.mark.parametrize("mac", ["b8:59:9f:ac:1e:a8", "b8599fac1ea8", "b859.9fac.1ea8",
                                 "B8:59:9F:AC:1E:A8"])
def test_mac_forms_normalised(mac, monkeypatch):
    pool = _pool()
    monkeypatch.setattr(orr, "get_pool", lambda: pool)
    orr.upsert(source="order:RRORD-101", **{**_GOOD, "mac": mac})
    assert pool.inserted[0]["mac_hex"] == "b8599fac1ea8"


@pytest.mark.parametrize("mac", ["", "b8599fac1ea", "b8599fac1ea8a", "g8599fac1ea8",
                                 "b8:59:9f:ac:1e", "b8-59-9f:ac:1e:a8", "b8:599f:ac1e:a8",
                                 "b8599fac1ea8\n", None])
def test_bad_mac_rejected_before_db(mac, monkeypatch):
    monkeypatch.setattr(orr, "get_pool", lambda: pytest.fail("touched db"))
    with pytest.raises(orr.BadMac):
        orr.upsert(source="order:RRORD-101", **{**_GOOD, "mac": mac})


@pytest.mark.parametrize("field,value", [
    ("vid", 0), ("vid", 4095), ("vid", "32"), ("vid", True), ("vid", 3.0),
    ("ipv4", "172.32.5"), ("ipv4", "172.32.5.256"), ("ipv4", "::1"), ("ipv4", None),
    ("hostname", ""), ("hostname", "bad host"), ("hostname", "-lead"), ("hostname", None),
])
def test_bad_values_rejected_before_db(field, value, monkeypatch):
    monkeypatch.setattr(orr, "get_pool", lambda: pytest.fail("touched db"))
    with pytest.raises(orr.BadValue):
        orr.upsert(source="order:RRORD-101", **{**_GOOD, field: value})


def test_upsert_refuses_mac_owned_by_other_source(monkeypatch):
    pool = _pool(existing_sources=["manual"], ip_holders=[])
    monkeypatch.setattr(orr, "get_pool", lambda: pool)
    with pytest.raises(orr.Conflict, match="manual"):
        orr.upsert(source="order:RRORD-101", **_GOOD)
    assert not pool.inserted


def test_upsert_refuses_mac_owned_by_sibling_rack(monkeypatch):
    """Rack "" and rack "/b" of the same order are different owners."""
    pool = _pool(existing_sources=["order:RRORD-101/b"])
    monkeypatch.setattr(orr, "get_pool", lambda: pool)
    with pytest.raises(orr.Conflict, match="order:RRORD-101/b"):
        orr.upsert(source="order:RRORD-101", **_GOOD)
    assert not pool.inserted


def test_upsert_refuses_untagged_row(monkeypatch):
    """A row with no source (legacy triage) reads back as '' -> conflict."""
    pool = _pool(existing_sources=[""])
    monkeypatch.setattr(orr, "get_pool", lambda: pool)
    with pytest.raises(orr.Conflict):
        orr.upsert(source="order:RRORD-101", **_GOOD)
    assert not pool.inserted


def test_rack_source_accepted(monkeypatch):
    pool = _pool()
    monkeypatch.setattr(orr, "get_pool", lambda: pool)
    orr.upsert(source="order:RRORD-101/b", **_GOOD)
    assert len(pool.inserted) == 1
    assert '"order:RRORD-101/b"' in pool.inserted[0]["user_context"]


def test_upsert_same_source_mac_is_an_update_not_conflict(monkeypatch):
    pool = _pool(existing_sources=["order:RRORD-101"])
    monkeypatch.setattr(orr, "get_pool", lambda: pool)
    orr.upsert(source="order:RRORD-101", **_GOOD)
    assert len(pool.inserted) == 1


def test_upsert_refuses_ip_held_by_other_mac(monkeypatch):
    pool = _pool(existing_sources=[], ip_holders=["aabbccddeeff"])
    monkeypatch.setattr(orr, "get_pool", lambda: pool)
    with pytest.raises(orr.Conflict, match="aabbccddeeff"):
        orr.upsert(source="order:RRORD-101", **_GOOD)
    assert not pool.inserted
    # the IP check is scoped to the subnet and excludes this MAC
    sql, params = next(c for c in pool.conn.calls if "AS holder_mac" in c[0])
    assert "dhcp4_subnet_id = %(vid)s" in sql
    assert params["vid"] == 32 and params["mac_hex"] == "b8599fac1ea8"


def test_upsert_inserts_with_order_source(monkeypatch):
    pool = _pool()
    monkeypatch.setattr(orr, "get_pool", lambda: pool)
    orr.upsert(source="order:RRORD-101", **_GOOD)
    assert len(pool.inserted) == 1
    p = pool.inserted[0]
    assert p == {"mac_hex": "b8599fac1ea8", "ipv4": "172.32.5.101",
                 "hostname": "rr101-u5l-bmc", "vid": 32,
                 "source": "order:RRORD-101",
                 "user_context": '{"source": "order:RRORD-101"}'}
    sql = " ".join(next(c for c in pool.conn.calls
                        if c[0].lstrip().startswith("INSERT"))[0].split())
    # mirrors flax_classify's partial-index ON CONFLICT; v4 only
    assert ("ON CONFLICT (dhcp_identifier, dhcp_identifier_type, dhcp4_subnet_id) "
            "WHERE dhcp4_subnet_id IS NOT NULL DO UPDATE") in sql
    assert "dhcp6_subnet_id" not in sql
    # the DO UPDATE can never clobber another source's row (race guard)
    assert "WHERE (kea.hosts.user_context::jsonb) ->> 'source' = %(source)s" in sql
    # checks run before the write, all on one connection (one transaction)
    kinds = [("owner" if "AS owner_source" in s else "ip" if "AS holder_mac" in s
              else "insert") for s, _ in pool.conn.calls]
    assert kinds == ["owner", "ip", "insert"]


def test_upsert_supersedes_same_source_ip_holder(monkeypatch):
    """Blade swap: the slot's IP is held by the OLD mac under the same order
    source -> that row is deleted and the new one inserted, in one transaction."""
    pool = _pool(ip_holders=[("0c42a1e16b52", "order:RRORD-101")])
    monkeypatch.setattr(orr, "get_pool", lambda: pool)
    orr.upsert(source="order:RRORD-101", **_GOOD)
    assert len(pool.inserted) == 1
    (d,) = pool.conn.deleted
    assert d["holder_hex"] == "0c42a1e16b52" and d["source"] == "order:RRORD-101"
    assert d["vid"] == 32
    sql = " ".join(next(c for c in pool.conn.calls if c[0].lstrip().startswith("DELETE"))[0].split())
    # only that MAC's row in that subnet, and only under the caller's exact source
    assert "dhcp_identifier = decode(%(holder_hex)s, 'hex')" in sql
    assert "dhcp4_subnet_id = %(vid)s" in sql
    assert "(user_context::jsonb) ->> 'source' = %(source)s" in sql
    kinds = [("owner" if "AS owner_source" in s else "ip" if "AS holder_mac" in s
              else "delete" if s.lstrip().startswith("DELETE") else "insert")
             for s, _ in pool.conn.calls]
    assert kinds == ["owner", "ip", "delete", "insert"]
    assert all(pool.conn.tx_flags) and pool.conn.tx_exits == [None]


@pytest.mark.parametrize("holders", [
    [("0c42a1e16b52", "order:RRORD-101/b")],                    # sibling rack
    [("0c42a1e16b52", "triage")],
    [("0c42a1e16b52", "")],                                     # untagged
    [("0c42a1e16b52", "order:RRORD-101"), ("aabbccddeeff", "manual")],
])
def test_upsert_ip_held_under_other_source_changes_nothing(holders, monkeypatch):
    pool = _pool(ip_holders=holders)
    monkeypatch.setattr(orr, "get_pool", lambda: pool)
    with pytest.raises(orr.Conflict, match="already held by"):
        orr.upsert(source="order:RRORD-101", **_GOOD)
    assert not pool.inserted and not pool.conn.deleted
    assert pool.conn.tx_exits == [orr.Conflict]       # raised inside -> rolled back


def test_upsert_runs_in_one_transaction(monkeypatch):
    """The pool is autocommit: every statement must run inside conn.transaction()."""
    pool = _pool(existing_sources=["order:RRORD-101"])
    monkeypatch.setattr(orr, "get_pool", lambda: pool)
    orr.upsert(source="order:RRORD-101", **_GOOD)
    assert len(pool.conn.calls) == 3 and all(pool.conn.tx_flags)
    assert pool.conn.tx_exits == [None]


def test_upsert_conflict_raised_inside_transaction(monkeypatch):
    pool = _pool(existing_sources=["manual"])
    monkeypatch.setattr(orr, "get_pool", lambda: pool)
    with pytest.raises(orr.Conflict):
        orr.upsert(source="order:RRORD-101", **_GOOD)
    assert pool.conn.tx_exits == [orr.Conflict]


@pytest.mark.parametrize("ipv4,vid", [("172.33.5.101", 32), ("10.32.5.101", 32),
                                      ("172.32.5.101", 33)])
def test_ipv4_outside_vid_net_rejected_before_db(ipv4, vid, monkeypatch):
    monkeypatch.setattr(orr, "get_pool", lambda: pytest.fail("touched db"))
    with pytest.raises(orr.BadValue, match="172"):
        orr.upsert(source="order:RRORD-101", **{**_GOOD, "ipv4": ipv4, "vid": vid})


def test_upsert_race_lost_raises_conflict(monkeypatch):
    """DO UPDATE ... WHERE matched nothing (row turned foreign between the
    check and the write) -> Conflict, so the transaction rolls back."""
    pool = _pool(insert_rowcount=0)
    monkeypatch.setattr(orr, "get_pool", lambda: pool)
    with pytest.raises(orr.Conflict):
        orr.upsert(source="order:RRORD-101", **_GOOD)


def test_delete_source_scoped_sql(monkeypatch):
    pool = _pool(delete_rowcount=3)
    monkeypatch.setattr(orr, "get_pool", lambda: pool)
    assert orr.delete_source("order:RRORD-101") == 3
    (sql, params), = pool.conn.calls
    s = " ".join(sql.split())
    assert s.startswith("DELETE FROM kea.hosts")
    assert "(user_context::jsonb) ->> 'source' = %s" in s
    assert "dhcp_identifier_type = 0" in s
    assert params == ("order:RRORD-101",)


def test_delete_source_does_not_touch_sibling_rack(monkeypatch):
    pool = _pool()
    monkeypatch.setattr(orr, "get_pool", lambda: pool)
    orr.delete_source("order:RRORD-101")
    (sql, params), = pool.conn.calls
    up = sql.upper()
    assert "LIKE" not in up and "STARTS_WITH" not in up and "~" not in sql
    assert "%" not in params[0]


def test_list_source(monkeypatch):
    pool = _pool(list_rows=[("b8599fac1ea8", "172.32.5.101", "rr101-u5l-bmc", 32)])
    monkeypatch.setattr(orr, "get_pool", lambda: pool)
    rows = orr.list_source("order:RRORD-101/b")
    assert rows == [{"mac": "b8599fac1ea8", "ipv4": "172.32.5.101",
                     "hostname": "rr101-u5l-bmc", "vid": 32}]
    (sql, params), = pool.conn.calls
    assert "(user_context::jsonb) ->> 'source' = %s" in " ".join(sql.split())
    assert params == ("order:RRORD-101/b",)


# --- HTTP ------------------------------------------------------------------

def test_http_409_and_400(monkeypatch):
    c = TestClient(app)
    monkeypatch.setattr(orr, "upsert", lambda **k: (_ for _ in ()).throw(orr.Conflict("mac owned by manual")))
    r = c.post("/api/v1/order-reservations", json=dict(source="order:RRORD-101", mac="b8599fac1ea8",
               ipv4="172.32.5.101", hostname="h", vid=32))
    assert r.status_code == 409 and "manual" in r.json()["error"]
    assert c.delete("/api/v1/order-reservations", params={"source": "triage"}).status_code == 400


def test_http_post_ok_passes_fields(monkeypatch):
    got = {}
    monkeypatch.setattr(orr, "upsert", lambda **k: got.update(k))
    body = dict(source="order:RRORD-101", mac="b8599fac1ea8", ipv4="172.32.5.101",
                hostname="h", vid=32)
    r = TestClient(app).post("/api/v1/order-reservations", json=body)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert got == body


def test_http_post_bad_input_is_400_without_db(monkeypatch):
    monkeypatch.setattr(orr, "get_pool", lambda: pytest.fail("touched db"))
    c = TestClient(app)
    base = dict(source="order:RRORD-101", mac="b8599fac1ea8", ipv4="172.32.5.101",
                hostname="h", vid=32)
    for bad in ({"source": "manual"}, {"mac": "zz"}, {"vid": 0}, {"ipv4": "x"}):
        r = c.post("/api/v1/order-reservations", json={**base, **bad})
        assert r.status_code == 400, bad
        assert "error" in r.json()
    r = c.post("/api/v1/order-reservations", json={k: v for k, v in base.items() if k != "vid"})
    assert r.status_code == 400
    r = c.post("/api/v1/order-reservations", json=[1, 2])
    assert r.status_code == 400


def test_http_get_and_delete(monkeypatch):
    c = TestClient(app)
    monkeypatch.setattr(orr, "list_source", lambda s: [{"mac": "m", "src": s}])
    monkeypatch.setattr(orr, "delete_source", lambda s: 2)
    r = c.get("/api/v1/order-reservations", params={"source": "order:RRORD-101/b"})
    assert r.status_code == 200 and r.json() == {"rows": [{"mac": "m", "src": "order:RRORD-101/b"}]}
    r = c.delete("/api/v1/order-reservations", params={"source": "order:RRORD-101"})
    assert r.status_code == 200 and r.json() == {"deleted": 2}


def test_http_missing_or_bad_source_is_400(monkeypatch):
    monkeypatch.setattr(orr, "get_pool", lambda: pytest.fail("touched db"))
    c = TestClient(app)
    assert c.get("/api/v1/order-reservations").status_code == 400
    assert c.delete("/api/v1/order-reservations").status_code == 400
    assert c.get("/api/v1/order-reservations", params={"source": "order:x"}).status_code == 400


# --- migration 036 (static: what it executes) --------------------------------

def _m036():
    import importlib.util
    import pathlib
    import flax_control
    p = (pathlib.Path(flax_control.__file__).resolve().parent.parent
         / "schema/versions/036_flax_control_order_reservations.py")
    spec = importlib.util.spec_from_file_location("m036", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _m036_sql(monkeypatch, fn):
    m = _m036()
    seen = []
    monkeypatch.setattr(m.op, "execute", lambda sql: seen.append(sql))
    getattr(m, fn)()
    return [" ".join(s.split()) for s in seen]


def test_m036_revision_chain():
    m = _m036()
    assert (m.revision, m.down_revision) == ("036000000001", "035000000001")


def test_m036_upgrade_grants(monkeypatch):
    assert _m036_sql(monkeypatch, "upgrade") == [
        "GRANT INSERT, UPDATE ON kea.hosts TO flax_control",
        "GRANT USAGE, SELECT ON SEQUENCE kea.hosts_host_id_seq TO flax_control",
    ]


def test_m036_downgrade_keeps_delete_and_column_update(monkeypatch):
    sql = _m036_sql(monkeypatch, "downgrade")
    assert "REVOKE INSERT, UPDATE ON kea.hosts FROM flax_control" in sql
    assert "REVOKE USAGE, SELECT ON SEQUENCE kea.hosts_host_id_seq FROM flax_control" in sql
    assert not any("DELETE" in s for s in sql)  # 022 owns DELETE
    # table-wide REVOKE UPDATE drops 009's column grant; it must be restored after
    i = sql.index("REVOKE INSERT, UPDATE ON kea.hosts FROM flax_control")
    assert sql.index("GRANT UPDATE (user_context) ON kea.hosts TO flax_control") > i
