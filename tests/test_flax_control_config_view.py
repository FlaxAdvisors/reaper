"""Tests for flax_control.config_view — config file readers.

All tests use tmp_path to avoid touching /etc/flax.
Monkey-patches FLAX_CONFIG_DIR so all readers use the tmp dir.
"""
import json
import pytest

from flax_control import config_view


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content if isinstance(content, str) else json.dumps(content))
    return p


# ---------------------------------------------------------------------------
# reconcile_tunables
# ---------------------------------------------------------------------------

def test_reconcile_tunables_uses_defaults_when_file_missing(tmp_path, monkeypatch):
    """When reconcile.json is absent, every key is flagged as '(default)'."""
    from flax_control import config_view
    monkeypatch.setattr(config_view, "FLAX_CONFIG_DIR", str(tmp_path))

    rows = config_view.reconcile_tunables()
    assert len(rows) > 0
    for row in rows:
        assert row["source"] == "(default)"
    # spot-check a known key
    keys = {r["key"] for r in rows}
    assert "sweep_interval_secs" in keys


def test_reconcile_tunables_shows_file_value_for_overridden_key(tmp_path, monkeypatch):
    """When reconcile.json overrides a key, source is 'reconcile.json'."""
    from flax_control import config_view
    monkeypatch.setattr(config_view, "FLAX_CONFIG_DIR", str(tmp_path))
    _write(tmp_path, "reconcile.json", {"sweep_interval_secs": 300})

    rows = config_view.reconcile_tunables()
    row = next(r for r in rows if r["key"] == "sweep_interval_secs")
    assert row["current"] == 300
    assert row["source"] == "reconcile.json"
    # unset key falls back to default
    debounce = next(r for r in rows if r["key"] == "debounce_secs")
    assert debounce["source"] == "(default)"


def test_reconcile_tunables_all_keys_present_even_when_file_overrides_some(tmp_path, monkeypatch):
    """Every DEFAULTS key appears in output even if the file only sets one."""
    from flax_control import config_view
    from flax_reconcile.config import DEFAULTS
    monkeypatch.setattr(config_view, "FLAX_CONFIG_DIR", str(tmp_path))
    _write(tmp_path, "reconcile.json", {"max_attempts": 5})

    rows = config_view.reconcile_tunables()
    keys = {r["key"] for r in rows}
    assert keys == set(DEFAULTS.keys())


# ---------------------------------------------------------------------------
# site_env_switches — allowlist
# ---------------------------------------------------------------------------

def test_site_env_switches_returns_only_allowlisted_keys(tmp_path, monkeypatch):
    """Only the five allowed keys are surfaced; secrets are dropped."""
    from flax_control import config_view
    monkeypatch.setattr(config_view, "FLAX_CONFIG_DIR", str(tmp_path))
    env_content = (
        "MGMT_VIP=10.0.0.1\n"
        "BANG_DHCP_SERVER=rabbit-fiesta\n"
        "BANG_RECONCILE=rabbit-fiesta\n"
        "PRIMARY_RABBIT=rabbit-fiesta\n"
        "TURTLE_TYPE=arista\n"
        "SECRET_THING=should_never_appear\n"
        "SOME_PASSWORD=hunter2\n"
    )
    _write(tmp_path, "site.env", env_content)

    result = config_view.site_env_switches()
    keys = {r["key"] for r in result}
    assert "MGMT_VIP" in keys
    assert "BANG_DHCP_SERVER" in keys
    assert "PRIMARY_RABBIT" in keys
    assert "TURTLE_TYPE" in keys
    assert "SECRET_THING" not in keys
    assert "SOME_PASSWORD" not in keys


def test_site_env_switches_missing_file_returns_empty(tmp_path, monkeypatch):
    """Missing site.env -> empty list, no crash."""
    from flax_control import config_view
    monkeypatch.setattr(config_view, "FLAX_CONFIG_DIR", str(tmp_path))

    result = config_view.site_env_switches()
    assert result == []


def test_site_env_switches_partial_allowlist_shows_present(tmp_path, monkeypatch):
    """Keys not in the file are absent from results (not shown as blank)."""
    from flax_control import config_view
    monkeypatch.setattr(config_view, "FLAX_CONFIG_DIR", str(tmp_path))
    _write(tmp_path, "site.env", "MGMT_VIP=10.0.1.10\n")

    result = config_view.site_env_switches()
    keys = {r["key"] for r in result}
    assert "MGMT_VIP" in keys
    assert "TURTLE_TYPE" not in keys


# ---------------------------------------------------------------------------
# credentials_reference — static, no file reads, no secret values
# ---------------------------------------------------------------------------

def test_credentials_reference_never_reads_credential_files(tmp_path, monkeypatch):
    """credentials_reference() is pure static data — no filesystem access."""
    from flax_control import config_view
    monkeypatch.setattr(config_view, "FLAX_CONFIG_DIR", str(tmp_path))
    # Even if credential files exist on disk, they are NOT read
    _write(tmp_path, "credentials.json",
           json.dumps({"eosuser": "admin", "eospass": "test-root-pw"}))

    refs = config_view.credentials_reference()
    # Must return a list of dicts with path and key_names fields
    assert isinstance(refs, list)
    assert len(refs) == 3
    paths = {r["path"] for r in refs}
    assert "/etc/flax/credentials.json" in paths


def test_credentials_reference_has_vault_note(monkeypatch):
    """Each credential ref must mention ansible-vault."""
    from flax_control import config_view
    refs = config_view.credentials_reference()
    for ref in refs:
        assert "ansible-vault" in ref.get("note", "").lower() or \
               "vault" in ref.get("note", "").lower(), \
               f"No vault note in {ref}"


# ---------------------------------------------------------------------------
# topology_files
# ---------------------------------------------------------------------------

