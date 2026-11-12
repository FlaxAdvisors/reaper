from flax_control import config_map_view

ACKS = [{"consumer":"flax-classify","consumed_at":"2026-07-09T14:00:00Z"}]

def test_live_when_file_older_than_ack():
    rows = config_map_view.build_config_map(
        [{"name":"vlans.json","decides":"vid","readers":["flax-classify"],"mtime":1000.0}],
        ACKS, now_ts=2000.0)
    # ack is 2026 (recent epoch); file mtime 1000 is far older -> live
    assert rows[0]["live_state"] == "live"

def test_absent_when_no_mtime():
    rows = config_map_view.build_config_map(
        [{"name":"gone.json","decides":"x","readers":["flax-classify"],"mtime":None}],
        ACKS, now_ts=2000.0)
    assert rows[0]["live_state"] == "absent"

def test_drift_when_file_newer_than_all_reader_acks():
    # file mtime is now; reader last acked long ago -> drift
    rows = config_map_view.build_config_map(
        [{"name":"vlans.json","decides":"vid","readers":["flax-classify"],"mtime":4102444800.0}],
        ACKS, now_ts=4102444800.0)
    assert rows[0]["live_state"] == "drift"

def test_reader_with_no_ack_is_ignored_for_drift():
    rows = config_map_view.build_config_map(
        [{"name":"x.json","decides":"x","readers":["flax-post"],"mtime":1000.0}],
        ACKS, now_ts=2000.0)
    # no ack for flax-post -> can't prove drift -> live
    assert rows[0]["live_state"] == "live"

def test_newest_ack_wins_across_multiple_same_consumer_rows():
    # flax-classify has two ack rows; the STALE one is iterated LAST
    # (mimicking consumer_acks ORDER BY consumer, source). Newest must win.
    acks = [
        {"consumer": "flax-classify", "consumed_at": "2026-07-09T14:00:00Z"},  # fresh
        {"consumer": "flax-classify", "consumed_at": "2026-07-09T02:00:00Z"},  # stale, later in list
    ]
    # file mtime sits between the two ack times: with newest-ack (14:00) it's LIVE;
    # a buggy last-write-wins (02:00) would call it DRIFT.
    import datetime
    mtime = datetime.datetime(2026,7,9,8,0,0,tzinfo=datetime.timezone.utc).timestamp()
    rows = config_map_view.build_config_map(
        [{"name":"vlans.json","decides":"vid","readers":["flax-classify"],"mtime":mtime}],
        acks, now_ts=mtime)
    assert rows[0]["live_state"] == "live"


from fastapi.testclient import TestClient
from unittest.mock import patch

def test_platform_pairs_in_catalogue_with_no_readers():
    """platform-pairs.json is record-only: catalogued for visibility, but no
    flax service consumes it, so it must declare an empty readers list --
    a non-empty one would make the drift view demand acks that never come."""
    entry = [c for c in config_map_view.CATALOGUE if c[0] == "platform-pairs.json"]
    assert len(entry) == 1, "platform-pairs.json missing from CATALOGUE"
    name, decides, readers = entry[0]
    assert readers == [], f"record-only file must have no readers, got {readers}"
    assert decides, "every catalogue entry needs a 'decides' description"


def test_config_page_renders_map():
    from flax_control.app import app
    with patch("flax_control.queries.consumer_acks", return_value=[]), \
         patch("flax_control.config_view.reconcile_tunables", return_value={}), \
         patch("flax_control.config_view.topology_files", return_value=[]), \
         patch("flax_control.config_view.classifier_dirs", return_value=[]), \
         patch("flax_control.config_view.site_env_switches", return_value={}), \
         patch("flax_control.config_view.credentials_reference", return_value=[]), \
         patch("flax_control.config_view.bmc_fw_manifest", return_value={}):
        resp = TestClient(app).get("/config")
    assert resp.status_code == 200
    assert b"Config in use" in resp.content
    assert b"vlans.json" in resp.content
