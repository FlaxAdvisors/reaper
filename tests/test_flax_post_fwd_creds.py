"""flax_post.fwd.creds loads Redfish BMC creds, failing closed to []."""


def test_loads_valid_creds(tmp_path):
    p = tmp_path / "creds.json"
    p.write_text('[{"bmcuser": "root", "bmcpass": "test-obmc-pw"}]')
    from flax_post.fwd import creds
    out = creds.load_redfish_creds(str(p))
    assert out == [{"bmcuser": "root", "bmcpass": "test-obmc-pw"}]


def test_missing_file_returns_empty(tmp_path):
    from flax_post.fwd import creds
    assert creds.load_redfish_creds(str(tmp_path / "nope.json")) == []


def test_vault_encrypted_returns_empty(tmp_path):
    p = tmp_path / "creds.json"
    p.write_text("$ANSIBLE_VAULT;1.1;AES256\ndeadbeef\n")
    from flax_post.fwd import creds
    assert creds.load_redfish_creds(str(p)) == []


def test_malformed_and_non_list_return_empty(tmp_path):
    bad = tmp_path / "bad.json"; bad.write_text("{not json")
    obj = tmp_path / "obj.json"; obj.write_text('{"bmcuser": "x"}')
    from flax_post.fwd import creds
    assert creds.load_redfish_creds(str(bad)) == []
    assert creds.load_redfish_creds(str(obj)) == []


def test_drops_entries_missing_fields(tmp_path):
    p = tmp_path / "creds.json"
    p.write_text('[{"bmcuser": "root", "bmcpass": "p"}, {"bmcuser": "x"}]')
    from flax_post.fwd import creds
    assert creds.load_redfish_creds(str(p)) == [{"bmcuser": "root", "bmcpass": "p"}]


def test_default_creds_path_is_bmc_json(monkeypatch):
    monkeypatch.delenv("FLAX_POST_FWD_CREDS", raising=False)
    import importlib
    from flax_post.fwd import creds as c
    importlib.reload(c)
    assert c.REDFISH_CREDENTIALS_PATH == "/etc/flax/credentials-bmc.json"
