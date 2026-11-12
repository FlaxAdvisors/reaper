"""Tests for flax_classify.kea_hosts (sole-writer upsert against kea.hosts)."""
from unittest.mock import MagicMock


def _mock_pool(fetchone_return=None, rowcount=0):
    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone_return
    cursor.fetchall.return_value = []  # default: no rows; tests override via side_effect
    cursor.rowcount = rowcount
    conn = MagicMock()
    conn.execute.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool, conn, cursor


def test_upsert_kea_host_inserts_with_user_context_classify():
    from flax_classify.kea_hosts import upsert_kea_host
    pool, conn, cursor = _mock_pool(fetchone_return=(1,))

    upsert_kea_host(
        pool,
        switch="rabbit-lorax", port="et6b1",
        mac="1c:34:da:7f:b3:a4", kind="bmc", vid=17,
        ipv4_address="172.17.6.101", hostname="unknown6b1-bmc",
        ipv6_address="fd00:17::6:101",
    )
    # call_args_list[0] is the kea.hosts upsert (calls 1+2 are the v6 writes)
    sql, params = conn.execute.call_args_list[0][0]
    assert "INSERT INTO kea.hosts" in sql
    # Real Kea schema: mac = dhcp_identifier + dhcp_identifier_type=0
    assert "dhcp_identifier" in sql
    assert "ON CONFLICT (dhcp_identifier, dhcp_identifier_type, dhcp4_subnet_id)" in sql
    # Kea's unique constraint is a partial index (WHERE dhcp4_subnet_id IS NOT
    # NULL); ON CONFLICT needs the same WHERE clause to use the partial index.
    assert "WHERE dhcp4_subnet_id IS NOT NULL" in sql
    assert "DO UPDATE" in sql
    # Read-merge-write: ON CONFLICT must concatenate (||) the new
    # classify block onto the existing user_context so operator_note
    # survives. user_context is TEXT in Kea schema -- cast to jsonb on
    # both sides of the merge, dump back to text on write.
    assert "user_context" in sql
    assert "||" in sql
    assert "jsonb_build_object" in sql
    assert "::jsonb" in sql
    assert "::text" in sql
    # mac string -> bytea decode
    assert "decode" in sql
    # ipv4_address from str -> bigint via inet arithmetic (Kea stores BIGINT)
    assert "(%(ipv4_address)s::inet - inet '0.0.0.0')::bigint" in sql
    # params bound correctly
    assert params["mac_hex"] == "1c34da7fb3a4"   # colons stripped + lower
    assert params["hostname"] == "unknown6b1-bmc"
    assert params["dhcp4_subnet_id"] == 17


def test_upsert_kea_host_writes_dhcp6_subnet_and_ipv6_reservation():
    """The upsert now (a) sets dhcp6_subnet_id = vid on the kea.hosts row in
    the INSERT + ON CONFLICT UPDATE, and (b) writes the host's
    kea.ipv6_reservations row in the SAME connection: RETURNING host_id from
    the upsert, then DELETE existing v6 rows for that host_id, then INSERT the
    v6 address (::inet). All three execute()s run on the one conn."""
    from flax_classify.kea_hosts import upsert_kea_host
    # upsert RETURNING host_id -> 42
    pool, conn, cursor = _mock_pool(fetchone_return=(42,))

    upsert_kea_host(
        pool,
        switch="rabbit-lorax", port="et6b1",
        mac="1c:34:da:7f:b3:a4", kind="bmc", vid=17,
        ipv4_address="172.17.6.101", hostname="unknown6b1-bmc",
        ipv6_address="fd00:17::6:101",
    )

    calls = conn.execute.call_args_list
    # Three executes on the SAME conn: upsert, v6 DELETE, v6 INSERT
    assert len(calls) == 3
    upsert_sql, upsert_params = calls[0][0]
    # (a) dhcp6_subnet_id threaded into the kea.hosts upsert
    assert "dhcp6_subnet_id" in upsert_sql
    assert "%(dhcp6_subnet_id)s" in upsert_sql
    assert "DO UPDATE SET" in upsert_sql
    assert "dhcp6_subnet_id = EXCLUDED.dhcp6_subnet_id" in upsert_sql
    assert upsert_params["dhcp6_subnet_id"] == 17
    # (a') the upsert returns host_id for the v6 write
    assert "RETURNING host_id" in upsert_sql

    del_sql, del_params = calls[1][0]
    assert "DELETE FROM kea.ipv6_reservations" in del_sql
    assert "host_id = %s" in del_sql
    assert del_params == (42,)   # the RETURNING host_id

    ins_sql, ins_params = calls[2][0]
    assert "INSERT INTO kea.ipv6_reservations" in ins_sql
    assert "%s::inet" in ins_sql   # address cast to inet
    assert "host_id" in ins_sql
    assert ins_params == ("fd00:17::6:101", 42)


