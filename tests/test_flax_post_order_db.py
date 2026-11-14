"""post_run / post_order writes, against the LOCAL scratch cluster as flax_post."""
import os

import pytest

psycopg = pytest.importorskip("psycopg")
MAC = "aa:de:ad:42:00:11"


def _dsn(user):
    e = os.environ.get
    return (f"host={e('PGHOST', '127.0.0.1')} port={e('PGPORT', '5432')} "
            f"dbname={e('PGDATABASE', 'flax')} user={user}")


class _Pool:
    def __init__(self, c): self.c = c
    def connection(self):
        import contextlib
        return contextlib.nullcontext(self.c)


@pytest.fixture()
def db(monkeypatch):
    try:
        admin = psycopg.connect(_dsn("postgres"), connect_timeout=3, autocommit=True)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"no DB: {exc}")
    if admin.execute("SELECT to_regclass('public._retention_scratch_marker')").fetchone()[0] is None:
        admin.close(); pytest.skip("not the local scratch cluster")
    if admin.execute("SELECT to_regclass('public.post_run')").fetchone()[0] is None:
        admin.close(); pytest.skip("migration 042 not applied")

    def clean():
        admin.execute("DELETE FROM post_run WHERE bmc_mac LIKE 'aa:de:ad:42:%'")
        admin.execute("DELETE FROM post_node WHERE bmc_mac LIKE 'aa:de:ad:42:%'")
        admin.execute("DELETE FROM post_order WHERE order_no LIKE 'ZZTEST-%'")
        admin.execute("DELETE FROM post_artifact WHERE bmc_mac LIKE 'aa:de:ad:42:%'")
    clean()
    c = psycopg.connect(_dsn("flax_post"), connect_timeout=3, autocommit=True)
    from flax_post import state
    monkeypatch.setattr(state, "get_pool", lambda: _Pool(c))
    yield admin, c
    clean(); c.close(); admin.close()


def _result(run_id="zz-r1"):
    r = {"verdict": "pass", "order_no": "ZZTEST-100", "finished_at": 1762400000,
         "serial": "TESTSN-ORD-1", "port": "et7b1"}
    if run_id:
        r["run_id"] = run_id
    return r


def test_record_result_writes_post_run(db):
    admin, _ = db
    from flax_post import state
    state.record_result(MAC, _result(), serial="TESTSN-ORD-1", order_no="ZZTEST-100", last_port="et7b1")
    assert admin.execute("SELECT verdict, order_no FROM post_run WHERE bmc_mac=%s AND run_id='zz-r1'",
                         (MAC,)).fetchone() == ("pass", "ZZTEST-100")


def test_record_result_without_run_id_writes_no_post_run(db):
    admin, _ = db
    from flax_post import state
    state.record_result(MAC, _result(run_id=None), serial="TESTSN-ORD-1", order_no="ZZTEST-100")
    assert admin.execute("SELECT count(*) FROM post_run WHERE bmc_mac=%s", (MAC,)).fetchone()[0] == 0


def test_update_result_moves_that_runs_history_verdict(db):
    admin, _ = db
    from flax_post import state
    state.record_result(MAC, dict(_result(), verdict="fail"), serial="TESTSN-ORD-1", order_no="ZZTEST-100")
    state.update_result(MAC, _result())                      # population re-judge: fail -> pass
    runs = state.get_node(MAC)["runs"]
    assert [(r["run_id"], r["verdict"]) for r in runs] == [("zz-r1", "pass")]
    assert admin.execute("SELECT verdict FROM post_run WHERE bmc_mac=%s AND run_id='zz-r1'",
                         (MAC,)).fetchone() == ("pass",)


def test_touch_order_reopens_finished(db):
    admin, c = db
    from flax_post import state
    admin.execute("INSERT INTO post_order (order_no, finished_at) VALUES ('ZZTEST-7', now())")
    state.touch_order(c, "ZZTEST-7")
    assert admin.execute("SELECT finished_at FROM post_order WHERE order_no='ZZTEST-7'"
                         ).fetchone() == (None,)
    assert "ZZTEST-7" in state.open_orders(c)
    assert any(o["order_no"] == "ZZTEST-7" for o in state.list_orders())


