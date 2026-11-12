import json
from flax_post.biosd import creds

def test_load_host_creds_prefers_root(tmp_path):
    p = tmp_path / "credentials-host.json"
    p.write_text(json.dumps([{"user": "flax", "pass": "x"}, {"user": "root", "pass": "test-root-pw"}]))
    assert creds.load_host_creds(str(p)) == ("root", "test-root-pw")

def test_load_host_creds_falls_back_to_first(tmp_path):
    p = tmp_path / "credentials-host.json"
    p.write_text(json.dumps([{"user": "flax", "pass": "y"}]))
    assert creds.load_host_creds(str(p)) == ("flax", "y")