def test_upsert_kea_host_strips_mac_separators():
    from flax_classify.kea_hosts import upsert_kea_host
    pool, conn, cursor = _mock_pool(fetchone_return=(1,))
    upsert_kea_host(
        pool, switch="rabbit-lorax", port="et6b1",
        mac="1C:34:DA:7F:B3:A4", kind="bmc", vid=17,
        ipv4_address="172.17.6.101", hostname="x",
        ipv6_address="fd00:17::6:101")
    params = conn.execute.call_args_list[0][0][1]
    assert params["mac_hex"] == "1c34da7fb3a4"


def test_read_aliases_for_macs_maps_results_back_to_input_mac():
    from unittest.mock import MagicMock
    from flax_classify import kea_hosts
    cur = MagicMock()
    cur.fetchall.return_value = [("1c34da7fb3a4", ["leo-mgmt", "x"])]
    conn = MagicMock(); conn.execute.return_value = cur
    conn.__enter__.return_value = conn; conn.__exit__.return_value = False
    pool = MagicMock(); pool.connection.return_value = conn

    out = kea_hosts.read_aliases_for_macs(
        pool, ["1c:34:da:7f:b3:a4", "1c:34:da:7f:b3:a2"])
    assert out == {"1c:34:da:7f:b3:a4": ["leo-mgmt", "x"]}  # a2 omitted (no aliases)
    sql = conn.execute.call_args[0][0]
    assert "user_context::jsonb -> 'aliases'" in sql
    assert "dhcp_identifier = ANY(%s)" in sql
    assert "dhcp_identifier_type = 0" in sql


def test_read_aliases_for_macs_empty_input_skips_db():
    from unittest.mock import MagicMock
    from flax_classify import kea_hosts
    pool = MagicMock()
    assert kea_hosts.read_aliases_for_macs(pool, []) == {}
    pool.connection.assert_not_called()


# ---------------------------------------------------------------------------
# release_leases_for_hosts
# ---------------------------------------------------------------------------

def test_release_leases_for_hosts_calls_definer_fn():
    from flax_classify.kea_hosts import release_leases_for_hosts
    pool, conn, cursor = _mock_pool(rowcount=1)
    release_leases_for_hosts(
        pool, hwaddrs=[b"\xaa\xbb\xcc\xdd\xee\xff"], v6_addrs=["fd00:2::6:65"])
    sqls = [c[0][0] for c in conn.execute.call_args_list]
    assert any("kea.flax_classify_release_leases" in s for s in sqls), \
        "must call the SECURITY DEFINER release function"
    # the hwaddrs + v6 addrs are passed as the function args
    call = next(c for c in conn.execute.call_args_list
                if "flax_classify_release_leases" in c[0][0])
    params = call[0][1]
    assert list(params[0]) == [b"\xaa\xbb\xcc\xdd\xee\xff"]
    assert list(params[1]) == ["fd00:2::6:65"]


def test_release_leases_noop_when_empty():
    from flax_classify.kea_hosts import release_leases_for_hosts
    pool, conn, cursor = _mock_pool()
    assert release_leases_for_hosts(pool, hwaddrs=[], v6_addrs=[]) is None
    conn.execute.assert_not_called()


def test_upsert_kea_host_stamps_source_when_given():
    from flax_classify.kea_hosts import upsert_kea_host
    pool, conn, cursor = _mock_pool(fetchone_return=(1,))
    upsert_kea_host(
        pool, switch="rabbit-edam", port="et6b1",
        mac="98:03:9b:75:29:ce", kind="bmc", vid=25,
        ipv4_address="172.25.6.101", hostname="rr003-et6b1-bmc",
        ipv6_address="fd00:25::6:101", source="post",
    )
    sql, params = conn.execute.call_args_list[0][0]
    # extra user_context object is merged in on both INSERT and ON CONFLICT
    assert "extra_ctx" in sql
    assert params["extra_ctx"].obj == {"source": "post"}


