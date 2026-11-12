"""state.run_owner: which blade produced a run's artifacts (spec 2026-09-14-post-occupant-reset §3.2)."""
from flax_post import state


class _Conn:
    def __init__(self, row):
        self.row, self.sql, self.params = row, None, None
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params):
        self.sql, self.params = sql, params
        return self
    def fetchone(self): return self.row


class _Pool:
    def __init__(self, conn): self.conn = conn
    def connection(self): return self.conn


def test_run_owner_returns_the_artifacts_bmc_mac_and_serial(monkeypatch):
    conn = _Conn(("98:03:9b:75:2b:12", "WTH49152ZGM1A"))
    monkeypatch.setattr(state, "get_pool", lambda: _Pool(conn))
    assert state.run_owner("de0598") == ("98:03:9b:75:2b:12", "WTH49152ZGM1A")
    assert "post_artifact" in conn.sql and conn.params == ("de0598",)


def test_run_owner_none_without_artifacts_or_run(monkeypatch):
    monkeypatch.setattr(state, "get_pool", lambda: _Pool(_Conn(None)))
    assert state.run_owner("gone") is None
    assert state.run_owner(None) is None
