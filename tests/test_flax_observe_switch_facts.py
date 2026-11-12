"""Tests for flax_observe.switch_facts."""
from unittest.mock import MagicMock, patch


def test_load_all_switches_returns_per_port_dict(monkeypatch):
    """load_all() returns {(switch, port): port_fact_dict} from one SELECT."""
    from flax_observe import switch_facts

    cursor = MagicMock()
    cursor.fetchall.return_value = [
        ("rabbit-lorax", {"Et6/1": {"link": "link", "macs": ["aa"]},
                          "Et6/2": {"link": "nolink", "macs": []}}),
    ]
    cursor.__enter__.return_value = cursor
    cursor.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    pool = MagicMock()
    pool.connection.return_value = conn
    monkeypatch.setattr(switch_facts, "get_pool", lambda: pool)

    out = switch_facts.load_all()
    assert out[("rabbit-lorax", "Et6/1")]["link"] == "link"
    assert out[("rabbit-lorax", "Et6/2")]["link"] == "nolink"


def test_cache_get_port_returns_current_value():
    from flax_observe.switch_facts import SwitchFactsCache
    cache = SwitchFactsCache()
    cache._replace_all({
        ("rabbit-lorax", "Et6/1"): {"link": "link", "macs": ["aa"]},
    })
    assert cache.get_port("rabbit-lorax", "Et6/1") == {"link": "link", "macs": ["aa"]}
    assert cache.get_port("rabbit-lorax", "Et99/1") is None


def test_cache_replace_is_atomic_under_concurrent_reads():
    """Multi-threaded read should never see a half-updated cache state."""
    import threading
    from flax_observe.switch_facts import SwitchFactsCache
    cache = SwitchFactsCache()
    cache._replace_all({("sw", f"Et{i}/1"): {"link": "link", "v": i}
                        for i in range(100)})

    errors: list[str] = []

    def reader():
        for _ in range(1000):
            port = cache.get_port("sw", "Et5/1")
            if port is not None and port.get("v") != 5:
                errors.append(f"saw {port}")

    def writer():
        for v in range(50):
            cache._replace_all({("sw", f"Et{i}/1"): {"link": "link", "v": i}
                                for i in range(100)})

    threads = [threading.Thread(target=reader) for _ in range(4)] + \
              [threading.Thread(target=writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:3]


def test_ports_with_mac_normalises_and_scans_all_switches():
    from flax_observe.switch_facts import SwitchFactsCache
    c = SwitchFactsCache()
    c._replace_all({("rabbit-a", "Ethernet6/1"): {"mask": "access", "macs": ["AA:BB:CC:00:00:01"]},
                    ("rabbit-b", "Ethernet7/1"): {"mask": "access", "macs": ["aa:bb:cc:00:00:01"]},
                    ("rabbit-a", "Ethernet8/1"): {"mask": "access", "macs": []},
                    ("rabbit-a", "Ethernet9/1"): {}})
    assert c.ports_with_mac("aa:bb:cc:00:00:01") == {("rabbit-a", "Ethernet6/1"),
                                                     ("rabbit-b", "Ethernet7/1")}
    assert c.ports_with_mac(None) == set()


def test_ports_with_mac_excludes_trunk_and_unmasked_ports():
    """A MAC learned on a trunk/uplink (or a port with no mask recorded) is
    not "seen elsewhere" for identity-hold purposes — only access ports
    count, matching enroll.py's steerable-port filter."""
    from flax_observe.switch_facts import SwitchFactsCache
    c = SwitchFactsCache()
    c._replace_all({("rabbit-a", "Ethernet6/1"): {"mask": "access", "macs": ["aa:bb:cc:00:00:01"]},
                    ("rabbit-a", "Ethernet7/1"): {"mask": "trunk", "macs": ["aa:bb:cc:00:00:01"]},
                    ("rabbit-a", "Ethernet8/1"): {"macs": ["aa:bb:cc:00:00:01"]}})
    assert c.ports_with_mac("aa:bb:cc:00:00:01") == {("rabbit-a", "Ethernet6/1")}
