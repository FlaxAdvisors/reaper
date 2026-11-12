# tests/test_flax_classify_legacy_import.py
"""Tests for flax_classify.legacy_import (dnsmasq dhcp-host -> kea.hosts)."""
from unittest.mock import MagicMock


def _mock_pool():
    cursor = MagicMock()
    cursor.fetchone.return_value = (0,)
    conn = MagicMock()
    conn.execute.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool, conn, cursor


def test_parse_dhcp_host_basic():
    from flax_classify.legacy_import import parse_dhcp_host_lines
    text = "dhcp-host=1c:34:da:7f:b3:a4,172.17.6.101,leopard6b1-bmc\n"
    out = parse_dhcp_host_lines(text)
    assert out == [{"mac": "1c:34:da:7f:b3:a4",
                    "ipv4_address": "172.17.6.101",
                    "hostname": "leopard6b1-bmc",
                    "vid": 17}]


def test_parse_dhcp_host_no_hostname():
    from flax_classify.legacy_import import parse_dhcp_host_lines
    out = parse_dhcp_host_lines("dhcp-host=aa:bb:cc:dd:ee:ff,172.24.3.1\n")
    assert out[0]["hostname"] == ""
    assert out[0]["vid"] == 24


def test_parse_bare_hostsdir_lines():
    """reaper-leased's /etc/dnsmasq.dhcp-hosts/* use the BARE format
    (mac,ip,name without the dhcp-host= prefix). Must be captured too."""
    from flax_classify.legacy_import import parse_dhcp_host_lines
    text = (
        "# /etc/dnsmasq.dhcp-hosts/triage\n"
        "08:c0:eb:d6:03:16,172.24.10.2,leopard10b2\n"
        "08:c0:eb:d6:03:18,172.24.10.102,leopard10b2-bmc\n"
        "9a:76:a8:36:4b:7f,172.24.25.201,leopard25b1vm1\n"
    )
    out = parse_dhcp_host_lines(text)
    assert len(out) == 3
    assert out[0] == {"mac": "08:c0:eb:d6:03:16",
                      "ipv4_address": "172.24.10.2",
                      "hostname": "leopard10b2", "vid": 24}
    assert out[2]["hostname"] == "leopard25b1vm1"


def test_parse_mixed_both_formats():
    """A capture concatenating hostsdir (bare) + bmc-reservations.conf
    (dhcp-host=) must yield all reservations from both."""
    from flax_classify.legacy_import import parse_dhcp_host_lines
    text = (
        "08:c0:eb:d6:03:16,172.24.10.2,leopard10b2\n"
        "dhcp-host=a8:2b:b5:b8:01:d4,192.168.88.13,rabbit-lorax-bmc,set:bmc\n"
    )
    out = parse_dhcp_host_lines(text)
    assert {r["mac"] for r in out} == {"08:c0:eb:d6:03:16", "a8:2b:b5:b8:01:d4"}


def test_parse_dhcp_host_tolerates_set_tag_and_comments():
    from flax_classify.legacy_import import parse_dhcp_host_lines
    text = (
        "# a comment\n"
        "interface=eth0.17\n"
        "dhcp-host=set:v17,aa:bb:cc:11:22:33,172.17.9.2,host9\n"
    )
    out = parse_dhcp_host_lines(text)
    assert len(out) == 1
    assert out[0]["mac"] == "aa:bb:cc:11:22:33"
    assert out[0]["ipv4_address"] == "172.17.9.2"
    assert out[0]["vid"] == 17


def test_parse_dhcp_host_mgmt_subnet_maps_to_mgmt_id():
    from flax_classify.legacy_import import parse_dhcp_host_lines, MGMT_SUBNET_ID
    out = parse_dhcp_host_lines("dhcp-host=98:03:9b:b5:18:5c,192.168.88.13,wedge-bmc\n")
    assert out[0]["vid"] == MGMT_SUBNET_ID


