import sys
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli.__main__ import Context, main  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _ctx(tmp_path):
    return Context(root=ROOT, home=tmp_path, hostname="orbit")


def test_no_args_is_usage(tmp_path):
    assert main([], _ctx(tmp_path)) == 2


def test_unknown_subcommand_is_usage(tmp_path):
    assert main(["nope"], _ctx(tmp_path)) == 2


def test_dispatch_is_lazy(tmp_path):
    sys.modules.pop("reapercli.commands.status", None)
    with pytest.raises(SystemExit) as e:
        main(["doctor", "--help"], _ctx(tmp_path))
    assert e.value.code == 0
    assert "reapercli.commands.status" not in sys.modules


def test_net_is_a_python_command_not_a_passthrough():
    import reapercli.__main__ as m
    assert m.COMMANDS["net"] == "reapercli.commands.net"
    assert not hasattr(m, "PASSTHROUGH")
