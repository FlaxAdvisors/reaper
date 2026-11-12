"""reaper migrate (spec §6): ha check -> apply_migrations.yml -> ha check."""
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli.__main__ import Context, main  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class Play:
    def __init__(self, rcs=None):
        self.calls, self.rcs = [], list(rcs or [])

    def __call__(self, argv, env):
        self.calls.append(argv)
        return self.rcs.pop(0) if self.rcs else 0

    def names(self):
        return [a[-1].rsplit("/", 1)[-1] for a in self.calls]


def ctx(tmp_path, play):
    (tmp_path / ".vault_pass_eindhoven").write_text("x\n")
    return Context(root=ROOT, home=tmp_path, hostname="orbit", play_runner=play)


def test_migrate_order(tmp_path):
    p = Play()
    assert main(["migrate", "eindhoven", "--tag", "0.9.207", "--yes"], ctx(tmp_path, p)) == 0
    assert p.names() == ["pg_ha_check.yml", "apply_migrations.yml", "pg_ha_check.yml"]
    assert "migration_image_tag=0.9.207" in p.calls[1]


def test_migrate_refuses_unhealthy(tmp_path):
    p = Play(rcs=[2])
    assert main(["migrate", "eindhoven", "--yes"], ctx(tmp_path, p)) == 3
    assert p.names() == ["pg_ha_check.yml"]


def test_migrate_failed_post_check_exits_1(tmp_path):
    p = Play(rcs=[0, 0, 2])
    assert main(["migrate", "eindhoven", "--yes"], ctx(tmp_path, p)) == 1


def test_migrate_plan_only(tmp_path, capsys):
    p = Play()
    assert main(["migrate", "eindhoven"], ctx(tmp_path, p)) == 0
    assert p.calls == []
    assert "apply_migrations.yml" in capsys.readouterr().out