def test_read_nodes_returns_assignment_columns(db):
    admin, _ = db
    from flax_post import state
    admin.execute("INSERT INTO post_node (bmc_mac, serial, assigned_order_no, assigned_serial) "
                  "VALUES (%s, 'TESTSN-ORD-1', 'ZZTEST-100', 'TESTSN-ORD-1')", (MAC,))
    n = state.read_nodes([MAC])[MAC]
    assert n["assigned_order_no"] == "ZZTEST-100" and n["serial"] == "TESTSN-ORD-1" and "vars" not in n


# --- Task 3: re-assignment ---------------------------------------------------
import json as _json

M1, SN1 = "aa:de:ad:42:00:01", "TESTSN-ORD-1"
HDR = {"order_no": "ZZTEST-A", "sales": None, "customer": "Z", "population": None}


def _seed(admin):
    for o, fin in (("ZZTEST-A", False), ("ZZTEST-B", False), ("ZZTEST-F", True)):
        admin.execute("INSERT INTO post_order (order_no, finished_at) VALUES (%s, %s)",
                      (o, "2026-11-01" if fin else None))
    runs = [{"run_id": "r0", "order_no": "ZZTEST-A"}, {"run_id": "r1", "order_no": "ZZTEST-F"},
            {"run_id": "r2", "order_no": "ZZTEST-A"}]
    admin.execute("INSERT INTO post_node (bmc_mac, serial, order_no, vars) VALUES (%s, %s, 'ZZTEST-A', %s)",
                  (M1, SN1, _json.dumps({"runs": runs, "result": {"run_id": "r2", "order_no": "ZZTEST-A"}})))
    for rid, o in (("r1", "ZZTEST-F"), ("r2", "ZZTEST-A")):
        admin.execute("INSERT INTO post_run (bmc_mac, run_id, serial, order_no, result) VALUES (%s,%s,%s,%s,'{}')",
                      (M1, rid, SN1, o))
    for rid, o, n in (("r1", "ZZTEST-F", 1), ("r2", "ZZTEST-A", 2), ("r0", "ZZTEST-A", 1)):
        for i in range(n):
            admin.execute("INSERT INTO post_artifact (bmc_mac, serial, order_no, run_id, stage, name, kind, content) "
                          "VALUES (%s,%s,%s,%s,'s',%s,'text','x')", (M1, SN1, o, rid, f"a{i}"))


def _orders(admin, table, mac=M1):
    return sorted(admin.execute(f"SELECT run_id, order_no FROM {table} WHERE bmc_mac=%s", (mac,)).fetchall())


def test_assign_moves_open_entries_only(db):
    admin, _ = db
    from flax_post import state
    admin.execute("DELETE FROM post_artifact WHERE bmc_mac LIKE 'aa:de:ad:42:%'")
    _seed(admin)
    state.assign_node(M1, SN1, {"order_no": "ZZTEST-B"}, HDR)
    assert _orders(admin, "post_run") == [("r1", "ZZTEST-F"), ("r2", "ZZTEST-B")]
    assert {r for r in _orders(admin, "post_artifact")} == {("r0", "ZZTEST-B"), ("r1", "ZZTEST-F"), ("r2", "ZZTEST-B")}
    v = admin.execute("SELECT order_no, vars FROM post_node WHERE bmc_mac=%s", (M1,)).fetchone()
    assert v[0] == "ZZTEST-B"
    assert {r["run_id"]: r["order_no"] for r in v[1]["runs"]} == {"r0": "ZZTEST-B", "r1": "ZZTEST-F", "r2": "ZZTEST-B"}
    admin.execute("DELETE FROM post_artifact WHERE bmc_mac LIKE 'aa:de:ad:42:%'")


def test_assign_leaves_other_serial_runs_alone(db):
    admin, _ = db
    from flax_post import state
    admin.execute("DELETE FROM post_artifact WHERE bmc_mac LIKE 'aa:de:ad:42:%'")
    _seed(admin)
    admin.execute("INSERT INTO post_artifact (bmc_mac, serial, order_no, run_id, stage, name, kind, content) "
                  "VALUES (%s,'TESTSN-ORD-OTHER','ZZTEST-A','r3','s','a','text','x')", (M1,))
    state.assign_node(M1, SN1, {"order_no": "ZZTEST-B"}, HDR)
    assert ("r3", "ZZTEST-A") in _orders(admin, "post_artifact")
    admin.execute("DELETE FROM post_artifact WHERE bmc_mac LIKE 'aa:de:ad:42:%'")


