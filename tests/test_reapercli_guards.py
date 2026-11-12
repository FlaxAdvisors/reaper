"""Deploy guards (spec §8.2): refuse with exit 3 and say why."""
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli import __main__ as cli  # noqa: E402
from reapercli.__main__ import Context, main  # noqa: E402
from reapercli.lib.components import load_components, placed_on  # noqa: E402
from reapercli.lib.gather import SiteView  # noqa: E402
from reapercli.lib.guards import Refused, clone_guard, missing_images  # noqa: E402
from reapercli.lib.inventory import image_pins, site_hosts  # noqa: E402
from reapercli.lib.sites import load_sites  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SITES = load_sites(ROOT)
COMPS = load_components(ROOT, SITES)


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def make_clone(tmp_path, branch="main"):
    """A clone of a bare origin with one pushed commit on `branch`."""
    origin, clone = tmp_path / "origin.git", tmp_path / "clone"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    git(clone, "config", "user.email", "t@t")
    git(clone, "config", "user.name", "t")
    git(clone, "checkout", "-q", "-B", branch)
    (clone / "f").write_text("1\n")
    git(clone, "add", "f")
    git(clone, "commit", "-qm", "one")
    git(clone, "push", "-q", "-u", "origin", branch)
    return clone


def test_clean_pushed_main_passes(tmp_path):
    sha = clone_guard(make_clone(tmp_path))
    assert len(sha) == 40


def test_dirty_refuses(tmp_path):
    clone = make_clone(tmp_path)
    (clone / "new").write_text("x")
    with pytest.raises(Refused, match="clone dirty"):
        clone_guard(clone)


def test_unpushed_refuses(tmp_path):
    clone = make_clone(tmp_path)
    (clone / "f").write_text("2\n")
    git(clone, "commit", "-qam", "two")
    with pytest.raises(Refused, match="not on origin/main"):
        clone_guard(clone)


def test_branch_refuses_unless_named(tmp_path):
    clone = make_clone(tmp_path, branch="x")
    with pytest.raises(Refused, match="--branch x"):
        clone_guard(clone)
    assert len(clone_guard(clone, allow_branch="x")) == 40
    with pytest.raises(Refused):
        clone_guard(clone, allow_branch="y")


def view_for(site_name, images_by_bang, dormant=()):
    site = SITES[site_name]
    sh = site_hosts(site, ROOT, "orbit")
    facts = {b: ({"dormant": True} if b in dormant else {"images": imgs}) for b, imgs in images_by_bang.items()}
    placed = {c.name: placed_on(c, sh, site, ROOT) for c in COMPS.values()}
    return SiteView(site, sh, facts, COMPS, image_pins(site, ROOT), sh.primary, placed)


def test_missing_images_names_bang():
    pin = image_pins(SITES["eindhoven"], ROOT)["apply_flax_post_image_tag"]
    v = view_for("eindhoven", {"bang-gouda": [f"flax-control:{pin}"], "bang-edam": []})
    assert missing_images(v, [COMPS["flax_post"]]) == [f"bang-edam: flax-control:{pin}"]


def test_missing_images_skips_dormant():
    pin = image_pins(SITES["braintree"], ROOT)["apply_flax_post_image_tag"]
    v = view_for("braintree", {"bang-fiesta": [f"flax-control:{pin}"], "bang-siesta": []}, dormant=("bang-siesta",))
    assert missing_images(v, [COMPS["flax_post"]]) == []


def test_refused_exits_3(tmp_path, monkeypatch, capsys):
    import sys
    import types
    mod = types.ModuleType("reapercli_test_refuser")

    def run(argv, ctx):
        raise Refused("nope")
    mod.run = run
    monkeypatch.setitem(sys.modules, "reapercli_test_refuser", mod)
    monkeypatch.setitem(cli.COMMANDS, "refuser", "reapercli_test_refuser")
    assert main(["refuser"], Context(root=ROOT, home=tmp_path, hostname="orbit")) == 3
    assert "refused: nope" in capsys.readouterr().err


def test_behind_origin_refuses(tmp_path):
    clone = make_clone(tmp_path)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "origin.git"), str(other)], check=True, capture_output=True)
    git(other, "config", "user.email", "t@t")
    git(other, "config", "user.name", "t")
    (other / "g").write_text("x\n")
    git(other, "add", "g")
    git(other, "commit", "-qm", "newer")
    git(other, "push", "-q")
    git(clone, "fetch", "-q")                 # a partial pull: fetched, never merged
    with pytest.raises(Refused, match="behind origin/main"):
        clone_guard(clone)


def test_keyboardinterrupt_exits_1(tmp_path, monkeypatch, capsys):
    import sys
    import types
    mod = types.ModuleType("reapercli_test_interrupted")

    def run(argv, ctx):
        raise KeyboardInterrupt
    mod.run = run
    monkeypatch.setitem(sys.modules, "reapercli_test_interrupted", mod)
    monkeypatch.setitem(cli.COMMANDS, "interrupted", "reapercli_test_interrupted")
    assert main(["interrupted"], Context(root=ROOT, home=tmp_path, hostname="orbit")) == 1
    assert "interrupted" in capsys.readouterr().err


def test_context_has_user():
    assert Context(root=ROOT, home=ROOT, hostname="h").user
