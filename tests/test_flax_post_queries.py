"""flax_post.queries maps raw DB rows into device dicts; lease presence drives `leased`."""
import datetime


def test_row_to_device_maps_columns_and_lease_present():
    from flax_post.queries import _row_to_device
    exp = datetime.datetime(2026, 6, 26, 12, 0, tzinfo=datetime.timezone.utc)
    # (mac, reservation_ip, lease_ip, hostname, vid, switch, port, kind, lease_state, lease_expires)
    row = ("1c:34:da:7f:b3:a4", "172.24.6.101", "172.24.6.101", "rr003et6b1-bmc",
           24, "rabbit-edam", "et6b1", "bmc", 0, exp)
    d = _row_to_device(row)
    assert d["mac"] == "1c:34:da:7f:b3:a4"
    assert d["reservation_ip"] == "172.24.6.101"
    assert d["kind"] == "bmc"
    assert d["switch"] == "rabbit-edam"
    assert d["port"] == "et6b1"
    assert d["leased"] is True
    assert d["lease_expires"] == exp.isoformat()


def test_row_to_device_no_lease_marks_unleased():
    from flax_post.queries import _row_to_device
    row = ("aa:bb:cc:00:00:01", "172.24.6.101", None, "rr003et6b1",
           24, "rabbit-edam", "et6b1", "host", None, None)
    d = _row_to_device(row)
    assert d["leased"] is False
    assert d["lease_ip"] is None
    assert d["lease_expires"] is None


def test_sql_filters_source_post_and_joins_active_lease():
    from flax_post.queries import _SQL
    assert "->>'source', '') = 'post'" in _SQL
    assert "LEFT JOIN kea.lease4 l ON l.hwaddr = h.dhcp_identifier AND l.state = 0" in _SQL
    assert "h.dhcp_identifier_type = 0" in _SQL
    assert "encode(h.dhcp_identifier, 'hex')" in _SQL


def test_lease_join_scoped_to_reservation_ip():
    """The lease join must also match l.address = h.ipv4_address, so a reservation
    only counts a lease at its OWN reserved ip. Otherwise a stale lease on a
    different subnet (e.g. a device reclassified vid25->vid24 whose old vid25 lease
    hasn't expired) joins by mac and the viewer surfaces the wrong bmc_ip."""
    from flax_post.queries import _SQL
    assert "AND l.address = h.ipv4_address" in _SQL
