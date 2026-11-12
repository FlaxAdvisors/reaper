"""Scope grammar: reaper <sub> [site|all] [args] (spec 2026-11-07 §4)."""
import pytest

pytest.importorskip("reapercli")
from reapercli.lib.sites import ScopeError, load_sites, resolve_scope  # noqa: E402

SITES_YML = """sites:
  eindhoven: { inventory: inventories/eindhoven, topology: files/configs/eindhoven/topology.yml, state: active }
  lavergne:  { inventory: inventories/lavergne,  topology: files/configs/lavergne/topology.yml,  state: active }
  braintree: { inventory: inventories/braintree, topology: files/configs/braintree/topology.yml, state: active,
               dormant_hosts: [bang-siesta] }
  oldsite:   { inventory: inventories/oldsite,   topology: files/configs/oldsite/topology.yml,   state: dormant }
"""


@pytest.fixture
def tree(tmp_path):
    root, home = tmp_path / "repo", tmp_path / "home"
    for s in ("eindhoven", "lavergne", "braintree", "oldsite"):
        (root / "inventories" / s).mkdir(parents=True)
    (root / "files" / "reaper").mkdir(parents=True)
    (root / "files" / "reaper" / "sites.yml").write_text(SITES_YML)
    home.mkdir()
    for s in ("eindhoven", "lavergne", "oldsite"):
        (home / f".vault_pass_{s}").write_text("x\n")
    return root, home


def names(sites):
    return [s.name for s in sites]


def test_default_is_vaulted_active_sites(tree):
    sites, rest = resolve_scope(["flax_post"], *tree)
    assert names(sites) == ["eindhoven", "lavergne"]
    assert rest == ["flax_post"]


def test_all_keyword_is_consumed(tree):
    sites, rest = resolve_scope(["all", "x"], *tree)
    assert names(sites) == ["eindhoven", "lavergne"]
    assert rest == ["x"]


def test_named_site(tree):
    sites, rest = resolve_scope(["eindhoven", "flax_post"], *tree)
    assert names(sites) == ["eindhoven"]
    assert rest == ["flax_post"]


def test_named_site_without_vault_names_the_file(tree):
    with pytest.raises(ScopeError, match=r"\.vault_pass_braintree"):
        resolve_scope(["braintree"], *tree)


def test_dormant_site_excluded_and_named_refused(tree):
    assert "oldsite" not in names(resolve_scope([], *tree)[0])
    with pytest.raises(ScopeError, match="dormant"):
        resolve_scope(["oldsite"], *tree)


def test_inventory_without_entry_is_an_error(tree):
    root, home = tree
    (root / "inventories" / "newsite").mkdir()
    with pytest.raises(ScopeError, match="newsite"):
        load_sites(root)


def test_dormant_hosts_loaded(tree):
    assert load_sites(tree[0])["braintree"].dormant_hosts == ("bang-siesta",)


def test_real_sites_file_loads():
    from pathlib import Path
    sites = load_sites(Path(__file__).resolve().parents[1])
    assert set(sites) == {"eindhoven", "lavergne", "braintree"}
    assert sites["braintree"].dormant_hosts == ("bang-siesta",)
