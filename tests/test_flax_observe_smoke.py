"""Smoke tests for the flax-observe package."""


def test_package_imports():
    import flax_observe
    assert flax_observe.__version__ == "0.9.87"


def test_sentinel_check_canonicalizes_internal_port():
    """An Arista-form sentinel row must freeze an et6b1-form observe port.

    flax-reconcile writes intentional_flap sentinels in Arista canonical form
    (Ethernet6/1); flax-observe's PortWorker calls intentional_flap_active with
    the geometry port in internal short form (et6b1). make_sentinel_check must
    canonicalize the lookup key so the freeze is seen (spec §9).
    """
    from flax_observe.__main__ import make_sentinel_check

    cache = {"set": {("rabbit-x", "Ethernet6/1")}}
    active = make_sentinel_check(cache)

    # Internal-form lookup must match the Arista-form sentinel.
    assert active("rabbit-x", "et6b1") is True
    # Arista-form lookup also matches (idempotent canonicalizer).
    assert active("rabbit-x", "Ethernet6/1") is True
    # Different port must not match.
    assert active("rabbit-x", "et7b1") is False
    # Different switch must not match.
    assert active("rabbit-y", "et6b1") is False


def test_sentinel_check_reads_latest_snapshot():
    """The predicate reads the mutable cache, so a refresh is reflected."""
    from flax_observe.__main__ import make_sentinel_check

    cache = {"set": set()}
    active = make_sentinel_check(cache)
    assert active("rabbit-x", "et6b1") is False
    cache["set"] = {("rabbit-x", "Ethernet6/1")}
    assert active("rabbit-x", "et6b1") is True
