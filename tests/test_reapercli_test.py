"""reaper test|tests (spec 2026-11-13 reaper-test-command): runs groups, parallel by default."""
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli.__main__ import COMMANDS, Context, main  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class Run:
    """Fake test runner: records (argv, capture), answers scripted rcs (pytest calls first)."""

    def __init__(self, rcs=None, shell_rc=None):
        self.calls, self.rcs, self.shell_rc = [], list(rcs or []), dict(shell_rc or {})

    def __call__(self, argv, capture):
        self.calls.append((list(argv), capture))
        if argv[0] == "bash":
            name = Path(argv[1]).name
            return self.shell_rc.get(name, 0), f"output of {name}\n"
        return (self.rcs.pop(0) if self.rcs else 0), ""

    def pytest_calls(self):
        return [a for a, _ in self.calls if a[0] != "bash"]


def ctx(tmp_path, run):
    return Context(root=ROOT, home=tmp_path, hostname="orbit", test_runner=run)


def test_alias_tests_dispatches(tmp_path):
    assert COMMANDS["tests"] == COMMANDS["test"]
    assert main(["tests", "--list"], ctx(tmp_path, Run())) == 0


def test_header_names_tree(tmp_path, capsys):
    main(["test", "--list"], ctx(tmp_path, Run()))
    assert capsys.readouterr().out.splitlines()[0].startswith("testing " + str(ROOT))


def test_default_is_parallel_two_passes(tmp_path):
    r = Run()
    assert main(["test", "cli", "--no-shell"], ctx(tmp_path, r)) == 0
    a, b = r.pytest_calls()
    assert a[0] == str(ROOT / "scripts" / "run-tests.sh")
    assert a[-4:] == ["-n", "auto", "-m", "not serial"]
    assert b[-2:] == ["-m", "serial"] and "-n" not in b
    assert str(ROOT / "tests" / "test_reapercli_test.py") in a


def test_j_and_serial(tmp_path):
    r = Run()
    main(["test", "cli", "--no-shell", "-j", "4"], ctx(tmp_path, r))
    assert r.pytest_calls()[0][-4:-2] == ["-n", "4"]
    r = Run()
    main(["test", "cli", "--no-shell", "--serial"], ctx(tmp_path, r))
    (only,) = r.pytest_calls()
    assert "-n" not in only and "-m" not in only


def test_serial_pass_no_tests_is_ok(tmp_path):
    assert main(["test", "cli", "--no-shell"], ctx(tmp_path, Run(rcs=[0, 5]))) == 0


def test_serial_pass_failure_fails(tmp_path):
    assert main(["test", "cli", "--no-shell"], ctx(tmp_path, Run(rcs=[0, 1]))) == 1


def test_passthrough_overrides_parallel(tmp_path):
    r = Run()
    main(["test", "cli", "--no-shell", "--", "-x", "--pdb"], ctx(tmp_path, r))
    (only,) = r.pytest_calls()
    assert only[-2:] == ["-x", "--pdb"] and "-n" not in only


def test_site_name_refused(tmp_path, capsys):
    r = Run()
    assert main(["test", "eindhoven"], ctx(tmp_path, r)) == 2
    assert "runs locally" in capsys.readouterr().err
    assert r.calls == []


def test_unknown_group_rc2(tmp_path):
    r = Run()
    assert main(["test", "bogus"], ctx(tmp_path, r)) == 2
    assert r.calls == []


def test_shell_failure_fails_run(tmp_path, capsys):
    r = Run(shell_rc={"test-bmc-fw-update.sh": 1})
    assert main(["test", "--shell"], ctx(tmp_path, r)) == 1
    out = capsys.readouterr().out
    assert "FAIL test-bmc-fw-update.sh" in out
    assert r.pytest_calls() == []
    assert all(capture for a, capture in r.calls)
    assert not any(a[1].endswith("test-venv.sh") for a, _ in r.calls)


def test_list_runs_nothing(tmp_path, capsys):
    r = Run()
    assert main(["test", "--list"], ctx(tmp_path, r)) == 0
    assert r.calls == []
    assert "(ungrouped)" in capsys.readouterr().out


def test_completion_offers_groups(tmp_path, capsys):
    main(["completion"], ctx(tmp_path, Run()))
    assert "post-lane" in capsys.readouterr().out