def test_clear_assignment_moves_to_header(db):
    admin, _ = db
    from flax_post import state
    admin.execute("DELETE FROM post_artifact WHERE bmc_mac LIKE 'aa:de:ad:42:%'")
    _seed(admin)
    state.assign_node(M1, SN1, {"order_no": "ZZTEST-B"}, HDR)
    state.assign_node(M1, SN1, None, HDR)
    assert ("r2", "ZZTEST-A") in _orders(admin, "post_run")
    assert admin.execute("SELECT assigned_order_no FROM post_node WHERE bmc_mac=%s", (M1,)).fetchone()[0] is None
    admin.execute("DELETE FROM post_artifact WHERE bmc_mac LIKE 'aa:de:ad:42:%'")


def test_reassign_failure_rolls_back_assignment(db, monkeypatch):
    admin, _ = db
    from flax_post import state
    admin.execute("INSERT INTO post_node (bmc_mac, serial) VALUES (%s, %s)", (M1, SN1))

    def boom(conn, *a, **k):
        conn.execute("SELECT 1/0")
    monkeypatch.setattr(state, "reassign_entries", boom)
    with pytest.raises(Exception):
        state.assign_node(M1, SN1, {"order_no": "ZZTEST-B"}, HDR)
    assert admin.execute("SELECT assigned_order_no FROM post_node WHERE bmc_mac=%s", (M1,)).fetchone()[0] is None


def test_header_change_moves_only_present_unassigned_nodes(db):
    admin, _ = db
    from flax_post import assign, state
    admin.execute("INSERT INTO post_order (order_no) VALUES ('ZZTEST-A'), ('ZZTEST-B')")
    P, D, O = "aa:de:ad:42:00:21", "aa:de:ad:42:00:22", "aa:de:ad:42:00:23"
    for mac, sn in ((P, "TESTSN-ORD-P"), (D, "TESTSN-ORD-D"), (O, "TESTSN-ORD-O")):
        admin.execute("INSERT INTO post_run (bmc_mac, run_id, serial, order_no, result) VALUES (%s,'rx',%s,'ZZTEST-A','{}')", (mac, sn))
        admin.execute("INSERT INTO post_node (bmc_mac, serial, order_no) VALUES (%s,%s,'ZZTEST-A')", (mac, sn))
    admin.execute("UPDATE post_node SET assigned_order_no='ZZTEST-A', assigned_serial='TESTSN-ORD-O' WHERE bmc_mac=%s", (O,))
    rows = {"et1b1": {"bmc_mac": P, "serial": "TESTSN-ORD-P", "order_no": "ZZTEST-A"},
            "et1b2": {"bmc_mac": O, "serial": "TESTSN-ORD-O", "order_no": "ZZTEST-A"}}
    before = admin.execute("SELECT order_no FROM post_settings WHERE id=1").fetchone()[0]
    try:
        f = assign.header_followers(rows, state.read_nodes([P, D, O]), dict(HDR))
        assert [x["bmc_mac"] for x in f] == [P]
        state.set_header("ZZTEST-B", f)
        got = dict(admin.execute("SELECT bmc_mac, order_no FROM post_run WHERE bmc_mac = ANY(%s)", ([P, D, O],)).fetchall())
        assert got == {P: "ZZTEST-B", D: "ZZTEST-A", O: "ZZTEST-A"}
    finally:
        admin.execute("UPDATE post_settings SET order_no=%s WHERE id=1", (before,))


