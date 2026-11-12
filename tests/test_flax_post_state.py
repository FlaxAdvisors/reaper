# tests/test_flax_post_state.py
"""Two-tier Postgres store: live post_state (by port), durable post_node (by id), settings."""
from unittest.mock import MagicMock


def _pool(fetch=None):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    cur = conn.execute.return_value
    cur.fetchall.return_value = fetch or []
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool, conn


def test_read_state_maps_rows(monkeypatch):
    from flax_post import state
    rows = [("et6b1", "rabbit-edam", "aa:bb", "SN1", "RRORD-003",
             {"power_on": "on", "watts": "210 W"})]
    pool, _ = _pool(fetch=rows)
    monkeypatch.setattr(state, "get_pool", lambda: pool)
    out = state.read_state()
    assert out["et6b1"]["serial"] == "SN1"
    assert out["et6b1"]["power_on"] == "on"           # vars flattened to top level
    assert out["et6b1"]["order_no"] == "RRORD-003"


def test_set_state_upserts_and_merges_vars(monkeypatch):
    from flax_post import state
    pool, conn = _pool()
    monkeypatch.setattr(state, "get_pool", lambda: pool)
    state.set_state("et6b1", bmc_mac="aa:bb", serial="SN1", power_on="on", watts="210 W")
    sql, params = conn.execute.call_args[0][0].lower(), conn.execute.call_args[0][1]
    assert "insert into post_state" in sql and "on conflict (port)" in sql
    assert "et6b1" in params                            # port bound
    assert any("power_on" in str(p) for p in params)    # vars json carries the slice


def test_upsert_node_keys_by_bmc_mac(monkeypatch):
    from flax_post import state
    pool, conn = _pool()
    monkeypatch.setattr(state, "get_pool", lambda: pool)
    state.upsert_node("aa:bb", serial="SN1", order_no="RRORD-003", last_port="et6b1", phase="Discover")
    sql = conn.execute.call_args[0][0].lower()
    assert "insert into post_node" in sql and "on conflict (bmc_mac)" in sql


def test_settings_roundtrip(monkeypatch):
    from flax_post import state
    import datetime
    at = datetime.datetime(2026, 9, 12, 3, 47, 40, tzinfo=datetime.timezone.utc)
    pool, conn = _pool(fetch=[("RRORD-003", "ovh-bare-tp", None, None, at)])
    monkeypatch.setattr(state, "get_pool", lambda: pool)
    assert state.read_settings() == {"order_no": "RRORD-003", "population": "ovh-bare-tp", "customer": None,
                                     "sales": None, "updated_at": "2026-09-12T03:47:40+00:00"}
    state.write_settings(order_no="RRORD-004")
    assert "update post_settings" in conn.execute.call_args[0][0].lower()


def test_write_settings_accepts_customer(monkeypatch):
    from flax_post import state
    executed = []
    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params): executed.append((sql, params))
    class _Pool:
        def connection(self): return _Conn()
    monkeypatch.setattr(state, "get_pool", lambda: _Pool())
    state.write_settings(customer="acme")
    assert "customer = %s" in executed[0][0]
    assert executed[0][1] == ("acme",)


def test_write_artifact_upserts_by_identity_run_stage_name(monkeypatch):
    from flax_post import state
    pool, conn = _pool()
    monkeypatch.setattr(state, "get_pool", lambda: pool)
    state.write_artifact("aa:bb", "run1", "inventory", "dmidecode", "raw",
                         "Handle 0x0001...", serial="SN1", order_no="RRORD-003", nbytes=220000)
    sql = conn.execute.call_args[0][0].lower()
    params = conn.execute.call_args[0][1]
    assert "insert into post_artifact" in sql
    assert "on conflict (bmc_mac, run_id, stage, name)" in sql
    assert "aa:bb" in params and "run1" in params and "inventory" in params and "dmidecode" in params


def test_list_artifacts_returns_metadata_only(monkeypatch):
    from flax_post import state
    rows = [("inventory", "dmidecode", "raw", 220000), ("inventory", "macinv", "digest", 4096)]
    pool, _ = _pool(fetch=rows)
    monkeypatch.setattr(state, "get_pool", lambda: pool)
    out = state.list_artifacts("aa:bb", "run1")
    assert out == [{"stage": "inventory", "name": "dmidecode", "kind": "raw", "bytes": 220000},
                   {"stage": "inventory", "name": "macinv", "kind": "digest", "bytes": 4096}]


def test_get_artifact_returns_content(monkeypatch):
    from flax_post import state
    pool, _ = _pool(fetch=[("Handle 0x0001...",)])
    monkeypatch.setattr(state, "get_pool", lambda: pool)
    assert state.get_artifact("aa:bb", "run1", "inventory", "dmidecode") == "Handle 0x0001..."


def test_get_stage_artifacts_returns_named_contents_with_capture_time(monkeypatch):
    import datetime as dt
    from flax_post import state
    t = dt.datetime(2026, 9, 24, 10, 0, tzinfo=dt.timezone.utc)
    pool, conn = _pool(fetch=[("dimmsum", "  64 GB  DIMM A0 ...", t), ("macinv", "2 Processor", t)])
    monkeypatch.setattr(state, "get_pool", lambda: pool)
    out = state.get_stage_artifacts("aa:bb", "run1", "inventory", names=["dimmsum", "macinv"])
    assert out == {"dimmsum": {"content": "  64 GB  DIMM A0 ...", "captured_at": t},
                   "macinv": {"content": "2 Processor", "captured_at": t}}
    sql, params = conn.execute.call_args[0]
    assert "FROM post_artifact" in sql and "captured_at" in sql and "name = ANY" in sql
    assert params == ("aa:bb", "run1", "inventory", ["dimmsum", "macinv"])


