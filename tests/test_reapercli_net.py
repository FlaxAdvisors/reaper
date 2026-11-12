"""reaper net fetch|drift|plan|push|setup (Phase 3): the switch/jump-host commands in
python, no prompts. The fake ansible-playbook plays apply_config.yml's check-mode
analysis: .check_result.json per limited device, .critical_changes.json when the
limit reaches localhost (ported from the bash-era test_reaper_push_critical_check)."""
import glob
import json
import os
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli.__main__ import Context, main  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class Plays:
    def __init__(self, configs: Path, critical=False, rc=None):
        self.configs, self.critical, self.rc = configs, critical, rc or {}
        self.calls = []

    def __call__(self, argv, env):
        self.calls.append(argv)
        name = argv[-1].rsplit("/", 1)[-1]
        if name == "apply_config.yml" and "--check" in argv:
            site_dir = self.configs / env["REAPER_SITE"]
            site_dir.mkdir(parents=True, exist_ok=True)
            limit = argv[argv.index("--limit") + 1] if "--limit" in argv else "all"
            hosts = limit.split(":")
            devs = [d.name for d in site_dir.iterdir() if d.is_dir()] if "all" in hosts else hosts
            for h in devs:
                if (site_dir / h).is_dir():
                    (site_dir / h / ".check_result.json").write_text(json.dumps({"critical": self.critical}))
            if "all" in hosts or "localhost" in hosts:
                crit = [{"device": Path(p).parent.name, "port": "x", "peer_device": "bang-gouda", "peer_port": "eth0"}
                        for p in glob.glob(str(site_dir / "*" / ".check_result.json"))
                        if json.load(open(p)).get("critical")]
                (site_dir / ".critical_changes.json").write_text(json.dumps(crit))
        return self.rc.get(name, 0)

    def named(self, name):
        return [c for c in self.calls if c[-1].endswith("/" + name)]


def limit_of(argv):
    return argv[argv.index("--limit") + 1] if "--limit" in argv else None


def extras(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]