def test_runs_under_includes_compact_runs(db):
    admin, _ = db
    from flax_post import state
    admin.execute("INSERT INTO post_order (order_no) VALUES ('ZZTEST-A')")
    admin.execute("INSERT INTO post_node (bmc_mac, serial, vars) VALUES (%s, %s, %s)",
                  (M1, SN1, _json.dumps({"runs": [{"run_id": "c0", "order_no": "ZZTEST-A", "verdict": "pass",
                                                   "finished_at": 10, "port": "et7b1"}]})))
    admin.execute("INSERT INTO post_artifact (bmc_mac, serial, order_no, run_id, stage, name, kind, content) "
                  "VALUES (%s,%s,'ZZTEST-A','c0','s','a','text','x')", (M1, SN1))
    admin.execute("INSERT INTO post_run (bmc_mac, run_id, serial, order_no, verdict, finished_at, result) "
                  "VALUES (%s,'f1',%s,'ZZTEST-A','pass',to_timestamp(20),'{\"port\":\"et7b1\"}')", (M1, SN1))
    got = {r["run_id"]: (r["compact"], r["serial"]) for r in state.runs_under("ZZTEST-A")
           if r["bmc_mac"] == M1}
    assert got == {"c0": (True, SN1), "f1": (False, SN1)}



def test_list_orders_includes_orders_with_runs_but_no_registry_row(db):
    admin, _ = db
    from flax_post import state
    admin.execute("INSERT INTO post_run (bmc_mac, run_id, serial, order_no, verdict, finished_at, result) "
                  "VALUES (%s,'old1',%s,'ZZTEST-OLD','pass',to_timestamp(5),'{}')", (M1, SN1))
    admin.execute("INSERT INTO post_order (order_no) VALUES ('ZZTEST-REG')")
    names = [o["order_no"] for o in state.list_orders()]
    assert "ZZTEST-OLD" in names and "ZZTEST-REG" in names
    admin.execute("INSERT INTO post_order (order_no, finished_at) VALUES ('ZZTEST-OLD', now())")
    assert "ZZTEST-OLD" not in [o["order_no"] for o in state.list_orders()]


# --- order labels: one record, one writer (spec 2026-11-09 post order labels) ---
import contextlib as _ctx
import inspect as _inspect


def _labels(admin, o):
    return admin.execute("SELECT sales, customer, population, finished_at IS NOT NULL "
                         "FROM post_order WHERE order_no=%s", (o,)).fetchone()


@_ctx.contextmanager
def _keep_settings(admin):
    cols = "order_no, sales, customer, population"
    before = admin.execute(f"SELECT {cols} FROM post_settings WHERE id=1").fetchone()
    try:
        yield
    finally:
        admin.execute("UPDATE post_settings SET order_no=%s, sales=%s, customer=%s, population=%s "
                      "WHERE id=1", before)


def test_set_order_labels_overwrites_and_blank_clears(db):
    admin, _ = db
    from flax_post import state
    state.set_order_labels("ZZTEST-L", sales="S1", customer="C1", population="P1")
    state.set_order_labels("ZZTEST-L", sales="S2", customer="")
    assert _labels(admin, "ZZTEST-L") == ("S2", None, "P1", False)
    state.set_order_labels("ZZTEST-L", population=None)
    assert _labels(admin, "ZZTEST-L") == ("S2", None, None, False)


def test_set_order_labels_creates_missing_row(db):
    admin, _ = db
    from flax_post import state
    state.set_order_labels("ZZTEST-N", sales="SN", customer="CN", population="PN")
    assert _labels(admin, "ZZTEST-N") == ("SN", "CN", "PN", False)


def test_set_order_labels_never_reopens_or_finishes(db):
    admin, _ = db
    from flax_post import state
    admin.execute("INSERT INTO post_order (order_no, finished_at) VALUES ('ZZTEST-F', now())")
    state.set_order_labels("ZZTEST-F", sales="SF")
    assert _labels(admin, "ZZTEST-F") == ("SF", None, None, True)


def test_touch_order_no_longer_copies_labels(db):
    admin, c = db
    from flax_post import state
    admin.execute("INSERT INTO post_order (order_no, sales, customer, population, finished_at) "
                  "VALUES ('ZZTEST-T', 'ST', 'CT', 'PT', now())")
    state.touch_order(c, "ZZTEST-T")
    assert _labels(admin, "ZZTEST-T") == ("ST", "CT", "PT", False)
    params = _inspect.signature(state.touch_order).parameters
    assert "sales" not in params and "customer" not in params