def test_topology_files_present_file_returns_parsed_data(tmp_path, monkeypatch):
    """A present geometry.json is returned with parsed data."""
    from flax_control import config_view
    monkeypatch.setattr(config_view, "FLAX_CONFIG_DIR", str(tmp_path))
    geom = [{"port": "Ethernet6/1", "ou": "6L"}]
    _write(tmp_path, "geometry.json", geom)

    files = config_view.topology_files()
    geom_entry = next(f for f in files if "geometry" in f["path"])
    assert geom_entry["present"] is True
    assert geom_entry["data"] == geom


def test_topology_files_missing_file_shows_not_present(tmp_path, monkeypatch):
    """A missing file returns present=False, data=None."""
    from flax_control import config_view
    monkeypatch.setattr(config_view, "FLAX_CONFIG_DIR", str(tmp_path))

    files = config_view.topology_files()
    for f in files:
        # No files exist — all should be not-present
        assert f["present"] is False
        assert f["data"] is None


def test_topology_files_has_drives_and_role_for_all_entries(tmp_path, monkeypatch):
    """Every topology file entry has 'drives' and 'deployed_by' metadata."""
    from flax_control import config_view
    monkeypatch.setattr(config_view, "FLAX_CONFIG_DIR", str(tmp_path))

    files = config_view.topology_files()
    # geometry, vlans, switches, no-steer-ports + turtle-geometry, bmc-only-families,
    # platform-pairs (record-only, added for braintree/eindhoven parity)
    # (rabbit-geometry.json retired 2026-07-06: observe enrollment is now
    # registry-driven, no longer a deployed config file)
    assert len(files) == 7
    for f in files:
        assert f.get("drives"), f"Missing 'drives' in {f['path']}"
        assert f.get("deployed_by"), f"Missing 'deployed_by' in {f['path']}"
    names = " ".join(f["path"] for f in files)
    assert "rabbit-geometry.json" not in names
    assert "turtle-geometry.json" in names
    assert "bmc-only-families.json" in names


# ---------------------------------------------------------------------------
# classifier_dirs (macmath/ + family-map/)
# ---------------------------------------------------------------------------

def test_classifier_dirs_reads_json_and_text_files(tmp_path, monkeypatch):
    from flax_control import config_view
    monkeypatch.setattr(config_view, "FLAX_CONFIG_DIR", str(tmp_path))
    mm = tmp_path / "macmath"; mm.mkdir()
    (mm / "16.json").write_text('{"vid": 16}')
    (mm / "17.json").write_text('{"vid": 17}')
    fm = tmp_path / "family-map"; fm.mkdir()
    (fm / "leopard.txt").write_text("# regexes\nLeopard.*\n")

    dirs = {d["name"]: d for d in config_view.classifier_dirs()}
    assert dirs["macmath"]["present"] and dirs["family-map"]["present"]
    mm_files = {f["name"]: f for f in dirs["macmath"]["files"]}
    assert [f["name"] for f in dirs["macmath"]["files"]] == ["16.json", "17.json"]  # sorted
    assert mm_files["16.json"]["data"] == {"vid": 16}
    fm_files = {f["name"]: f for f in dirs["family-map"]["files"]}
    assert fm_files["leopard.txt"]["data"] is None          # .txt is not JSON
    assert "Leopard" in fm_files["leopard.txt"]["raw"]       # raw still available


def test_classifier_dirs_missing_dirs_present_false(tmp_path, monkeypatch):
    from flax_control import config_view
    monkeypatch.setattr(config_view, "FLAX_CONFIG_DIR", str(tmp_path))
    dirs = config_view.classifier_dirs()
    assert {d["name"] for d in dirs} == {"macmath", "family-map"}
    assert all(d["present"] is False and d["files"] == [] for d in dirs)


# ---------------------------------------------------------------------------
# bmc_fw_manifest
# ---------------------------------------------------------------------------

def test_bmc_fw_manifest_present(tmp_path, monkeypatch):
    from flax_control import config_view
    monkeypatch.setattr(config_view, "FLAX_CONFIG_DIR", str(tmp_path))
    _write(tmp_path, "bmc-firmware-versions.json",
           {"tiogapass-openbmc": {"target_version": "flax-onetree-1.0.6-202606261452"}})
    out = config_view.bmc_fw_manifest()
    assert out["present"] is True
    assert out["data"]["tiogapass-openbmc"]["target_version"] == \
        "flax-onetree-1.0.6-202606261452"
    assert out["deployed_by"] == "apply_bmc_fw_manifest"


def test_bmc_fw_manifest_absent(tmp_path, monkeypatch):
    from flax_control import config_view
    monkeypatch.setattr(config_view, "FLAX_CONFIG_DIR", str(tmp_path))
    out = config_view.bmc_fw_manifest()
    assert out["present"] is False
    assert out["data"] is None


def test_bmc_fw_manifest_malformed(tmp_path, monkeypatch):
    from flax_control import config_view
    monkeypatch.setattr(config_view, "FLAX_CONFIG_DIR", str(tmp_path))
    (tmp_path / "bmc-firmware-versions.json").write_text("not valid json {{")
    out = config_view.bmc_fw_manifest()
    assert out["present"] is True
    assert out["data"] is None


def test_platform_pairs_in_topology_meta():
    """platform-pairs.json appears in the topology file list with the metadata
    every entry is required to carry (see
    test_topology_files_has_drives_and_role_for_all_entries)."""
    entry = [m for m in config_view._TOPOLOGY_META
             if m["name"] == "platform-pairs.json"]
    assert len(entry) == 1, "platform-pairs.json missing from _TOPOLOGY_META"
    assert entry[0]["drives"], "entry needs a 'drives' description"
    assert entry[0]["deployed_by"] == "apply_lease_agent"
