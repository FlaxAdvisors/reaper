"""reaper ipxe: the TFTP NBP build + install, never part of a deploy (operator 2026-11-10:
the build is not byte-reproducible, so every pxe_payloads deploy replaced ipxe.efi)."""
from pathlib import Path

import pytest
import yaml

pytest.importorskip("reapercli")
from reapercli.__main__ import main  # noqa: E402
from test_reapercli_deploy import Fake, deploys  # noqa: E402

ROLE = Path(__file__).resolve().parents[1] / "roles" / "apply_pxe_payloads"


def test_plan_only_without_yes(tmp_path, capsys):
    f = Fake()
    assert main(["ipxe", "eindhoven"], f.ctx(tmp_path)) == 0
    assert f.plays == []
    out = capsys.readouterr().out
    assert "ipxe eindhoven:" in out and "bang-gouda" in out and "818aaaa5e" in out
    assert "(plan only: add --yes to build and install)" in out


def test_yes_builds_with_the_flag_tftp_only_inside_the_ha_guard(tmp_path):
    f = Fake()
    assert main(["ipxe", "eindhoven", "--yes"], f.ctx(tmp_path)) == 0
    [argv] = deploys(f)
    assert argv[-1].endswith("apply_bang_services.yml")
    assert argv[argv.index("--tags") + 1] == "tftp"
    assert "apply_pxe_payloads_ipxe_build=True" in argv
    assert "apply_pxe_payloads_ipxe_install=True" in argv
    seq = [x for x in f.log if not x.startswith("record")]
    assert seq == ["pg_ha_check.yml", "stop bang-edam", "apply_bang_services.yml tftp",
                   "start bang-edam", "pg_ha_check.yml"]
    assert f.stdin["bang-gouda"][0]["component"] == "ipxe"


def test_failed_play_exits_1_and_records_nothing(tmp_path):
    f = Fake(play_rc={"apply_bang_services.yml": 2})
    assert main(["ipxe", "eindhoven", "--yes"], f.ctx(tmp_path)) == 1
    assert not [x for x in f.log if x.startswith("record")]


def test_role_never_builds_or_installs_nbps_by_default():
    d = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    assert d["apply_pxe_payloads_ipxe_build"] is False and d["apply_pxe_payloads_ipxe_install"] is False
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
    nbp = [t for t in tasks if "tftp" in (t.get("tags") or [])]
    assert len(nbp) == 6
    gates = [t.get("when") for t in nbp]
    assert gates == ["apply_pxe_payloads_ipxe_build | bool"] * 4 + \
        ["apply_pxe_payloads_ipxe_install | bool"] * 2


def test_cached_installs_without_building(tmp_path):
    f = Fake()
    assert main(["ipxe", "eindhoven", "--cached", "--yes"], f.ctx(tmp_path)) == 0
    [argv] = deploys(f)
    assert "apply_pxe_payloads_ipxe_install=True" in argv
    assert "apply_pxe_payloads_ipxe_build=False" in argv