def test_header_set_writes_new_order_only(db):
    admin, _ = db
    from flax_post import state
    admin.execute("INSERT INTO post_order (order_no, sales) VALUES ('ZZTEST-A', 'SA')")
    with _keep_settings(admin):
        admin.execute("UPDATE post_settings SET order_no='ZZTEST-A', sales='SA' WHERE id=1")
        state.set_header("ZZTEST-B", [], sales="SB")
        assert _labels(admin, "ZZTEST-B")[0] == "SB"
        assert _labels(admin, "ZZTEST-A")[0] == "SA"
        assert admin.execute("SELECT order_no, sales FROM post_settings WHERE id=1"
                             ).fetchone() == ("ZZTEST-B", "SB")


def test_header_order_change_does_not_carry_labels(db):
    admin, _ = db
    from flax_post import state
    admin.execute("INSERT INTO post_order (order_no, sales, customer, population) "
                  "VALUES ('ZZTEST-A', 'SA', 'CA', 'PA')")
    with _keep_settings(admin):
        admin.execute("UPDATE post_settings SET order_no='ZZTEST-A', sales='SA', customer='CA', "
                      "population='PA' WHERE id=1")
        state.set_header("ZZTEST-B", [])
        assert _labels(admin, "ZZTEST-B") == (None, None, None, False)
        assert admin.execute("SELECT sales, customer, population FROM post_settings WHERE id=1"
                             ).fetchone() == (None, None, None)


def test_header_set_on_finished_order_keeps_labels(db):
    admin, _ = db
    from flax_post import state
    admin.execute("INSERT INTO post_order (order_no, sales, customer, population, finished_at) "
                  "VALUES ('ZZTEST-B', 'SB', 'CB', 'PB', now())")
    with _keep_settings(admin):
        state.set_header("ZZTEST-B", [])
        assert _labels(admin, "ZZTEST-B") == ("SB", "CB", "PB", False)
        assert admin.execute("SELECT sales, customer, population FROM post_settings WHERE id=1"
                             ).fetchone() == ("SB", "CB", "PB")


def test_assign_writes_no_labels(db):
    admin, _ = db
    from flax_post import state
    admin.execute("INSERT INTO post_order (order_no, sales, customer) VALUES ('ZZTEST-B', 'SB', 'CB')")
    admin.execute("INSERT INTO post_node (bmc_mac, serial) VALUES (%s, %s)", (M1, SN1))
    state.assign_node(M1, SN1, {"order_no": "ZZTEST-B", "sales": "X", "customer": "Y",
                                "population": "P"}, HDR)
    assert admin.execute("SELECT assigned_sales, assigned_customer, assigned_population "
                         "FROM post_node WHERE bmc_mac=%s", (M1,)).fetchone() == (None, None, "P")
    assert _labels(admin, "ZZTEST-B") == ("SB", "CB", None, False)


def test_mark_finished_overwrites(db):
    admin, _ = db
    from flax_post import state
    admin.execute("INSERT INTO post_order (order_no, sales, customer) VALUES ('ZZTEST-M', 'S1', 'C1')")
    state.mark_finished("ZZTEST-M", sales=None, customer="C2", counts={"n": 1})
    assert _labels(admin, "ZZTEST-M") == (None, "C2", None, True)


def test_order_labels_includes_finished(db):
    admin, _ = db
    from flax_post import state
    admin.execute("INSERT INTO post_order (order_no, sales, population) VALUES ('ZZTEST-O', 'SO', 'PO')")
    admin.execute("INSERT INTO post_order (order_no, customer, finished_at) "
                  "VALUES ('ZZTEST-D', 'CD', now())")
    got = state.order_labels()
    assert got["ZZTEST-O"] == {"sales": "SO", "customer": None, "population": "PO", "finished_at": None}
    assert got["ZZTEST-D"]["customer"] == "CD" and got["ZZTEST-D"]["finished_at"]
    assert state.order_row("ZZTEST-O")["population"] == "PO"
    rows = {o["order_no"]: o for o in state.list_orders(include_finished=True)}
    assert rows["ZZTEST-O"]["population"] == "PO" and "ZZTEST-D" in rows