def test_upsert_kea_host_conflict_merge_guards_legacy_import_source():
    """The ON CONFLICT merge must not let a re-upsert's extra_ctx (e.g.
    source='triage') clobber an EXISTING row's source='legacy-import' tag --
    this upsert-time CASE guard is the sole surviving legacy-import
    protection post-3b (the global sweep that once also preserved it,
    delete_stale_kea_hosts, was retired in Task 3). Pure SQL-shape assertion:
    no DB test pattern exists for this module (tests here are all
    mocked-pool), so this checks the CASE guard's presence/shape directly in
    the upsert SQL."""
    from flax_classify.kea_hosts import upsert_kea_host
    pool, conn, cursor = _mock_pool(fetchone_return=(1,))
    upsert_kea_host(
        pool, switch="rabbit-lorax", port="et6b1",
        mac="1c:34:da:7f:b3:a4", kind="bmc", vid=17,
        ipv4_address="172.17.6.101", hostname="unknown6b1-bmc",
        ipv6_address="fd00:17::6:101", source="triage",
    )
    sql, params = conn.execute.call_args_list[0][0]
    # The guard checks the EXISTING (pre-conflict) row's source, not the
    # incoming one.
    assert "kea.hosts.user_context::jsonb) ->> 'source'" in sql
    assert "'legacy-import'" in sql
    # When the guard fires, extra_ctx's own 'source' key must be stripped
    # before merging (jsonb `-` key-delete operator), not merged verbatim.
    assert "(%(extra_ctx)s::jsonb) - 'source'" in sql
    # The non-guarded (normal) branch still merges extra_ctx as-is.
    assert "ELSE %(extra_ctx)s::jsonb" in sql
    assert params["extra_ctx"].obj == {"source": "triage"}


def test_upsert_kea_host_source_defaults_to_empty_object():
    from flax_classify.kea_hosts import upsert_kea_host
    pool, conn, cursor = _mock_pool(fetchone_return=(1,))
    upsert_kea_host(
        pool, switch="rabbit-lorax", port="et6b1",
        mac="1c:34:da:7f:b3:a4", kind="bmc", vid=17,
        ipv4_address="172.17.6.101", hostname="unknown6b1-bmc",
        ipv6_address="fd00:17::6:101",
    )
    sql, params = conn.execute.call_args_list[0][0]
    assert params["extra_ctx"].obj == {}   # no source -> empty merge, classify unchanged


# ---------------------------------------------------------------------------
# post-lane reservation lifecycle: read/delete/stamp + db_now
# ---------------------------------------------------------------------------

def test_read_post_reservations_scopes_to_source_post():
    from flax_classify.kea_hosts import read_post_reservations
    pool, conn, cursor = _mock_pool()
    cursor.fetchall.return_value = [
        ("aa:aa:aa:aa:aa:aa", "rabbit-edam", "et6b1", "bmc", "rr003-et6b1-bmc",
         26, "172.26.6.101", "fd00:26::6:101", {"link_down_since": "x"}, False),
    ]
    rows = read_post_reservations(pool)
    sql = conn.execute.call_args_list[0][0][0]
    assert "'source'" in sql and "'post'" in sql
    assert rows == [{"mac": "aa:aa:aa:aa:aa:aa", "switch": "rabbit-edam",
                     "port": "et6b1", "kind": "bmc", "hostname": "rr003-et6b1-bmc",
                     "vid": 26, "ipv4": "172.26.6.101", "ipv6": "fd00:26::6:101",
                     "post": {"link_down_since": "x"}, "operator_note": False}]


