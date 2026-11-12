"""Tests for flax_observe.host_probe.lookup_kea_ip — resolve a MAC's v4 IP
from Kea's Postgres backend (replaces the retired dnsmasq.leases/dhcp-hosts
file lookup now that Kea owns DHCP)."""
from unittest.mock import MagicMock


def _mock_pool(fetchone_results):
    """Build a pool whose cursor.fetchone() yields fetchone_results in order
    (one per conn.execute call)."""
    cursor = MagicMock()
    cursor.fetchone.side_effect = list(fetchone_results)
    conn = MagicMock()
    conn.execute.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    return pool, conn, cursor


def test_lookup_kea_ip_returns_live_lease_first():
    from flax_observe.host_probe import lookup_kea_ip
    pool, conn, cur = _mock_pool([("172.17.0.50",)])
    assert lookup_kea_ip(pool, "1c:34:da:7f:b3:a4") == "172.17.0.50"
    # only the lease4 query ran (early return)
    assert conn.execute.call_count == 1
    assert "kea.lease4" in conn.execute.call_args[0][0]


def test_lookup_kea_ip_falls_back_to_reservation():
    from flax_observe.host_probe import lookup_kea_ip
    # lease miss (None) then reservation hit
    pool, conn, cur = _mock_pool([(None,), ("172.16.7.101",)])
    assert lookup_kea_ip(pool, "a8:2b:b5:b8:01:d4") == "172.16.7.101"
    assert conn.execute.call_count == 2
    assert "kea.hosts" in conn.execute.call_args_list[1][0][0]


def test_lookup_kea_ip_none_when_absent_in_both():
    from flax_observe.host_probe import lookup_kea_ip
    pool, conn, cur = _mock_pool([(None,), None])
    assert lookup_kea_ip(pool, "de:ad:be:ef:00:00") is None


def test_lookup_kea_ip_none_for_empty_mac():
    from flax_observe.host_probe import lookup_kea_ip
    assert lookup_kea_ip(MagicMock(), "") is None


def test_lookup_kea_ip_strips_mac_separators():
    from flax_observe.host_probe import lookup_kea_ip
    pool, conn, cur = _mock_pool([("172.17.0.50",)])
    lookup_kea_ip(pool, "1C:34:DA:7F:B3:A4")
    params = conn.execute.call_args[0][1]
    assert params[0] == "1c34da7fb3a4"


def test_kea_lease_fresh_true_when_cltt_after_boundary():
    from unittest.mock import MagicMock
    from flax_observe import host_probe
    import datetime
    expire = datetime.datetime(2026, 6, 22, 4, 45, 0, tzinfo=datetime.timezone.utc)
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (expire, 600)  # cltt = 04:35
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock(); pool.connection.return_value = conn
    assert host_probe.kea_lease_fresh(pool, "98:03:9b:90:7c:3c",
                                      "2026-06-22T04:00:00Z") is True


def test_kea_lease_fresh_false_when_cltt_before_boundary():
    from unittest.mock import MagicMock
    from flax_observe import host_probe
    import datetime
    expire = datetime.datetime(2026, 6, 22, 3, 45, 0, tzinfo=datetime.timezone.utc)
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (expire, 600)  # cltt = 03:35
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock(); pool.connection.return_value = conn
    assert host_probe.kea_lease_fresh(pool, "98:03:9b:90:7c:3c",
                                      "2026-06-22T04:00:00Z") is False


def test_kea_lease_fresh_false_when_no_lease():
    from unittest.mock import MagicMock
    from flax_observe import host_probe
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = None
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock(); pool.connection.return_value = conn
    assert host_probe.kea_lease_fresh(pool, "98:03:9b:90:7c:3c",
                                      "2026-06-22T04:00:00Z") is False


def test_kea_lease_fresh_true_when_no_boundary_and_active_lease():
    from unittest.mock import MagicMock
    from flax_observe import host_probe
    import datetime
    expire = datetime.datetime(2026, 6, 22, 3, 45, 0, tzinfo=datetime.timezone.utc)
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (expire, 600)
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock(); pool.connection.return_value = conn
    assert host_probe.kea_lease_fresh(pool, "98:03:9b:90:7c:3c", None) is True


def test_nginx_pxe_seen_boundary_fresh(tmp_path):
    from flax_observe import host_probe
    log = tmp_path / "access.log"
    log.write_text(
        '10.0.0.5 - - [22/Jun/2026:04:30:00 +0000] '
        '"GET /suse/live/test/LiveLeap HTTP/1.1" 200\n')
    assert host_probe.nginx_pxe_seen(str(log), "10.0.0.5",
                                     "2026-06-22T04:00:00Z") == "found"


def test_nginx_pxe_seen_boundary_stale(tmp_path):
    from flax_observe import host_probe
    log = tmp_path / "access.log"
    log.write_text(
        '10.0.0.5 - - [22/Jun/2026:03:30:00 +0000] '
        '"GET /suse/live/test/LiveLeap HTTP/1.1" 200\n')
    assert host_probe.nginx_pxe_seen(str(log), "10.0.0.5",
                                     "2026-06-22T04:00:00Z") == "notfound"


def test_nginx_pxe_seen_no_boundary_is_legacy(tmp_path):
    from flax_observe import host_probe
    log = tmp_path / "access.log"
    log.write_text(
        '10.0.0.5 - - [22/Jun/2026:03:30:00 +0000] '
        '"GET /suse/live/test/LiveLeap HTTP/1.1" 200\n')
    assert host_probe.nginx_pxe_seen(str(log), "10.0.0.5") == "found"


def test_ssh_uptime_password_in_env_not_argv(monkeypatch):
    import flax_observe.host_probe as hp
    seen = {}

    class _CP:
        returncode = 0

    def _run(argv, **kw):
        seen.update(argv=argv, **kw)
        return _CP()
    monkeypatch.setattr(hp.subprocess, "run", _run)
    assert hp.ssh_uptime("10.0.0.1", [{"user": "root", "pass": "fake-host-pw"}]) == "ok"
    assert seen["argv"][:2] == ["sshpass", "-e"]
    assert not any("fake-host-pw" in a for a in seen["argv"])
    assert seen["env"]["SSHPASS"] == "fake-host-pw"
