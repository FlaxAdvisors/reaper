"""reaper test: group table -> test files (spec 2026-11-13 reaper-test-command)."""
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli.lib import testgroups as tg  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def tree(tmp_path, *names):
    for n in names:
        p = tmp_path / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
    return tmp_path


def rel(root, paths):
    return [str(p.relative_to(root)) for p in paths]


def test_union_dedups(tmp_path):
    r = tree(tmp_path, "tests/test_flax_post_a.py", "tests/test_flax_rack_b.py", "tests/test_flax_rungs_c.py")
    files, shell = tg.resolve(r, ["post", "post-lane"])
    assert rel(r, files) == ["tests/test_flax_post_a.py", "tests/test_flax_rack_b.py", "tests/test_flax_rungs_c.py"]
    assert shell == []


def test_flax_composite_covers_new_service(tmp_path):
    r = tree(tmp_path, "tests/test_flax_newsvc_a.py", "tests/test_flax_post_a.py")
    files, _ = tg.resolve(r, ["flax"])
    assert "tests/test_flax_newsvc_a.py" in rel(r, files)


def test_shell_excludes_venv_builder(tmp_path):
    r = tree(tmp_path, "scripts/test-venv.sh", "scripts/test-bmc-fw-update.sh", "tests/test_flax_post_a.py")
    files, shell = tg.resolve(r, ["shell"])
    assert files == []
    assert rel(r, shell) == ["scripts/test-bmc-fw-update.sh"]


def test_unknown_group_lists_names(tmp_path):
    r = tree(tmp_path, "tests/test_flax_post_a.py")
    with pytest.raises(tg.GroupError) as e:
        tg.resolve(r, ["bogus"])
    assert "bogus" in str(e.value) and "post" in str(e.value)


def test_empty_group_is_an_error(tmp_path):
    r = tree(tmp_path, "tests/test_flax_post_a.py")
    with pytest.raises(tg.GroupError) as e:
        tg.resolve(r, ["rack"])
    assert "rack" in str(e.value)


def test_path_passes_through(tmp_path):
    r = tree(tmp_path, "tests/test_x.py", "scripts/test-y.sh")
    files, shell = tg.resolve(r, ["tests/test_x.py", "scripts/test-y.sh"])
    assert rel(r, files) == ["tests/test_x.py"]
    assert rel(r, shell) == ["scripts/test-y.sh"]


def test_no_words_is_everything(tmp_path):
    r = tree(tmp_path, "tests/test_a.py", "tests/test_flax_post_b.py", "tests/helper.py",
             "scripts/test-venv.sh", "scripts/test-z.sh")
    files, shell = tg.resolve(r, [])
    assert rel(r, files) == ["tests/test_a.py", "tests/test_flax_post_b.py"]
    assert rel(r, shell) == ["scripts/test-z.sh"]


def test_real_repo_every_group_nonempty():
    for name in tg.group_names():
        files, shell = tg.resolve(ROOT, [name])
        assert files or shell, name


def test_ungrouped_lists_unmatched(tmp_path):
    r = tree(tmp_path, "tests/test_misc.py", "tests/test_flax_post_a.py")
    assert rel(r, tg.ungrouped(r)) == ["tests/test_misc.py"]


def test_nodeid_passes_through(tmp_path):
    r = tree(tmp_path, "tests/test_x.py")
    files, _ = tg.resolve(r, ["tests/test_x.py::test_one"])
    assert rel(r, files) == ["tests/test_x.py::test_one"]


def test_path_with_no_tests_is_an_error(tmp_path):
    r = tree(tmp_path, "tests/fixtures/x.json", "tests/test_a.py")
    with pytest.raises(tg.GroupError) as e:
        tg.resolve(r, ["tests/fixtures"])
    assert "tests/fixtures" in str(e.value)


def test_directory_expands_to_its_tests(tmp_path):
    r = tree(tmp_path, "tests/test_a.py", "tests/helper.py")
    files, _ = tg.resolve(r, ["tests"])
    assert rel(r, files) == ["tests/test_a.py"]