def test_read_post_sql_has_keep_set_columns():
    """The post reader gains the same keep-set fields the materializer's
    desired/actual comparisons use (Task 2, phase 3a): kind, hostname, vid,
    ipv4 (host()-wrapped), ipv6 (v6-join, same shape as _READ_ACTUALS_SQL)."""
    from flax_classify.kea_hosts import _READ_POST_SQL
    assert "'classify'->>'kind'" in _READ_POST_SQL
    assert "hostname" in _READ_POST_SQL
    assert "dhcp4_subnet_id" in _READ_POST_SQL
    # host() must wrap the ipv4 rendering here too -- same /32 bug class as
    # _READ_ACTUALS_SQL (see test_read_actuals_ipv4_rendering_strips_netmask).
    assert "host('0.0.0.0'::inet + h.ipv4_address)" in _READ_POST_SQL
    assert "('0.0.0.0'::inet + h.ipv4_address)::text" not in _READ_POST_SQL
    # ipv6 via the same LEFT JOIN shape _READ_ACTUALS_SQL uses
    assert "LEFT JOIN kea.ipv6_reservations" in _READ_POST_SQL
    assert "host(v6.address)" in _READ_POST_SQL
    # existing keep-set keys untouched
    assert "'classify'->>'switch'" in _READ_POST_SQL
    assert "'classify'->>'port'" in _READ_POST_SQL
    assert "operator_note" in _READ_POST_SQL


def test_stamp_post_timers_merges_post_key():
    from flax_classify.kea_hosts import stamp_post_timers
    pool, conn, cursor = _mock_pool()
    stamp_post_timers(pool, {"aa:aa:aa:aa:aa:aa": {"link_down_since": "t"}})
    sql, params = conn.execute.call_args_list[0][0]
    assert "UPDATE kea.hosts" in sql
    assert "'post'" in sql and "||" in sql
    assert "'post'" in sql and "'source'" in sql  # scoped to source=post


def test_stamp_post_timers_preserves_operator_note_rows():
    """Rows with a non-empty user_context.operator_note must never be
    re-stamped -- same guard as every other mutating query in this file."""
    from flax_classify.kea_hosts import stamp_post_timers
    pool, conn, cursor = _mock_pool()
    stamp_post_timers(pool, {"aa:aa:aa:aa:aa:aa": {"link_down_since": "t"}})
    sql, params = conn.execute.call_args_list[0][0]
    assert "UPDATE kea.hosts" in sql
    assert "operator_note" in sql
    assert "COALESCE((user_context::jsonb) ->> 'operator_note', '') = ''" in sql


def test_db_now_returns_timestamp():
    from flax_classify.db import db_now
    import datetime
    pool, conn, cursor = _mock_pool(fetchone_return=(datetime.datetime(2026, 7, 2),))
    assert db_now(pool) == datetime.datetime(2026, 7, 2)
    assert "now()" in conn.execute.call_args_list[0][0][0].lower()


def test_read_actuals_ipv4_rendering_strips_netmask():
    """host() must wrap the inet+bigint sum in _READ_ACTUALS_SQL.

    A bare ::text cast renders '172.17.7.4/32' (the /32 survives the cast),
    which made every converged row plan a perpetual ipv4-drift upsert on the
    live bang-gouda deploy (2026-07-04). Pin the host() wrapping so it cannot
    regress silently.
    """
    from flax_classify.kea_hosts import _READ_ACTUALS_SQL

    assert "host('0.0.0.0'::inet + h.ipv4_address)" in _READ_ACTUALS_SQL
    assert "('0.0.0.0'::inet + h.ipv4_address)::text" not in _READ_ACTUALS_SQL


def test_read_actuals_sql_has_operator_note_expression():
    """read_kea_actuals gains operator_note (bool) so the apply layer can
    guard against deleting operator-curated actual rows (phase 3a apply-layer
    safety). Same expression _READ_POST_SQL already uses."""
    from flax_classify.kea_hosts import _READ_ACTUALS_SQL
    assert ("COALESCE((h.user_context::jsonb)->>'operator_note','') <> ''"
            in _READ_ACTUALS_SQL)


def test_read_kea_actuals_row_shape_includes_operator_note():
    from flax_classify.kea_hosts import read_kea_actuals
    pool, conn, cursor = _mock_pool()
    cursor.fetchall.return_value = [
        ("aa:aa:aa:aa:aa:aa", "172.17.6.101", "unknown6b1-bmc", 17,
         "fd00:17::6:101", "triage", True, True),
    ]
    rows = read_kea_actuals(pool)
    assert rows == [{"mac": "aa:aa:aa:aa:aa:aa", "ipv4": "172.17.6.101",
                     "hostname": "unknown6b1-bmc", "vid": 17,
                     "ipv6": "fd00:17::6:101", "source": "triage",
                     "has_classify": True, "operator_note": True}]


# ---------------------------------------------------------------------------
# delete_other_subnet_rows — apply-time relocation cleanup (post-3b fix)
# ---------------------------------------------------------------------------