def test_get_stage_artifacts_without_names_reads_the_whole_stage(monkeypatch):
    from flax_post import state
    pool, conn = _pool(fetch=[])
    monkeypatch.setattr(state, "get_pool", lambda: pool)
    assert state.get_stage_artifacts("aa:bb", "run1", "inventory") == {}
    sql, params = conn.execute.call_args[0]
    assert "ANY" not in sql and params == ("aa:bb", "run1", "inventory")


def test_get_artifact_missing_returns_none(monkeypatch):
    from flax_post import state
    pool, _ = _pool(fetch=[])
    monkeypatch.setattr(state, "get_pool", lambda: pool)
    assert state.get_artifact("aa:bb", "run1", "inventory", "nope") is None


def test_purge_run_deletes_by_identity_and_run(monkeypatch):
    from flax_post import state
    pool, conn = _pool()
    monkeypatch.setattr(state, "get_pool", lambda: pool)
    state.purge_run("aa:bb", "run1")
    sql = conn.execute.call_args[0][0].lower()
    params = conn.execute.call_args[0][1]
    assert "delete from post_artifact" in sql and "where bmc_mac = %s and run_id = %s" in sql
    assert params == ("aa:bb", "run1")


# ---- durable result (spec 2026-09-11-post-completion-latch) ----
def test_get_node_returns_vars_or_empty(monkeypatch):
    from flax_post import state
    pool, _ = _pool(fetch=[({"result": {"run_id": "r1"}, "runs": []},)])
    monkeypatch.setattr(state, "get_pool", lambda: pool)
    assert state.get_node("aa:bb") == {"result": {"run_id": "r1"}, "runs": []}
    pool2, _ = _pool(fetch=[])
    monkeypatch.setattr(state, "get_pool", lambda: pool2)
    assert state.get_node("aa:bb") == {}


def test_record_result_appends_run_and_sets_result(monkeypatch):
    from flax_post import state
    import json
    pool, conn = _pool(fetch=[({"runs": [{"run_id": "r0", "verdict": "fail"}]},)])
    monkeypatch.setattr(state, "get_pool", lambda: pool)
    state.record_result("aa:bb", {"run_id": "r1", "verdict": "pass", "finished_at": 5,
                                  "order_no": "RRORD-100", "port": "et6b1"},
                        serial="SN1", order_no="RRORD-100", last_port="et6b1")
    sql, params = next(c[0] for c in conn.execute.call_args_list
                       if "insert into post_node" in c[0][0].lower())
    vars_ = json.loads(params[-1])
    assert vars_["result"]["run_id"] == "r1"
    assert [r["run_id"] for r in vars_["runs"]] == ["r0", "r1"]
    assert vars_["runs"][-1] == {"run_id": "r1", "verdict": "pass", "finished_at": 5,
                                "order_no": "RRORD-100", "port": "et6b1"}


def test_write_artifact_strips_nul_bytes(monkeypatch):
    """A SOL capture carries NUL bytes (serial noise/BREAK); PostgreSQL text
    rejects them and the raise escaped poll_target AFTER the verdict was
    written (et10b2 2026-09-12: stale Qualify, no console step, no result)."""
    from flax_post import state
    pool, conn = _pool()
    monkeypatch.setattr(state, "get_pool", lambda: pool)
    state.write_artifact("aa:bb", "run1", "console", "sol.txt", "raw", "a\x00b\x00\x00c")
    _, params = conn.execute.call_args[0]
    assert params[7] == "abc" and params[8] == 3
    assert "\x00" not in params[7]


def test_update_result_refreshes_result_without_a_history_entry(monkeypatch):
    from flax_post import state
    import json
    pool, conn = _pool()
    monkeypatch.setattr(state, "get_pool", lambda: pool)
    state.update_result("aa:bb", {"run_id": "r1", "verdict": "fail"})
    sql, params = next(c[0] for c in conn.execute.call_args_list
                       if "insert into post_node" in c[0][0].lower())
    vars_ = json.loads(params[-1])
    assert vars_ == {"result": {"run_id": "r1", "verdict": "fail"}}


def test_settings_roundtrip_carries_sales(monkeypatch):
    from flax_post import state
    pool, _ = _pool(fetch=[("RRORD-1033", "acme-leo", "Acme", "S-ORD1", None)])
    monkeypatch.setattr(state, "get_pool", lambda: pool)
    assert state.read_settings()["sales"] == "S-ORD1"


def test_write_settings_sales_only(monkeypatch):
    from flax_post import state
    executed = []
    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params): executed.append((sql, params))
    class _Pool:
        def connection(self): return _Conn()
    monkeypatch.setattr(state, "get_pool", lambda: _Pool())
    state.write_settings(sales="S-ORD1")
    assert "sales = %s" in executed[0][0] and executed[0][1] == ("S-ORD1",)
