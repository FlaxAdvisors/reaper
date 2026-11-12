"""Consume the existing services: switch_facts read + per-port join (pure)."""
from unittest.mock import MagicMock
from flax_post import consume


def _dev(kind, port="et6b1", leased=True):
    ip = "172.24.6.101" if kind == "bmc" else "172.24.7.101"
    return {"mac": "aa:bb:cc:00:00:01" if kind == "bmc" else "aa:bb:cc:00:00:02",
            "reservation_ip": ip, "lease_ip": ip if leased else None,
            "switch": "rabbit-edam", "port": port, "kind": kind, "leased": leased}


def test_switch_ports_projects(monkeypatch):
    conn = MagicMock(); conn.__enter__.return_value = conn; conn.__exit__.return_value = False
    conn.execute.return_value.fetchall.return_value = [
        ("rabbit-edam", {"Ethernet6/1": {"link": "link", "macs": ["aa:bb:cc:00:00:01"]}})]
    pool = MagicMock(); pool.connection.return_value = conn
    monkeypatch.setattr(consume, "get_pool", lambda: pool)
    out = consume.switch_ports("rabbit-edam")
    assert out["Ethernet6/1"]["link"] == "link"


def test_consumed_by_port_joins_link_mac_lease():
    facts = {"Ethernet6/1": {"link": "link",
                             "macs": ["AA:BB:CC:00:00:01", "aa:bb:cc:00:00:02"],
                             "bmc_mac": "aa:bb:cc:00:00:01"}}
    out = consume.consumed_by_port([_dev("bmc"), _dev("host")], facts, "rabbit-edam")
    c = out["et6b1"]
    assert c["link"] == "link"
    assert c["bmc_mac"] == "aa:bb:cc:00:00:01" and c["host_mac"] == "aa:bb:cc:00:00:02"
    assert c["bmc_mac_seen"] is True and c["host_mac_seen"] is True   # case-insensitive
    assert c["bmc_ip"] == "172.24.6.101" and c["host_ip"] == "172.24.7.101"
    assert c["bmc_leased"] is True and c["bmc_reserved"] is True


def test_consumed_by_port_no_link_no_macseen():
    facts = {"Ethernet6/1": {"link": "notconnect", "macs": []}}
    c = consume.consumed_by_port([_dev("bmc", leased=False)], facts, "rabbit-edam")["et6b1"]
    assert c["link"] == "nolink"
    assert c["bmc_mac_seen"] is False
    assert c["bmc_leased"] is False and c["bmc_reserved"] is True
