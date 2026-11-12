from flax_control import omnibox


def _lk(macs):  # device_lookup: colon-mac -> mac if known
    return lambda m: m if m in macs else None


def test_mac_query_resolves_to_single_mac():
    r = omnibox.resolve("aa:bb:cc:00:11:22",
                        device_lookup=_lk({"aa:bb:cc:00:11:22"}),
                        port_lookup=lambda s,p: None,
                        serial_lookup=lambda s: [],
                        hostname_lookup=lambda t: [])
    assert r == {"kind": "mac", "mac": "aa:bb:cc:00:11:22"}

def test_bare_hex_mac_is_normalised():
    r = omnibox.resolve("aabbcc001122",
                        device_lookup=_lk({"aa:bb:cc:00:11:22"}),
                        port_lookup=lambda s,p: None,
                        serial_lookup=lambda s: [], hostname_lookup=lambda t: [])
    assert r["kind"] == "mac" and r["mac"] == "aa:bb:cc:00:11:22"

def test_switch_slash_port_resolves():
    r = omnibox.resolve("rabbit-gouda/et6b1",
                        device_lookup=lambda m: None,
                        port_lookup=lambda s,p: "aa:bb:cc:00:11:22" if (s,p)==("rabbit-gouda","et6b1") else None,
                        serial_lookup=lambda s: [], hostname_lookup=lambda t: [])
    assert r == {"kind": "mac", "mac": "aa:bb:cc:00:11:22"}

def test_text_serial_and_hostname_merge_to_candidates():
    r = omnibox.resolve("SV7220",
                        device_lookup=lambda m: None, port_lookup=lambda s,p: None,
                        serial_lookup=lambda s: ["aa:bb:cc:00:11:22"],
                        hostname_lookup=lambda t: ["dd:ee:ff:00:11:22"])
    assert r["kind"] == "candidates"
    assert {c["mac"] for c in r["candidates"]} == {"aa:bb:cc:00:11:22","dd:ee:ff:00:11:22"}

def test_single_text_hit_returns_mac_not_candidates():
    r = omnibox.resolve("fl001-a3",
                        device_lookup=lambda m: None, port_lookup=lambda s,p: None,
                        serial_lookup=lambda s: [], hostname_lookup=lambda t: ["aa:bb:cc:00:11:22"])
    assert r == {"kind": "mac", "mac": "aa:bb:cc:00:11:22"}

def test_no_match_returns_none_kind():
    r = omnibox.resolve("nope",
                        device_lookup=lambda m: None, port_lookup=lambda s,p: None,
                        serial_lookup=lambda s: [], hostname_lookup=lambda t: [])
    assert r == {"kind": "none", "q": "nope"}


from fastapi.testclient import TestClient
from unittest.mock import patch

def test_search_route_redirects_on_single_mac():
    from flax_control.app import app
    with patch("flax_control.queries.device_one", return_value={"mac":"aa:bb:cc:00:11:22"}):
        resp = TestClient(app).get("/search?q=aa:bb:cc:00:11:22", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/devices/aa:bb:cc:00:11:22"

def test_search_route_renders_none_page():
    from flax_control.app import app
    with patch("flax_control.queries.device_one", return_value=None), \
         patch("flax_control.queries.device_mac_by_switch_port", return_value=None), \
         patch("flax_control.queries.device_macs_by_serial", return_value=[]), \
         patch("flax_control.queries.hostname_macs", return_value=[]):
        resp = TestClient(app).get("/search?q=nope")
    assert resp.status_code == 200
    assert b"No node matches" in resp.content