def test_delete_other_subnet_rows_sql_shape_scopes_mac_source_and_other_vid():
    """Same-owner vid-move cleanup: deletes THIS mac's rows tagged with THIS
    source whose dhcp4_subnet_id differs from the just-upserted keep_vid.
    operator_note-protected rows are excluded IN SQL (deliberate human
    override survives; the next-cycle multi_actual skip is then the correct
    visible outcome for that mac)."""
    from flax_classify.kea_hosts import delete_other_subnet_rows
    pool, conn, cursor = _mock_pool(rowcount=1)
    n = delete_other_subnet_rows(
        pool, mac="98:03:9b:86:86:12", source="triage", keep_vid=24)
    assert n == 1
    sqls = [c[0][0] for c in conn.execute.call_args_list]
    del_sql, del_params = next(
        c[0] for c in conn.execute.call_args_list
        if "DELETE FROM kea.hosts" in c[0][0])
    assert "dhcp_identifier_type = 0" in del_sql
    # Scoped to THIS mac (hex-encoded bytea)
    assert "dhcp_identifier = %(mac)s" in del_sql
    assert del_params["mac"] == bytes.fromhex("98039b868612")
    # Only the OTHER-subnet rows: the just-written keep_vid row survives
    assert "dhcp4_subnet_id <> %(keep_vid)s" in del_sql
    assert del_params["keep_vid"] == 24
    # Source-scoped: only the SAME owner's rows (a foreign/legacy row for
    # this mac is a genuinely ambiguous state left to the planner's
    # multi_actual skip, never silently deleted here)
    assert "->> 'source'" in del_sql and "%(source)s" in del_sql
    assert del_params["source"] == "triage"
    # operator_note exclusion lives in the SQL itself
    assert "operator_note" in del_sql
    # select-then-delete shape: the SELECT mirrors the DELETE's WHERE
    sel_sql = next(s for s in sqls if s.strip().startswith("SELECT")
                   and "FROM kea.hosts" in s)
    assert "dhcp4_subnet_id <> %(keep_vid)s" in sel_sql
    assert "operator_note" in sel_sql


def test_delete_other_subnet_rows_releases_leases(monkeypatch):
    """Mirrors delete_hosts_for_mac's select→delete→release shape: the doomed
    rows' hwaddrs + v6 reservation addresses are captured BEFORE the delete
    (cascade drops ipv6_reservations) and handed to release_leases_for_hosts."""
    import flax_classify.kea_hosts as kh
    pool, conn, cursor = _mock_pool(rowcount=1)
    cursor.fetchall.side_effect = [
        [(7, b"\x98\x03\x9b\x86\x86\x12")],   # SELECT doomed hosts
        [("fd00:17::8:84",)],                  # SELECT v6 addrs
    ]
    rel = {}
    monkeypatch.setattr(kh, "release_leases_for_hosts",
                        lambda pool, *, hwaddrs, v6_addrs: rel.update(
                            h=list(hwaddrs), v6=list(v6_addrs)))
    n = kh.delete_other_subnet_rows(
        pool, mac="98:03:9b:86:86:12", source="triage", keep_vid=24)
    assert n == 1
    assert rel["h"] == [b"\x98\x03\x9b\x86\x86\x12"]
    assert rel["v6"] == ["fd00:17::8:84"]
    sqls = [c[0][0] for c in conn.execute.call_args_list]
    v6_idx = next(i for i, s in enumerate(sqls) if "ipv6_reservations" in s)
    del_idx = next(i for i, s in enumerate(sqls) if "DELETE FROM kea.hosts" in s)
    assert v6_idx < del_idx, "v6 SELECT must precede host DELETE (cascade)"


def test_delete_other_subnet_rows_release_failure_does_not_raise(monkeypatch):
    import flax_classify.kea_hosts as kh
    pool, conn, cursor = _mock_pool(rowcount=1)
    cursor.fetchall.side_effect = [[(7, b"\x98\x03\x9b\x86\x86\x12")], []]

    def _boom(*a, **k):
        raise RuntimeError("lease db down")
    monkeypatch.setattr(kh, "release_leases_for_hosts", _boom)
    assert kh.delete_other_subnet_rows(
        pool, mac="98:03:9b:86:86:12", source="triage", keep_vid=24) == 1
