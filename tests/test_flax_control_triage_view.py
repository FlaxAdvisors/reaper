"""Tests for flax_control.triage_view — the triage node/state table builder.

Mirrors the old switchportrecon 10988 dashboard: only geometry.json (triage rack)
ports, one row per port, the 12 observe vars color-classified, OU-sorted.
"""
from flax_control import triage_view


def _obs(switch, port, **vars_values):
    """Build an observe_state_all-shaped entry; vars_values is {var: value}."""
    return {
        "switch": switch, "port": port,
        "vars": {k: {"value": v, "since": "2026-06-19T00:00:00Z"}
                 for k, v in vars_values.items()},
        "last_polled": "2026-06-19T12:00:00Z", "resolved": {},
    }


GEOM = [
    {"port": "et6b2", "ou": "22C", "switch": "rabbit-lorax"},
    {"port": "et6b1", "ou": "20L", "switch": "rabbit-lorax"},
]


def test_classify_value_good_bad_neutral():
    for v in ("link", "found", "ok", "on", "openbmc", "traditional"):
        assert triage_view.classify_value(v) == "good"
    for v in ("nolink", "fail", "notfound", "off"):
        assert triage_view.classify_value(v) == "bad"
    for v in ("unknown", "absent", "", None, "weird"):
        assert triage_view.classify_value(v) == "neutral"


def test_build_rows_only_geometry_ports_and_ou_sorted():
    observe = {
        "rabbit-lorax:Ethernet6/1": _obs("rabbit-lorax", "Ethernet6/1",
                                         linkstate="link", bmcping="ok"),
        # a turtle swp port NOT in geometry -> must be excluded
        "turtle-lorax:swp9": _obs("turtle-lorax", "swp9", linkstate="link"),
    }
    rows = triage_view.build_rows(GEOM, observe)
    assert [r["ou"] for r in rows] == ["20L", "22C"]          # OU-sorted
    assert all(r["switch"] == "rabbit-lorax" for r in rows)    # no turtle row
    assert len(rows) == 2


def test_build_rows_canonicalizes_port_forms_and_fills_vars():
    """observe key uses the long Arista form; geometry uses et6b1 — they match."""
    observe = {"rabbit-lorax:Ethernet6/1":
               _obs("rabbit-lorax", "Ethernet6/1", linkstate="link",
                    bmcping="fail", inventory="unknown")}
    rows = triage_view.build_rows(GEOM, observe)
    row = next(r for r in rows if r["ou"] == "20L")
    assert row["observed"] is True
    assert row["last_polled"] == "2026-06-19T12:00:00Z"
    cells = {c["name"]: c for c in row["cells"]}
    assert cells["linkstate"]["value"] == "link" and cells["linkstate"]["cls"] == "good"
    assert cells["bmcping"]["value"] == "fail" and cells["bmcping"]["cls"] == "bad"
    assert cells["inventory"]["cls"] == "neutral"
    # every var in the canonical order is present as a cell
    assert [c["name"] for c in row["cells"]] == list(triage_view.VAR_ORDER)


def test_build_rows_unobserved_port_is_all_neutral_unknown():
    """A geometry port with no observe_state row still renders (observed=False),
    all cells unknown/neutral."""
    rows = triage_view.build_rows(GEOM, {})  # no observe data at all
    row = rows[0]
    assert row["observed"] is False
    assert all(c["value"] == "unknown" and c["cls"] == "neutral" for c in row["cells"])


def test_build_rows_exposes_port_link_forms():
    rows = triage_view.build_rows(GEOM, {})
    row = next(r for r in rows if r["ou"] == "20L")
    assert row["port"] == "Et6/1"            # display form
    assert row["port_url"] == "Ethernet6/1"  # link target (switch_detail form)


def test_triage_status_exposes_platform_fields():
    from flax_control.triage_compat import observe_row_to_triage_status
    row = {
        "port": "et6b1",
        "vars": {"chassissn": {"value": "found", "since": "2026-06-22T00:00:00Z"}},
        "resolved": {
            "chassis_sn": "ABC123", "bmc_mac": "aa:bb:cc:dd:ee:ff",
            "product_name": "Tioga Pass", "bmc_kind": "openbmc",
            "link_session_since": "2026-06-22T01:02:03Z",
        },
        "last_polled": "2026-06-22T00:00:01Z",
    }
    out = observe_row_to_triage_status(row, ou="20L")
    assert out["product_name"] == "Tioga Pass"
    assert out["bmc_kind"] == "openbmc"
    assert out["link_session_since"] == "2026-06-22T01:02:03Z"
