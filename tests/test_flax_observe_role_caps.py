from flax_observe.role_caps import _observe_switches_from_rows


def test_only_observe_capable_roles_contribute_switches():
    role_rows = [
        ("post", {"capabilities": {"observe": True}}),
        ("integra", {"capabilities": {"observe": False}}),
        ("triage", {"capabilities": {}}),          # no flag -> excluded
        ("weird", None),                            # null definition -> safe
    ]
    universe_rows = [
        ("post", "switch", "rabbit-edam"),
        ("post", "prefix", "turtle"),               # non-switch kind ignored
        ("integra", "port", "rabbit-gouda"),        # not observe-capable anyway
        ("triage", "switch", "rabbit-gouda"),       # role not observe-capable
    ]
    assert _observe_switches_from_rows(role_rows, universe_rows) == frozenset({"rabbit-edam"})


def test_empty_rows_yield_empty_set():
    assert _observe_switches_from_rows([], []) == frozenset()
