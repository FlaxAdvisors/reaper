"""flax_post.assign: the one effective-order rule (spec 2026-11-06 per-node order)."""
from flax_post import assign

HDR = {"order_no": "RRORD-100", "sales": "S-1", "customer": "X", "population": "ovh-bare-tp"}
NODE = {"assigned_order_no": "RRORD-1033", "assigned_customer": "Acme",
        "assigned_population": "acme-leo", "assigned_sales": None, "assigned_serial": "SN1",
        "serial": "SN1", "assigned_at": "2026-11-06T05:00:00+00:00"}


def _pick(d):
    return {k: d[k] for k in ("order_no", "customer", "population", "sales", "source", "stale")}


ORDERS = {"RRORD-100": {"sales": "S-1", "customer": "X", "population": "ovh-bare-tp",
                        "finished_at": None},
          "RRORD-1033": {"sales": "S-1033", "customer": "Barivia", "population": "barivia-leo",
                         "finished_at": None}}


def test_effective_header_when_no_assignment():
    e = assign.effective(None, HDR, "SN1", ORDERS)
    assert _pick(e) == dict(HDR, source="header", stale=False)


def test_effective_uses_assignment_when_serial_matches():
    assert _pick(assign.effective(NODE, HDR, "SN1", ORDERS)) == {
        "order_no": "RRORD-1033", "customer": "Barivia", "population": "acme-leo",
        "sales": "S-1033", "source": "node", "stale": False}


def test_effective_node_labels_come_from_its_order_record():
    node = dict(NODE, assigned_customer="stale", assigned_sales="S-ORD101033")
    e = assign.effective(node, HDR, "SN1", ORDERS)
    assert (e["sales"], e["customer"]) == ("S-1033", "Barivia")


def test_effective_header_labels_from_record():
    hdr = dict(HDR, sales="S-HDR-STALE", customer="hdr-stale")
    e = assign.effective(None, hdr, "SN1", ORDERS)
    assert (e["sales"], e["customer"]) == ("S-1", "X")


def test_effective_population_chain():
    assert assign.effective(NODE, HDR, "SN1", ORDERS)["population"] == "acme-leo"
    node = dict(NODE, assigned_population=None)
    assert assign.effective(node, HDR, "SN1", ORDERS)["population"] == "barivia-leo"
    orders = dict(ORDERS, **{"RRORD-1033": dict(ORDERS["RRORD-1033"], population=None)})
    assert assign.effective(node, HDR, "SN1", orders)["population"] == "ovh-bare-tp"  # settings


def test_effective_no_record_gives_blank_labels():
    node = dict(NODE, assigned_order_no="RRORD-NEW")
    e = assign.effective(node, HDR, "SN1", ORDERS)
    assert (e["order_no"], e["sales"], e["customer"]) == ("RRORD-NEW", None, None)
    e = assign.effective(node, HDR, "SN1", None)
    assert (e["sales"], e["customer"]) == (None, None)


def test_effective_ignores_assignment_after_serial_change():
    e = assign.effective(NODE, HDR, "SN2")
    assert e["order_no"] == "RRORD-100" and e["source"] == "header" and e["stale"] is True


def test_effective_defaults_serial_to_the_nodes_own():
    assert assign.effective(NODE, HDR)["source"] == "node"


def test_order_prefix_parity_with_classify():
    from flax_classify import post_reserve
    for x in ("RRORD-100", "RRORD-1033", "RRORD-003", "", None):
        assert assign.order_prefix(x) == post_reserve.order_prefix(x), x


def test_hostname():
    assert assign.hostname("RRORD-1033", "et7b1") == "rr033-et7b1"
    assert assign.hostname(None, "et7b1") is None