def test_import_reservations_tags_legacy_import():
    from flax_classify.legacy_import import import_reservations
    pool, conn, cursor = _mock_pool()
    entries = [{"mac": "1c:34:da:7f:b3:a4", "ipv4_address": "172.17.6.101",
                "hostname": "x", "vid": 17}]
    n = import_reservations(pool, entries)
    assert n == 1
    sql, params = conn.execute.call_args[0]
    assert "INSERT INTO kea.hosts" in sql
    assert "legacy-import" in sql or params.get("source") == "legacy-import"
    assert params["dhcp4_subnet_id"] == 17


def test_coverage_gap_returns_uncovered_macs(monkeypatch):
    """coverage_gap returns the set of captured macs NOT present in kea.hosts."""
    from flax_classify.legacy_import import coverage_gap
    pool, conn, cursor = _mock_pool()
    # DB reports only one of the two macs present.
    cursor.fetchall.return_value = [(bytes.fromhex("1c34da7fb3a4"),)]
    entries = [
        {"mac": "1c:34:da:7f:b3:a4", "ipv4_address": "172.17.6.101", "hostname": "", "vid": 17},
        {"mac": "aa:bb:cc:dd:ee:ff", "ipv4_address": "172.24.3.1", "hostname": "", "vid": 24},
    ]
    gap = coverage_gap(pool, entries)
    assert gap == {"aabbccddeeff"}


def test_parse_dhcp_host_ignores_lease_time_suffix():
    from flax_classify.legacy_import import parse_dhcp_host_lines
    for suffix in ("1h", "45m", "30s", "3600"):
        out = parse_dhcp_host_lines(
            f"dhcp-host=aa:bb:cc:dd:ee:ff,172.17.6.101,realhost,{suffix}\n")
        assert out[0]["hostname"] == "realhost", suffix
    # lease time with NO hostname must not become the hostname
    out = parse_dhcp_host_lines("dhcp-host=aa:bb:cc:dd:ee:ff,172.17.6.101,1h\n")
    assert out[0]["hostname"] == ""


def test_vid_for_ip_raises_on_unknown_range():
    import pytest
    from flax_classify.legacy_import import parse_dhcp_host_lines
    with pytest.raises(ValueError):
        parse_dhcp_host_lines("dhcp-host=aa:bb:cc:dd:ee:ff,10.0.0.1,host\n")


def test_adopt_owned_legacy_rows_retags_by_desired_owner():
    """The one-shot cutover remediation: adopt re-tags kea.hosts rows still
    tagged source='legacy-import' whose mac now has a desired_reservations
    row, setting source to that row's owner_role -- but ONLY those rows.
    Sites migrated straight into the enforce architecture (no engine-era
    source pre-tagging) leave every flax-owned mac tagged legacy-import,
    which the materializer treats as permanently NOT-OURS. Verifies the SQL
    scopes to legacy-import overlap and returns the updated rowcount."""
    from flax_classify.legacy_import import adopt_owned_legacy_rows
    pool, conn, cursor = _mock_pool()
    cursor.rowcount = 25
    n = adopt_owned_legacy_rows(pool)
    assert n == 25
    sql = conn.execute.call_args[0][0]
    assert "UPDATE kea.hosts" in sql
    # scoped to the desired-reservations overlap...
    assert "desired_reservations" in sql
    # ...and ONLY rows still tagged legacy-import (never touches an already
    # role-owned row, nor a legacy-import row with no desired overlap).
    assert "'legacy-import'" in sql
    # re-tagged to the DESIRED row's owner, not a hardcoded role.
    assert "owner_role" in sql
    # The mac-join regexp replacement MUST reach Postgres with DOUBLED
    # backslashes so its E-string yields backreferences (\1..\6), not octal
    # escapes. A non-raw Python literal collapses "\\1" -> "\1" and the join
    # silently matches 0 rows (see _ADOPT_SQL's note). Guard the string layer.
    assert r"E'\\1:\\2:\\3:\\4:\\5:\\6'" in sql