@pytest.fixture
def lab(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    for s in ("eindhoven", "lavergne", "braintree"):
        (home / f".vault_pass_{s}").write_text("x\n")
    configs = tmp_path / "configs"
    site_dir = configs / "eindhoven"
    for dev in ("sw-a", "sw-b", "bang-gouda", "bang-edam"):
        (site_dir / dev / "config").mkdir(parents=True)
        (site_dir / dev / "config" / "running-config.txt").write_text(f"hostname {dev}\n")
    (site_dir / "topology.yml").write_text(
        "primary_turtle: turtle-gouda\njumphost_pairs:\n  stage1: bang-gouda\n  stage3: bang-edam\n")
    # stale state from an older full-site run: a critical change on sw-b
    (site_dir / ".critical_changes.json").write_text(json.dumps([{"device": "sw-b", "port": "swp49"}]))
    (site_dir / "sw-b" / ".check_result.json").write_text(json.dumps({"critical": True}))
    return home, configs, site_dir


def ctx_for(lab, plays, clone=None):
    home, configs, _ = lab
    return Context(root=ROOT, home=home, hostname="orbit", play_runner=plays, configs=configs,
                   runner=Ssh(), clone=clone)


@pytest.fixture
def clone(tmp_path):
    c = tmp_path / "clone"
    subprocess.run(["git", "init", "-q", str(c)], check=True)
    return c


def test_plan_clears_stale_results_and_runs_analysis(lab):
    plays = Plays(lab[1])
    assert main(["net", "plan", "eindhoven", "sw-a"], ctx_for(lab, plays)) == 0
    (call,) = plays.calls
    assert limit_of(call).split(":") == ["sw-a", "localhost"]
    assert "--check" in call and "--diff" in call
    assert f"config_output_dir={lab[1]}" in extras(call)
    assert json.loads((lab[2] / ".critical_changes.json").read_text()) == []
    assert not (lab[2] / "sw-b" / ".check_result.json").exists()


def test_plan_without_devices_checks_switches_and_jumphosts(lab):
    plays = Plays(lab[1])
    assert main(["net", "plan", "eindhoven"], ctx_for(lab, plays)) == 0
    assert [c[-1].rsplit("/", 1)[-1] for c in plays.calls] == ["apply_config.yml", "apply_jumphost_config.yml"]
    assert all("--check" in c for c in plays.calls)


def test_split_devices():
    from reapercli.commands.net import split_devices
    assert split_devices(["bang-gouda", "turtle-gouda"]) == ("bang-gouda", "turtle-gouda")
    assert split_devices(["sw-a", "sw-b"]) == ("", "sw-a:sw-b")
    assert split_devices([]) == ("", "")


def test_fetch_limits_to_devices(lab):
    plays = Plays(lab[1])
    assert main(["net", "fetch", "eindhoven", "sw-a", "bang-edam"], ctx_for(lab, plays)) == 0
    (call,) = plays.named("collect_config.yml")
    assert limit_of(call) == "sw-a:bang-edam"
    assert "--check" not in call


def test_fetch_without_scope_runs_every_site(lab):
    plays = Plays(lab[1])
    assert main(["fetch"], ctx_for(lab, plays)) == 0
    assert len(plays.named("collect_config.yml")) == 3


def test_devices_need_one_site(lab):
    plays = Plays(lab[1])
    assert main(["fetch", "all", "sw-a"], ctx_for(lab, plays)) == 2
    assert plays.calls == []


def test_drift_is_check_diff(lab):
    plays = Plays(lab[1])
    assert main(["net", "drift", "eindhoven"], ctx_for(lab, plays)) == 0
    (call,) = plays.calls
    assert call[-1].endswith("collect_config.yml") and "--check" in call and "--diff" in call


def test_failed_site_exits_1(lab):
    plays = Plays(lab[1], rc={"collect_config.yml": 2})
    assert main(["fetch", "eindhoven"], ctx_for(lab, plays)) == 1


def test_setup_renders_every_site_in_scope(lab, clone):
    plays = Plays(lab[1])
    home = lab[0]
    assert main(["setup"], ctx_for(lab, plays, clone)) == 0
    calls = plays.named("render_ssh_jumphosts.yml")
    assert len(calls) == 3
    for c in calls:
        assert f"output_file={home / '.ssh' / 'config.d' / 'reaper-jumphosts'}" in extras(c)
    assert (home / ".ssh" / "config.d").is_dir()
    assert oct((home / ".ssh" / "config.d").stat().st_mode & 0o777) == "0o700"


def test_setup_finishes_the_users_shell(lab, clone, monkeypatch):
    """reaper on PATH, completion + pdsh lines, a python3.11 user-base PATH line moved to
    python3.12, ~/jumphosts.txt -- all for the user's HOME, idempotently."""
    home = lab[0]
    monkeypatch.setenv("PATH", f"{home}/.local/bin:/usr/bin:/bin")
    (home / ".bashrc").write_text("export PATH=$(python3.11 -m site --user-base)/bin:$PATH\n")
    ssh = home / ".ssh" / "config.d"
    ssh.mkdir(parents=True)

    class Render(Plays):
        def __call__(self, argv, env):
            (ssh / "reaper-jumphosts").write_text("Host bang-gouda\n  User flax\nHost bang-edam\n")
            return super().__call__(argv, env)

    assert main(["setup", "eindhoven"], ctx_for(lab, Render(lab[1]), clone)) == 0
    link = home / ".local" / "bin" / "reaper"
    assert link.is_symlink() and os.readlink(link) == str(clone / "reaper")
    rc = (home / ".bashrc").read_text()
    assert "python3.11" not in rc and "export PATH=$(python3.12 -m site --user-base)/bin:$PATH" in rc
    assert f". {clone}/scripts/reaper-completion.bash" in rc.splitlines()
    assert "export WCOLL=~/jumphosts.txt" in rc and "export PDSH_RCMD_TYPE=ssh" in rc
    assert (home / "jumphosts.txt").read_text() == "bang-gouda\nbang-edam\n"
    assert main(["setup", "eindhoven"], ctx_for(lab, Render(lab[1]), clone)) == 0
    assert (home / ".bashrc").read_text() == rc                        # idempotent


def test_setup_installs_the_rule_zero_hooks_in_the_clone(lab, clone):
    assert main(["setup", "eindhoven"], ctx_for(lab, Plays(lab[1]), clone)) == 0
    for h in ("pre-commit", "pre-push"):
        hook = (clone / ".git" / "hooks" / h).read_text()
        assert "reaper-secrets-check" in hook and f'exec "$c" {h}' in hook


def test_setup_fails_when_a_foreign_hook_blocks_the_install(lab, clone):
    (clone / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
    plays = Plays(lab[1])
    assert main(["setup", "eindhoven"], ctx_for(lab, plays, clone)) == 1
    assert plays.named("render_ssh_jumphosts.yml")          # the ssh config is still written


def test_aliases_route_to_net(lab):
    a, b = Plays(lab[1]), Plays(lab[1])
    assert main(["plan", "eindhoven", "sw-a"], ctx_for(lab, a)) == 0
    assert main(["net", "plan", "eindhoven", "sw-a"], ctx_for(lab, b)) == 0
    assert a.calls == b.calls and a.calls


def test_net_without_sub_is_usage(lab):
    assert main(["net"], ctx_for(lab, Plays(lab[1]))) == 2
    assert main(["net", "nope"], ctx_for(lab, Plays(lab[1]))) == 2


# ---- push ----------------------------------------------------------------------------

@pytest.fixture
def staged(lab, monkeypatch):
    import reapercli.lib.staged_push as sp
    seen = []
    monkeypatch.setattr(sp, "preflight", lambda site_dir, changes: seen.append(changes) or 0)
    return seen


def state(lab):
    p = lab[0] / ".cache" / "reaper" / "staged-push-eindhoven.json"
    return json.loads(p.read_text()) if p.exists() else None


def test_single_device_push_ignores_stale_critical_state(lab, capsys):
    plays = Plays(lab[1])
    assert main(["push", "eindhoven", "sw-a", "--yes"], ctx_for(lab, plays)) == 0
    out = capsys.readouterr().out
    assert "STAGED PUSH REQUIRED" not in out
    check = [c for c in plays.calls if "--check" in c]
    apply_ = [c for c in plays.calls if "--check" not in c]
    assert len(check) == 1 and len(apply_) == 1
    assert limit_of(check[0]).split(":") == ["sw-a", "localhost"]
    assert "--diff" not in check[0]
    assert limit_of(apply_[0]) == "sw-a"
    assert not (lab[2] / "sw-b" / ".check_result.json").exists()


def test_single_device_push_still_stages_a_real_critical_change(lab, capsys):
    (lab[2] / ".critical_changes.json").write_text("[]")   # stale "all clear"
    plays = Plays(lab[1], critical=True)
    assert main(["push", "eindhoven", "sw-a"], ctx_for(lab, plays)) == 0
    out = capsys.readouterr().out
    assert "STAGED PUSH REQUIRED" in out and "sw-a" in out
    assert "reaper push eindhoven sw-a --stage 1 --yes" in out
    assert not [c for c in plays.calls if "--check" not in c]


def test_push_without_yes_applies_nothing(lab, capsys):
    plays = Plays(lab[1])
    assert main(["push", "eindhoven"], ctx_for(lab, plays)) == 0
    assert plays.calls and all("--check" in c for c in plays.calls)
    assert "rerun with --yes" in capsys.readouterr().out


def test_critical_push_with_yes_but_no_stage_refuses(lab):
    plays = Plays(lab[1], critical=True)
    assert main(["push", "eindhoven", "sw-a", "--yes"], ctx_for(lab, plays)) == 3
    assert not [c for c in plays.calls if "--check" not in c]


def test_stage1_records_plan_and_applies_stage1_jh(lab, staged):
    plays = Plays(lab[1], critical=True)
    assert main(["push", "eindhoven", "sw-a", "--stage", "1", "--yes"], ctx_for(lab, plays)) == 0
    st = state(lab)
    assert st["done"] == [1] and st["limit"] == "sw-a" and st["stage1_jh"] == "bang-gouda"
    (apply_,) = [c for c in plays.calls if "--check" not in c]
    assert apply_[-1].endswith("apply_jumphost_config.yml") and limit_of(apply_) == "bang-gouda"
    assert len(staged) == 1


def test_stage1_without_yes_records_nothing(lab, staged):
    plays = Plays(lab[1], critical=True)
    assert main(["push", "eindhoven", "sw-a", "--stage", "1"], ctx_for(lab, plays)) == 0
    assert state(lab) is None
    assert not [c for c in plays.calls if "--check" not in c]


def test_stage1_refuses_when_nothing_is_critical(lab, staged):
    plays = Plays(lab[1])
    assert main(["push", "eindhoven", "sw-a", "--stage", "1", "--yes"], ctx_for(lab, plays)) == 3
    assert state(lab) is None


def test_stage1_preflight_failure_exits_1(lab, monkeypatch):
    import reapercli.lib.staged_push as sp
    monkeypatch.setattr(sp, "preflight", lambda *a: 1)
    plays = Plays(lab[1], critical=True)
    assert main(["push", "eindhoven", "sw-a", "--stage", "1", "--yes"], ctx_for(lab, plays)) == 1
    assert state(lab) is None and not [c for c in plays.calls if "--check" not in c]


def stage1(lab):
    plays = Plays(lab[1], critical=True)
    assert main(["push", "eindhoven", "sw-a", "--stage", "1", "--yes"], ctx_for(lab, plays)) == 0


def test_stage2_refuses_without_stage1(lab, staged):
    plays = Plays(lab[1], critical=True)
    assert main(["push", "eindhoven", "sw-a", "--stage", "2", "--yes"], ctx_for(lab, plays)) == 3
    assert plays.calls == []


def test_stage2_refuses_when_config_changed_since_stage1(lab, staged):
    stage1(lab)
    (lab[2] / "sw-a" / "config" / "running-config.txt").write_text("hostname sw-a\nvlan 99\n")
    plays = Plays(lab[1], critical=True)
    assert main(["push", "eindhoven", "sw-a", "--stage", "2", "--yes"], ctx_for(lab, plays)) == 3
    assert plays.calls == []


def test_stage2_ignores_authorized_keys_rewrite(lab, staged):
    stage1(lab)
    (lab[2] / "bang-gouda" / "config" / "authorized_keys").write_text("ssh-ed25519 AAAA new\n")
    plays = Plays(lab[1], critical=True)
    assert main(["push", "eindhoven", "sw-a", "--stage", "2", "--yes"], ctx_for(lab, plays)) == 0


def test_stage2_refuses_other_device_list(lab, staged):
    stage1(lab)
    plays = Plays(lab[1], critical=True)
    assert main(["push", "eindhoven", "sw-a", "sw-b", "--stage", "2", "--yes"], ctx_for(lab, plays)) == 3
    assert plays.calls == []


def test_stage2_applies_via_stage3(lab, staged):
    stage1(lab)
    plays = Plays(lab[1], critical=True)
    assert main(["push", "eindhoven", "sw-a", "--stage", "2", "--yes"], ctx_for(lab, plays)) == 0
    (apply_,) = plays.calls
    assert "--check" not in apply_ and limit_of(apply_) == "sw-a"
    assert state(lab)["done"] == [1, 2]


def test_stage2_proxy_extra_var_is_one_json_value(lab, staged):
    stage1(lab)
    plays = Plays(lab[1], critical=True)
    main(["push", "eindhoven", "sw-a", "--stage", "2", "--yes"], ctx_for(lab, plays))
    js = [json.loads(e) for e in extras(plays.calls[0]) if e.startswith("{")]
    merged = {k: v for d in js for k, v in d.items()}
    assert merged["ansible_ssh_extra_args"].startswith("-o ProxyCommand='ssh -W %h:%p")
    assert merged["ansible_ssh_extra_args"].endswith("flax@bang-edam'")
    assert merged["ansible_paramiko_proxy_command"].endswith("flax@bang-edam")


def test_stage3_refuses_before_stage2(lab, staged):
    stage1(lab)
    plays = Plays(lab[1], critical=True)
    assert main(["push", "eindhoven", "sw-a", "--stage", "3", "--yes"], ctx_for(lab, plays)) == 3


def test_stage3_applies_stage3_jh_and_clears_state(lab, staged):
    stage1(lab)
    ctx = ctx_for(lab, Plays(lab[1], critical=True))
    assert main(["push", "eindhoven", "sw-a", "--stage", "2", "--yes"], ctx) == 0
    plays = Plays(lab[1], critical=True)
    assert main(["push", "eindhoven", "sw-a", "--stage", "3", "--yes"], ctx_for(lab, plays)) == 0
    (call,) = plays.calls
    assert call[-1].endswith("apply_jumphost_config.yml") and limit_of(call) == "bang-edam"
    assert state(lab) is None


def test_single_jumphost_skips_stage3(lab, staged, capsys):
    (lab[2] / "topology.yml").write_text("primary_turtle: turtle-gouda\njumphost_pairs:\n  stage1: bang-gouda\n")
    stage1(lab)
    plays = Plays(lab[1], critical=True)
    assert main(["push", "eindhoven", "sw-a", "--stage", "2", "--yes"], ctx_for(lab, plays)) == 0
    assert "stage 3 skipped (single jump host)" in capsys.readouterr().out
    assert "flax@bang-gouda'" in [e for e in extras(plays.calls[0]) if e.startswith("{")][0]
    assert state(lab) is None


def test_jumphost_push_needs_yes(lab):
    plays = Plays(lab[1])
    assert main(["push", "eindhoven", "bang-edam"], ctx_for(lab, plays)) == 0
    assert all("--check" in c for c in plays.calls)
    plays = Plays(lab[1])
    assert main(["push", "eindhoven", "bang-edam", "--yes"], ctx_for(lab, plays)) == 0
    (call,) = plays.calls
    assert call[-1].endswith("apply_jumphost_config.yml") and limit_of(call) == "bang-edam" and "--check" not in call


def test_extra_dict_or_spaced_value_goes_as_json():
    from reapercli.lib.ansible import playbook_argv
    argv = playbook_argv(SITES_EHV(), ROOT, "x.yml", extra={"a": "b", "c": "has space", "d": {"e": 1}})
    es = extras(argv)
    assert "a=b" in es
    assert json.dumps({"c": "has space"}) in es and json.dumps({"d": {"e": 1}}) in es


def SITES_EHV():
    from reapercli.lib.sites import load_sites
    return load_sites(ROOT)["eindhoven"]


# ---- final-review fixes ------------------------------------------------------------------

class Ssh:
    def __init__(self, rc=0):
        self.calls, self.rc = [], rc

    def __call__(self, argv, stdin, timeout):
        from reapercli.lib.remote import Result
        self.calls.append((argv[-2], argv[-1]))
        return Result(self.rc, "", "")


def test_stage2_probe_runs_on_the_stage1_bang_against_the_turtle_port(lab, staged):
    stage1(lab)
    plays, ssh = Plays(lab[1], critical=True), Ssh()
    ctx = ctx_for(lab, plays)
    ctx.runner = ssh
    assert main(["push", "eindhoven", "sw-a", "--stage", "2", "--yes"], ctx) == 0
    assert [c[-1].rsplit("/", 1)[-1] for c in plays.calls] == ["apply_config.yml"]
    ((target, cmd),) = ssh.calls
    assert target == "flax@bang-gouda" and "/dev/tcp/" in cmd and "/22" in cmd


def test_stage2_probe_failure_is_seen(lab, staged, capsys):
    stage1(lab)
    plays = Plays(lab[1], critical=True)
    ctx = ctx_for(lab, plays)
    ctx.runner = Ssh(rc=1)
    assert main(["push", "eindhoven", "sw-a", "--stage", "2", "--yes"], ctx) == 1
    assert "NOT reverted" in capsys.readouterr().out and state(lab) is None


def test_stage_hops_use_the_bang_name_even_on_site(lab, staged):
    stage1(lab)
    plays = Plays(lab[1], critical=True)
    ctx = ctx_for(lab, plays)
    ctx.hostname, ctx.runner = "bang-gouda", Ssh()
    assert main(["push", "eindhoven", "sw-a", "--stage", "2", "--yes"], ctx) == 0
    proxy = [e for e in extras(plays.calls[0]) if e.startswith("{")][0]
    assert "flax@bang-edam'" in proxy and "192.168" not in proxy


def test_stage1_refuses_without_stage1_jumphost(lab, staged):
    (lab[2] / "topology.yml").write_text("primary_turtle: turtle-gouda\n")
    plays = Plays(lab[1], critical=True)
    assert main(["push", "eindhoven", "sw-a", "--stage", "1", "--yes"], ctx_for(lab, plays)) == 3
    assert not [c for c in plays.calls if "--check" not in c] and state(lab) is None


def test_multi_site_push_keeps_going_after_a_refused_site(lab, capsys):
    plays = Plays(lab[1], critical=True)
    rc = main(["push", "all", "--yes"], ctx_for(lab, plays))
    out = capsys.readouterr()
    sites = {c[c.index("-e") + 1] for c in plays.calls}
    assert {"site_name=eindhoven", "site_name=lavergne", "site_name=braintree"} <= sites
    assert rc == 3 and "refused" in (out.out + out.err)


# ---- Phase 3 deferred minors ----------------------------------------------------------

def test_stage2_refuses_when_topology_changed_since_stage1(lab, staged):
    stage1(lab)
    (lab[2] / "topology.yml").write_text(
        "primary_turtle: rabbit-gouda\njumphost_pairs:\n  stage1: bang-gouda\n  stage3: bang-edam\n")
    plays = Plays(lab[1], critical=True)
    assert main(["push", "eindhoven", "sw-a", "--stage", "2", "--yes"], ctx_for(lab, plays)) == 3
    assert plays.calls == []


def test_stage2_refuses_when_services_changed_since_stage1(lab, staged):
    (lab[2] / "services").mkdir()
    (lab[2] / "services" / "steer.json").write_text("{}")
    stage1(lab)
    (lab[2] / "services" / "steer.json").write_text('{"et9b1": 25}')
    plays = Plays(lab[1], critical=True)
    assert main(["push", "eindhoven", "sw-a", "--stage", "2", "--yes"], ctx_for(lab, plays)) == 3


def test_stage1_without_topology_refuses_cleanly(lab, staged):
    (lab[2] / "topology.yml").unlink()
    plays = Plays(lab[1], critical=True)
    assert main(["push", "eindhoven", "sw-a", "--stage", "1", "--yes"], ctx_for(lab, plays)) == 3
    assert plays.calls == []                # refused before the check run


def test_stage2_without_primary_turtle_refuses_before_applying(lab, staged):
    stage1(lab)
    p = lab[2] / "topology.yml"
    import reapercli.lib.staged_push as sp
    plan = sp.Plan.load(sp.state_path(lab[0], "eindhoven"))
    p.write_text("jumphost_pairs:\n  stage1: bang-gouda\n  stage3: bang-edam\n")
    plan.tree = sp.tree_hash(lab[1], "eindhoven")         # same tree as recorded, only the turtle gone
    plan.save(sp.state_path(lab[0], "eindhoven"))
    plays = Plays(lab[1], critical=True)
    assert main(["push", "eindhoven", "sw-a", "--stage", "2", "--yes"], ctx_for(lab, plays)) == 3
    assert plays.calls == []


def test_stage2_default_target_is_every_non_jumphost(lab, staged):
    plays = Plays(lab[1], critical=True)
    assert main(["push", "eindhoven", "--stage", "1", "--yes"], ctx_for(lab, plays)) == 0
    plays = Plays(lab[1], critical=True)
    assert main(["push", "eindhoven", "--stage", "2", "--yes"], ctx_for(lab, plays)) == 0
    assert limit_of(plays.calls[0]) == "all:!jumphosts:!localhost"


def test_quoted_value_goes_as_json():
    from reapercli.lib.ansible import playbook_argv
    es = extras(playbook_argv(SITES_EHV(), ROOT, "x.yml", extra={"q": "a'b", "p": "/x/y-z.yml", "n": 3}))
    assert json.dumps({"q": "a'b"}) in es and "p=/x/y-z.yml" in es and "n=3" in es
