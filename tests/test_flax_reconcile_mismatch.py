from flax_reconcile.mismatch import compute_mismatches


def test_lease_matches_reservation_is_not_a_mismatch():
    leases = [{"mac": "aa:bb:cc:00:00:01", "ip": "172.17.0.5"}]
    hosts = [{"mac": "aa:bb:cc:00:00:01", "ip": "172.17.0.5"}]
    assert compute_mismatches(leases, hosts) == []


def test_lease_differs_from_reservation_is_a_mismatch():
    leases = [{"mac": "aa:bb:cc:00:00:01", "ip": "172.17.0.99"}]
    hosts = [{"mac": "aa:bb:cc:00:00:01", "ip": "172.17.0.5"}]
    out = compute_mismatches(leases, hosts)
    assert out == [{"mac": "aa:bb:cc:00:00:01",
                    "lease_ip": "172.17.0.99", "reserved_ip": "172.17.0.5"}]


def test_reservation_without_lease_is_a_mismatch():
    # Reserved but the device is not currently leased anywhere → needs a kick
    # to come up on its reservation. lease_ip is None.
    hosts = [{"mac": "aa:bb:cc:00:00:01", "ip": "172.17.0.5"}]
    out = compute_mismatches([], hosts)
    assert out == [{"mac": "aa:bb:cc:00:00:01",
                    "lease_ip": None, "reserved_ip": "172.17.0.5"}]


def test_lease_without_reservation_is_ignored():
    # Pool-mode device with no reservation → nothing to reconcile.
    leases = [{"mac": "aa:bb:cc:00:00:09", "ip": "172.17.0.40"}]
    assert compute_mismatches(leases, []) == []


def test_mac_casing_is_normalised():
    leases = [{"mac": "AA:BB:CC:00:00:01", "ip": "172.17.0.99"}]
    hosts = [{"mac": "aa:bb:cc:00:00:01", "ip": "172.17.0.5"}]
    out = compute_mismatches(leases, hosts)
    assert out[0]["mac"] == "aa:bb:cc:00:00:01"
