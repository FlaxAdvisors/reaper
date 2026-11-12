"""reaper svc ls|logs and image ls (spec §6)."""
import json
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli.__main__ import Context, main  # noqa: E402
from reapercli.commands.svc import resolve  # noqa: E402
from reapercli.lib.components import load_components  # noqa: E402
from reapercli.lib.remote import Result  # noqa: E402
from reapercli.lib.sites import load_sites  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
COMPS = load_components(ROOT, load_sites(ROOT))


def test_resolve_component_to_first_unit():
    assert resolve("flax_post", COMPS) == ("unit", "flax-post", COMPS["flax_post"])


def test_resolve_bare_unit_wins_over_same_named_container():
    assert resolve("kea-dhcp4", COMPS)[:2] == ("unit", "kea-dhcp4")


def test_resolve_container_only_name():
    assert resolve("flax-stack-postgres-1", COMPS)[:2] == ("container", "flax-stack-postgres-1")


def test_resolve_unknown_is_none():
    assert resolve("nope", COMPS) is None


def ctx_with(tmp_path, runner):
    (tmp_path / ".vault_pass_eindhoven").write_text("x\n")
    return Context(root=ROOT, home=tmp_path, hostname="orbit", runner=runner)


def test_logs_go_to_the_vip_holder(tmp_path, capsys):
    calls = []

    def runner(argv, stdin, timeout):
        calls.append(argv)
        if argv[-1].startswith("ip "):
            return Result(0, "inet 192.168.88.10/24" if "flax@bang-gouda" in argv else "inet 192.168.88.22/24", "")
        return Result(0, "a log line\n", "")
    assert main(["svc", "logs", "eindhoven", "flax_post", "-n", "5"], ctx_with(tmp_path, runner)) == 0
    log_call = calls[-1]
    assert "flax@bang-gouda" in log_call and "journalctl -u flax-post" in log_call[-1] and "-n 5" in log_call[-1]
    assert "a log line" in capsys.readouterr().out


def test_logs_unknown_name_exits_2(tmp_path):
    assert main(["svc", "logs", "eindhoven", "nope"], ctx_with(tmp_path, lambda *a: Result(0, "", ""))) == 2


def facts(images):
    return {"vip": False, "host": {}, "units": {"_present": []}, "containers": {}, "images": images,
            "pg": {}, "keepalived": {"state": "active"}}


def test_image_ls_missing_on_standby_fails(tmp_path, capsys):
    pins = {}
    for line in (ROOT / "inventories/eindhoven/group_vars/all/vars.yml").read_text().splitlines():
        if line.startswith("apply_flax_post_image_tag:"):
            pins["post"] = line.split('"')[1]
    have = [f"flax-control:{pins['post']}"]

    def runner(argv, stdin, timeout):
        imgs = have if "flax@bang-gouda" in argv else []
        return Result(0, json.dumps(facts(imgs)), "")
    rc = main(["image", "ls", "eindhoven"], ctx_with(tmp_path, runner))
    out = capsys.readouterr().out
    assert rc == 1
    assert any("bang-edam" in l and f"flax-control:{pins['post']}" in l and "FAIL" in l for l in out.splitlines())
