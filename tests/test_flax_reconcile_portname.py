from flax_reconcile.portname import to_internal, to_arista


def test_to_internal_arista_long():
    assert to_internal("Ethernet6/1") == "et6b1"


def test_to_internal_arista_short():
    assert to_internal("Et6/1") == "et6b1"


def test_to_internal_passthrough_internal():
    assert to_internal("et6b1") == "et6b1"


def test_to_internal_passthrough_swp():
    assert to_internal("swp6") == "swp6"


def test_to_internal_is_inverse_of_to_arista():
    assert to_internal(to_arista("et6b1")) == "et6b1"
