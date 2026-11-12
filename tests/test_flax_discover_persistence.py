from contextlib import contextmanager

from flax_discover.persistence import next_vm_n, supersede_port, sweep_vacant_port


def test_next_vm_n_starts_at_one():
    assert next_vm_n(existing={}) == 1  # no VMs on this port yet


def test_next_vm_n_fills_lowest_gap():
    # existing: {mac: vm_n} for VMs already on this port
    assert next_vm_n(existing={"a": 1, "b": 3}) == 2


def test_next_vm_n_after_contiguous():
    assert next_vm_n(existing={"a": 1, "b": 2}) == 3


# ---- supersede_port: SQL / param-shape unit tests (no DB) ----

class _FakeCursor:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class _FakeConn:
    def __init__(self, captured, rowcount):
        self._captured = captured
        self._rowcount = rowcount

    def execute(self, sql, params):
        self._captured["sql"] = sql
        self._captured["params"] = params
        return _FakeCursor(self._rowcount)


class _FakePool:
    def __init__(self, captured, rowcount=0):
        self._captured = captured
        self._rowcount = rowcount

    @contextmanager
    def connection(self):
        yield _FakeConn(self._captured, self._rowcount)


def test_supersede_port_uses_all_array_predicate_not_in():
    captured = {}
    supersede_port(_FakePool(captured), switch="rabbit-x", port="et6b1",
                   keep_macs=["aa:bb:cc:00:00:64", "aa:bb:cc:00:00:62"])
    sql = captured["sql"]
    # psycopg3: must be `<> ALL(%s)`, never `IN`/`NOT IN` tuple expansion.
    assert "<> ALL(%(keep)s)" in sql
    assert " IN " not in sql.upper()


def test_supersede_port_keep_param_is_a_list():
    captured = {}
    supersede_port(_FakePool(captured), switch="rabbit-x", port="et6b1",
                   keep_macs=("aa:bb:cc:00:00:64",))  # tuple in
    keep = captured["params"]["keep"]
    # psycopg3 binds a list as a single array param; coerce to list regardless.
    assert isinstance(keep, list)
    assert keep == ["aa:bb:cc:00:00:64"]
    assert captured["params"]["switch"] == "rabbit-x"
    assert captured["params"]["port"] == "et6b1"


def test_supersede_port_returns_rowcount():
    assert supersede_port(_FakePool({}, rowcount=3), switch="s", port="p",
                          keep_macs=["m"]) == 3


def test_sweep_vacant_port_sql_gates_on_max_last_seen_and_db_clock():
    captured = {}
    sweep_vacant_port(_FakePool(captured), switch="rabbit-x", port="et6b1",
                      debounce_secs=600)
    sql = captured["sql"]
    assert "DELETE FROM devices" in sql
    assert "max(last_seen)" in sql          # per-port newest row
    assert "now()" in sql                   # DB clock, not the daemon clock
    assert "make_interval" in sql           # debounce applied as an interval
    # subquery must be scoped to THIS (switch, port), not all devices
    assert "d2.switch" in sql
    assert "d2.port" in sql


def test_sweep_vacant_port_params_shape():
    captured = {}
    sweep_vacant_port(_FakePool(captured), switch="rabbit-x", port="et6b1",
                      debounce_secs=600)
    assert captured["params"] == {"switch": "rabbit-x", "port": "et6b1",
                                  "debounce": 600}


def test_sweep_vacant_port_returns_rowcount():
    assert sweep_vacant_port(_FakePool({}, rowcount=2), switch="s", port="p",
                             debounce_secs=600) == 2
