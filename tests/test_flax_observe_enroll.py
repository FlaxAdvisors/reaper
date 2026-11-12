"""Tests for flax_observe.enroll (lab-enrollment scope: dynamic_access +
enrollment helpers) and the __main__ enrollment-scope seam."""


class FakeCache:
    """Mirrors SwitchFactsCache.ports_for(switch) -> {arista_port: fact}.

    Seeded with {(switch, arista_port): fact_dict}, same shape as the real
    cache's internal _by_port map.
    """

    def __init__(self, by_port):
        self._by_port = dict(by_port)

    def ports_for(self, switch):
        return {port: fact for (sw, port), fact in self._by_port.items()
                if sw == switch}


def test_access_ports_returned_in_internal_form():
    from flax_observe.enroll import dynamic_access
    cache = FakeCache({
        ("rabbit-lorax", "Ethernet10/2"): {"mask": "access"},
    })
    out = dynamic_access(cache, ["rabbit-lorax"], set())
    assert out == {("rabbit-lorax", "et10b2")}


def test_non_access_mask_excluded():
    from flax_observe.enroll import dynamic_access
    cache = FakeCache({
        ("rabbit-lorax", "Ethernet5/1"): {"mask": "access"},
        ("rabbit-lorax", "Ethernet49/1"): {"mask": "trunk"},
        ("rabbit-lorax", "Ethernet50/1"): {"mask": "unknown"},
        ("rabbit-lorax", "Ethernet51/1"): {},  # no mask field
    })
    out = dynamic_access(cache, ["rabbit-lorax"], set())
    assert out == {("rabbit-lorax", "et5b1")}


def test_no_steer_entry_excluded():
    from flax_observe.enroll import dynamic_access
    cache = FakeCache({
        ("rabbit-gouda", "Ethernet5/1"): {"mask": "access"},
        ("rabbit-gouda", "Ethernet32/1"): {"mask": "access"},
    })
    no_steer = {("rabbit-gouda", "Ethernet32/1")}  # Arista long form
    out = dynamic_access(cache, ["rabbit-gouda"], no_steer)
    assert out == {("rabbit-gouda", "et5b1")}


def test_multiple_switches_aggregated():
    from flax_observe.enroll import dynamic_access
    cache = FakeCache({
        ("rabbit-lorax", "Ethernet5/1"): {"mask": "access"},
        ("rabbit-gouda", "Ethernet6/2"): {"mask": "access"},
    })
    out = dynamic_access(cache, ["rabbit-lorax", "rabbit-gouda"], set())
    assert out == {("rabbit-lorax", "et5b1"), ("rabbit-gouda", "et6b2")}


def test_switch_missing_from_cache_skipped():
    from flax_observe.enroll import dynamic_access
    cache = FakeCache({
        ("rabbit-lorax", "Ethernet5/1"): {"mask": "access"},
    })
    out = dynamic_access(cache, ["rabbit-lorax", "rabbit-ghost"], set())
    assert out == {("rabbit-lorax", "et5b1")}


def test_empty_cache_returns_empty_set():
    from flax_observe.enroll import dynamic_access
    out = dynamic_access(FakeCache({}), ["rabbit-lorax"], set())
    assert out == set()


def test_empty_switch_list_returns_empty_set():
    from flax_observe.enroll import dynamic_access
    cache = FakeCache({("rabbit-lorax", "Ethernet5/1"): {"mask": "access"}})
    out = dynamic_access(cache, [], set())
    assert out == set()


# --- __main__ dynamic-enrollment scope now sources from the registry, not a
# file. These pin the seam that fed rabbit_switches -> enroll.dynamic_access.

def test_observe_switches_sourced_from_registry_reader(monkeypatch):
    """_observe_switches() derives the dynamic switch set from
    read_observe_eligible_switches(pool) -- the published role registry --
    threading the live pool through, NOT reading rabbit-geometry.json."""
    from flax_observe import __main__ as observe_main

    seen = {}

    def fake_reader(pool):
        seen["pool"] = pool
        return frozenset({"rabbit-edam"})

    monkeypatch.setattr(observe_main, "read_observe_eligible_switches", fake_reader)
    sentinel_pool = object()
    out = observe_main._observe_switches(sentinel_pool)
    assert out == ["rabbit-edam"]
    # The registry reader received the live pool (no file path anywhere).
    assert seen["pool"] is sentinel_pool


def test_observe_switches_empty_registry_falls_back_to_static(monkeypatch):
    """A None return (registry empty/unreadable) collapses to [] -> no dynamic
    enrollment -> static behaviour, the safe deploy-order fallback."""
    from flax_observe import __main__ as observe_main

    monkeypatch.setattr(observe_main, "read_observe_eligible_switches",
                        lambda pool: None)
    assert observe_main._observe_switches(object()) == []


def test_link_down_access_port_is_enrolled():
    from flax_observe.enroll import dynamic_access
    cache = FakeCache({
        ("rabbit-edam", "Ethernet7/1"): {"mask": "access", "access_vid": 25, "link": "nolink"},
        ("rabbit-edam", "Ethernet7/2"): {"mask": "access", "access_vid": 25, "link": "link"},
        ("rabbit-edam", "Ethernet7/3"): {"link": "nolink"},
    })
    out = dynamic_access(cache, ["rabbit-edam"], set())
    assert out == {("rabbit-edam", "et7b1"), ("rabbit-edam", "et7b2")}
