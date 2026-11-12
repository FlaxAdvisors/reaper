"""reaper status / ha, end to end with only ssh and ansible-playbook faked."""
import json
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli.__main__ import Context, main  # noqa: E402
from reapercli.lib.ansible import playbook_argv  # noqa: E402
from reapercli.lib.remote import Result  # noqa: E402
from reapercli.lib.sites import load_sites  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SITES = load_sites(ROOT)


def bang_facts(vip, recovery):
    pg = ({"in_recovery": True, "alembic": "042", "wal_receiver": "streaming", "replay_lag_bytes": 0}
          if recovery else
          {"in_recovery": False, "alembic": "042", "slot_retained_bytes": 0, "freshness": {},
           "replication": [{"addr": "192.168.88.22", "state": "streaming", "lag_bytes": 0}]})
    return {"vip": vip, "keepalived": {"state": "active"}, "pg": pg,
            "host": {"disks": [{"mount": "/", "used_pct": 40}], "mem_avail_pct": 50,
                     "failed_units": [], "tailscale": {}, "load1": 0.1},
            "units": {"_present": []}, "containers": {}, "timers": {}, "crons": [],
            "listen": [], "files": {}, "images": [], "deployed": {}}


def ctx_for(tmp_path, *sites, ssh=None, play=None):
    for s in sites:
        (tmp_path / f".vault_pass_{s}").write_text("x\n")

    def runner(argv, stdin, timeout):
        if "flax@bang-gouda" in argv:
            return Result(0, json.dumps(bang_facts(True, False)), "")
        return Result(0, json.dumps(bang_facts(False, True)), "")
    return Context(root=ROOT, home=tmp_path, hostname="orbit", runner=ssh or runner, play_runner=play)


def test_playbook_argv_dormant_limit():
    argv = playbook_argv(SITES["braintree"], ROOT, "pg_ha_check.yml")
    assert argv[argv.index("--limit") + 1] == "all:!bang-siesta"


def test_playbook_argv_tags_no_limit():
    argv = playbook_argv(SITES["eindhoven"], ROOT, "apply_bang_services.yml", tags=("flax_post",))
    assert argv[argv.index("--tags") + 1] == "flax_post"
    assert "--limit" not in argv
    assert "-i" in argv and argv[argv.index("-i") + 1].endswith("inventories/eindhoven/hosts.yml")


def test_status_fails_when_vip_holder_misses_flax_post(tmp_path, capsys):
    rc = main(["status", "eindhoven"], ctx_for(tmp_path, "eindhoven"))
    out = capsys.readouterr().out
    assert rc == 1                                   # nothing is running on gouda in the fake
    assert out.index(" ha ") < out.index(" infra ")
    assert "flax_post" in out


def test_status_json_is_rows(tmp_path, capsys):
    main(["status", "eindhoven", "--json"], ctx_for(tmp_path, "eindhoven"))
    data = json.loads(capsys.readouterr().out)
    assert any(r["section"] == "ha" and r["item"] == "vip" and r["status"] == "ok" for r in data)


def test_ha_status_ok_pair(tmp_path, capsys):
    assert main(["ha", "status", "eindhoven"], ctx_for(tmp_path, "eindhoven")) == 0
    assert "streaming" in capsys.readouterr().out


def test_ha_check_skips_dormant_standby_site(tmp_path, capsys):
    calls = []
    rc = main(["ha", "check", "braintree"],
              ctx_for(tmp_path, "braintree", play=lambda argv, env: calls.append(argv) or 0))
    assert rc == 0 and calls == []
    assert "standby dormant" in capsys.readouterr().out


def test_ha_check_runs_assert_playbook(tmp_path):
    calls = []
    main(["ha", "check", "eindhoven"], ctx_for(tmp_path, "eindhoven", play=lambda a, e: calls.append((a, e)) or 0))
    argv, env = calls[0]
    assert any(a.endswith("pg_ha_check.yml") for a in argv)
    assert "ha_check_mode=assert" in argv and env["REAPER_SITE"] == "eindhoven"


def test_playbooks_use_reapers_own_ansible_cfg(tmp_path):
    calls = []
    main(["ha", "check", "eindhoven"], ctx_for(tmp_path, "eindhoven", play=lambda a, e: calls.append(e) or 0))
    assert calls[0]["ANSIBLE_CONFIG"] == str(ROOT / "ansible.cfg")
