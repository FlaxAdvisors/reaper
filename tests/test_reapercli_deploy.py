"""reaper deploy (spec §8): registry tags against both bangs, guards, HA path, records,
and the stale-bind check afterwards."""
import json
import re
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli.__main__ import Context, main  # noqa: E402
from reapercli.commands.deploy import expand, play_groups  # noqa: E402
from reapercli.lib.components import load_components  # noqa: E402
from reapercli.lib.inventory import image_pins  # noqa: E402
from reapercli.lib.remote import Result  # noqa: E402
from reapercli.lib.sites import load_sites  # noqa: E402
from test_reapercli_guards import make_clone  # noqa: E402
from test_reapercli_status import bang_facts  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SITES = load_sites(ROOT)
COMPS = load_components(ROOT, SITES)
VIP_HOLDER = {"eindhoven": "bang-gouda", "lavergne": "bang-rhythm", "braintree": "bang-fiesta"}
SITE_OF = {"bang-gouda": "eindhoven", "bang-edam": "eindhoven", "bang-rhythm": "lavergne",
           "bang-blues": "lavergne", "bang-fiesta": "braintree", "bang-siesta": "braintree"}


def all_images(site):
    pins = image_pins(SITES[site], ROOT)
    return sorted({f"{c.image_repo}:{pins[c.image_var]}" for c in COMPS.values()
                   if c.image_repo and c.image_var in pins})


class Fake:
    """ssh + ansible-playbook in one ordered log. `stale` maps probe number (1-based,
    per bang) -> bind_inodes for that probe."""

    def __init__(self, play_rc=None, missing=None, stale=None, conts=None, fail=None, holder=None):
        self.log, self.plays, self.stdin = [], [], {}
        self.play_rc, self.missing, self.stale = play_rc or {}, missing or {}, stale or {}
        self.probes, self.timeouts = {}, {}
        self.conts, self.fail, self.holder = conts or {}, fail, holder   # host -> containers facts

    def ssh(self, argv, stdin, timeout):
        if any(k in argv[-1] for k in TRIAGE_CMDS):
            return self.triage(argv[-2], argv[-1])
        host, cmd = argv[-2].replace("flax@bang-", "bang-"), argv[-1]
        site = SITE_OF[host]
        holder = self.holder or VIP_HOLDER[site]
        if cmd == "python3.12 -":
            n = self.probes[host] = self.probes.get(host, 0) + 1
            f = bang_facts(host == holder, host != holder)
            f["images"] = [i for i in all_images(site) if i not in self.missing.get(host, ())]
            f["containers"] = self.conts.get(host, {})
            f.update(self.stale.get((host, n), {}))
            return Result(0, json.dumps(f), "")
        if cmd.startswith("ip -o -4 addr"):
            return Result(0, "inet 192.168.88.10/24" if host == holder else "", "")
        if "keepalived" in cmd:
            self.log.append(f"{'stop' if ' stop ' in cmd else 'start'} {host}")
        elif "/etc/flax/deployed" in cmd:
            self.log.append(f"record {host}")
            self.stdin[host] = json.loads(stdin)
        else:
            self.log.append(f"{host}: {cmd}")
            self.timeouts[cmd] = timeout
            if self.fail and self.fail in cmd:
                return Result(1, "", "timed out; the drain stays set")
        return Result(0, "", "")

    # ---- the operator's triage clone on the primary, and prod-* images (lib/triage)
    timg = None          # target -> set of images present; None = nothing anywhere
    tstate = None        # extra key=value lines for the triage guard script
    ship_loads = True
    ps = None            # docker ps output on the VIP holder; None = the right tags

    def triage(self, target, cmd):
        self.timg = self.timg if self.timg is not None else {}
        if "reference=prod-" in cmd:
            return Result(0, self.listing or "", "")
        if "docker rmi" in cmd:
            self.log.append(f"triage rmi {target} " + " ".join(sorted(re.findall(r"prod-\w+:\w+", cmd))))
            return Result(0, "", "")
        if " build" in cmd and self.term_on_build:
            import signal
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)   # what a kill delivers
        if "branch --show-current" in cmd:
            st = {"branch": "main", "dirty": 0, "head": TSHA, "pushed": 1, "behind": 0, "dist": 2, "uisrc": 1,
                  "fetched": 1,
                  **(self.tstate or {})}
            return Result(0, "".join(f"{k}={v}\n" for k, v in st.items()), "")
        if "git show" in cmd:
            name = cmd.rsplit(":", 1)[1].strip("'")
            return Result(0, TFILES[name], "")
        if "docker image inspect" in cmd:
            want = re.findall(r"prod-\w+:\w+", cmd)
            return Result(0, "".join(i + "\n" for i in want if i not in self.timg.get(target, set())), "")
        if "docker save" in cmd:
            self.log.append(f"triage ship {cmd.split('flax@')[1].split()[0]}")
            if self.ship_loads:
                for t in list(self.timg) + [k for k in TSTANDBY]:
                    if t.startswith("flax@"):
                        self.timg.setdefault(t, set()).update(re.findall(r"prod-\w+:\w+", cmd))
            return Result(0, "", "")
        if " build" in cmd:
            self.log.append(f"triage build {target}")
            self.build_cmd = cmd
            tag = re.search(r"TRIAGE_TAG=(\w+)", cmd).group(1)
            self.timg.setdefault(target, set()).update(f"prod-{s}:{tag}" for s in TSVCS)
            if self.flap_on_build:
                self.holder = self.flap_on_build
            return Result(0, "", "")
        if "config --images" in cmd:
            self.log.append(f"triage standby-check {target}")
            tag = self.ref_tag or TSHA[:12]
            return Result(0, "".join(f"prod-{s}:{tag}\n" for s in TSVCS), "")
        if "com.docker.compose.project=prod" in cmd:
            self.log.append(f"triage verify {target}")
            tag = self.ref_tag or TSHA[:12]
            return Result(0, self.ps if self.ps is not None
                          else "".join(f"prod-{s}-1 prod-{s}:{tag} running\n" for s in TSVCS), "")
        raise AssertionError(cmd)

    ref_tag = None
    flap_on_build = None     # the VIP moves to this bang while the images build
    listing = None           # docker images output for the prune
    term_on_build = False    # the operator's deploy is killed (SIGTERM) mid-build

    def play(self, argv, env):
        name = argv[-1].rsplit("/", 1)[-1]
        self.plays.append(argv)
        tags = argv[argv.index("--tags") + 1] if "--tags" in argv else ""
        self.log.append(f"{name} {tags}".strip())
        return self.play_rc.get(name, 0)

    def ctx(self, tmp_path, *sites):
        for s in sites or ("eindhoven",):
            (tmp_path / f".vault_pass_{s}").write_text("x\n")
        return Context(root=ROOT, home=tmp_path, hostname="orbit", runner=self.ssh, play_runner=self.play,
                       user="tester", clone=make_clone(tmp_path))


TRIAGE_CMDS = ("~/git/triage", "docker image inspect", "docker save", "com.docker.compose.project=prod",
               "config --images", "reference=prod-", "docker rmi")
TSHA = "abcdef0123456789abcdef0123456789abcdef01"
TOLD = "1234567890abcdef1234567890abcdef12345678"
TSVCS = ("server", "api", "bmc_fw", "bios_fw", "fru_fw")
TSTANDBY = ("flax@bang-edam", "flax@bang-blues", "flax@bang-siesta")
TFILES = {"docker-compose-prod.yml": "image: prod-api:${TRIAGE_TAG:?x}\n"
          + "".join(f"- {k}=${{{k}?}}\n" for k in ("POWER_INTERLOCK_SKIP_PORTS", "MEZZ_FLASH_PORTS",
                                                  "BMC_FW_DISABLE_PORTS", "BIOS_FW_DISABLE_PORTS")),
          ".env": "FLASK_APP=x\n", ".env_prod": "A=1\n"}


def deploys(f):
    return [a for a in f.plays if "pg_ha_check.yml" not in a[-1]]


def test_expand_flax_core():
    assert [c.name for c in expand(["flax_core"], COMPS)] == [
        "flax_classify", "flax_discover", "flax_observe", "flax_reconcile", "flax_switch_sense"]


def test_play_groups_union_and_own_playbook():
    assert play_groups([COMPS["flax_post"], COMPS["post_tgz"], COMPS["wan"]]) == [
        ("apply_bang_services.yml", ("flax_post", "post_tgz")), ("apply_wan_failover.yml", ())]


def test_keepalived_refused(tmp_path):
    f = Fake()
    assert main(["deploy", "eindhoven", "keepalived", "--yes"], f.ctx(tmp_path)) == 3
    assert f.plays == []


def test_unknown_component_exits_2(tmp_path):
    assert main(["deploy", "eindhoven", "nope"], Fake().ctx(tmp_path)) == 2


def test_plan_only_without_yes(tmp_path, capsys):
    f = Fake()
    assert main(["deploy", "eindhoven", "flax_post"], f.ctx(tmp_path)) == 0
    assert f.plays == []
    out = capsys.readouterr().out
    assert "apply_bang_services.yml" in out and "flax_post" in out


def test_check_runs_the_plays_in_check_mode_and_changes_nothing_else(tmp_path, capsys):
    # operator 2026-11-10: the plan did not say which files a deploy would touch
    f = Fake()
    f.envs = []
    play = f.play
    f.play = lambda argv, env: (f.envs.append(env), play(argv, env))[1]
    assert main(["deploy", "lavergne", "node_config", "--check"], f.ctx(tmp_path, "lavergne")) == 0
    [argv] = f.plays
    assert "--check" in argv and "--diff" in argv and argv[argv.index("--tags") + 1] == "node_config"
    assert f.envs[0]["ANSIBLE_DISPLAY_OK_HOSTS"] == "false"
    assert not [x for x in f.log if x.startswith(("stop ", "start ", "record "))]   # no HA path, no record
    assert "check mode" in capsys.readouterr().out


def test_check_fails_when_a_play_fails(tmp_path):
    f = Fake(play_rc={"apply_bang_services.yml": 2})
    assert main(["deploy", "lavergne", "node_config", "--check"], f.ctx(tmp_path, "lavergne")) == 1


@pytest.mark.parametrize("args", [["node_config", "--check", "--yes"], ["triage", "--check"]])
def test_check_refuses_with_yes_and_for_triage(tmp_path, args):
    f = Fake()
    assert main(["deploy", "lavergne", *args], f.ctx(tmp_path, "lavergne")) == 2 and f.plays == []


def test_unit_deploy_both_bangs_no_limit(tmp_path):
    f = Fake()
    assert main(["deploy", "eindhoven", "flax_post", "--yes"], f.ctx(tmp_path)) == 0
    [argv] = deploys(f)
    assert argv[-1].endswith("apply_bang_services.yml")
    assert argv[argv.index("--tags") + 1] == "flax_post"
    assert "--limit" not in argv


def test_ha_sensitive_wraps_in_guard(tmp_path):
    f = Fake()
    assert main(["deploy", "eindhoven", "flax_console", "--yes"], f.ctx(tmp_path)) == 0
    seq = [x for x in f.log if not x.startswith("record")]
    assert seq == ["pg_ha_check.yml", "stop bang-edam", "apply_bang_services.yml flax_console",
                   "start bang-edam", "pg_ha_check.yml"]


def test_failed_play_still_rearms_and_exits_1(tmp_path):
    f = Fake(play_rc={"apply_bang_services.yml": 2})
    assert main(["deploy", "eindhoven", "flax_console", "--yes"], f.ctx(tmp_path)) == 1
    assert "start bang-edam" in f.log
    assert not [x for x in f.log if x.startswith("record")]


def test_dormant_standby_limit_and_no_guard(tmp_path, capsys):
    f = Fake()
    assert main(["deploy", "braintree", "flax_console", "--yes"], f.ctx(tmp_path, "braintree")) == 0
    [argv] = deploys(f)
    assert argv[argv.index("--limit") + 1] == "all:!bang-siesta"
    assert not [x for x in f.log if x.startswith(("stop", "start"))]
    assert "standby dormant, no HA guard" in capsys.readouterr().out


def test_missing_image_refuses_all_sites_first(tmp_path):
    pin = image_pins(SITES["lavergne"], ROOT)["apply_flax_post_image_tag"]
    f = Fake(missing={"bang-blues": (f"flax-control:{pin}",)})
    assert main(["deploy", "all", "flax_post", "--yes"], f.ctx(tmp_path, "eindhoven", "lavergne")) == 3
    assert f.plays == []


def test_records_written_after_success(tmp_path):
    f = Fake()
    ctx = f.ctx(tmp_path)
    assert main(["deploy", "eindhoven", "flax_post", "--yes"], ctx) == 0
    pin = image_pins(SITES["eindhoven"], ROOT)["apply_flax_post_image_tag"]
    for bang in ("bang-gouda", "bang-edam"):
        [rec] = f.stdin[bang]
        assert rec["component"] == "flax_post" and rec["image"] == f"flax-control:{pin}"
        assert rec["user"] == "tester" and rec["from"] == "orbit" and len(rec["commit"]) == 40


def test_wan_runs_its_own_playbook_untagged(tmp_path):
    f = Fake()
    assert main(["deploy", "eindhoven", "wan", "--yes"], f.ctx(tmp_path)) == 0
    [argv] = deploys(f)
    assert argv[-1].endswith("apply_wan_failover.yml") and "--tags" not in argv


def test_branch_refused_without_flag(tmp_path):
    f = Fake()
    ctx = f.ctx(tmp_path)
    import subprocess
    subprocess.run(["git", "-C", str(ctx.clone), "checkout", "-qb", "feat"], check=True)
    assert main(["deploy", "eindhoven", "flax_post", "--yes"], ctx) == 3
    assert f.plays == []


OBSERVE_STALE = {"bind_inodes": {"flax-observe": {"/etc/flax/vlans.json": [2, 1]}},
                 "containers": {"flax-observe": {"state": "running"}}}
CONSOLE_STALE = {"bind_inodes": {"flax-stack-flax-control-1": {"/etc/flax/bmcfw.json": [2, 1]}},
                 "containers": {"flax-stack-flax-control-1": {"state": "running"}}}


def test_stale_after_deploy_hints_and_exits_1(tmp_path, capsys):
    f = Fake(stale={("bang-gouda", 2): OBSERVE_STALE})
    assert main(["deploy", "eindhoven", "flax_core", "--yes"], f.ctx(tmp_path)) == 1
    assert "reaper svc restart eindhoven flax-observe --host bang-gouda --yes" in capsys.readouterr().out
    assert not [x for x in f.log if "systemctl restart" in x]


def test_restart_stale_restarts_then_rechecks(tmp_path):
    f = Fake(stale={("bang-gouda", 2): OBSERVE_STALE})
    assert main(["deploy", "eindhoven", "flax_core", "--yes", "--restart-stale"], f.ctx(tmp_path)) == 0
    assert [x for x in f.log if "systemctl restart" in x] == ["bang-gouda: sudo -n systemctl restart flax-observe"]
    assert f.probes["bang-gouda"] == 3


def test_stale_console_takes_ha_path(tmp_path):
    f = Fake(stale={("bang-gouda", 2): CONSOLE_STALE})
    assert main(["deploy", "eindhoven", "flax_post", "--yes", "--restart-stale"], f.ctx(tmp_path)) == 0
    i = next(i for i, x in enumerate(f.log) if "--force-recreate flax-control" in x)
    assert f.log.index("stop bang-edam") < i < f.log.index("start bang-edam")


# ---- the fw agents drain before a triage deploy recreates them (fw-agents-drain-before-deploy)
AGENTS_UP = {"bang-gouda": {"prod-api-1": {"state": "running"}, "prod-bmc_fw-1": {"state": "running"},
                            "prod-bios_fw-1": {"state": "running"}, "prod-fru_fw-1": {"state": "exited"}}}
DRAIN = "bang-gouda: docker exec prod-api-1 python fwdrain.py on bmc_fw bios_fw"
RELEASE = "bang-gouda: docker exec prod-api-1 python fwdrain.py off bmc_fw bios_fw"
TRIAGE_PLAY = "apply_bang_services.yml triage_compose"


def test_triage_drains_running_agents_then_plays_then_releases(tmp_path):
    f = Fake(conts=AGENTS_UP)
    assert main(["deploy", "eindhoven", "triage", "--yes"], f.ctx(tmp_path)) == 0
    assert f.log.index(DRAIN) < f.log.index(TRIAGE_PLAY) < f.log.index(RELEASE)
    assert f.timeouts[DRAIN.split(": ", 1)[1]] >= 1800      # on waits up to 30 min for a flash
    # a play that does not recreate an agent leaves it drained for ever: always release


def test_drain_failure_skips_play_and_releases(tmp_path):
    f = Fake(conts=AGENTS_UP, fail="fwdrain.py on")
    assert main(["deploy", "eindhoven", "triage", "--yes"], f.ctx(tmp_path)) == 1
    assert TRIAGE_PLAY not in f.log
    assert RELEASE in f.log                 # a timed-out `on` leaves the drain set


def test_play_failure_still_releases(tmp_path):
    f = Fake(conts=AGENTS_UP, play_rc={"apply_bang_services.yml": 2})
    assert main(["deploy", "eindhoven", "triage", "--yes"], f.ctx(tmp_path)) == 1
    assert f.log.index(TRIAGE_PLAY) < f.log.index(RELEASE)


def test_no_agent_running_no_drain(tmp_path):
    f = Fake()
    assert main(["deploy", "eindhoven", "triage", "--yes"], f.ctx(tmp_path)) == 0
    assert not [x for x in f.log if "fwdrain" in x]


def test_plan_names_the_drain_and_runs_nothing(tmp_path, capsys):
    f = Fake(conts=AGENTS_UP)
    assert main(["deploy", "eindhoven", "triage"], f.ctx(tmp_path)) == 0
    assert "drain bang-gouda: bmc_fw bios_fw" in capsys.readouterr().out
    assert not [x for x in f.log if "fwdrain" in x]


def test_non_triage_deploy_never_drains(tmp_path):
    f = Fake(conts=AGENTS_UP)
    assert main(["deploy", "eindhoven", "flax_post", "--yes"], f.ctx(tmp_path)) == 0
    assert not [x for x in f.log if "fwdrain" in x]


def test_triage_refused_when_vip_off_declared_master(tmp_path, capsys):
    """apply_triage_compose brings the stack up on the DECLARED master and downs it
    everywhere else: with the VIP on edam it would kill edam's live agents."""
    f = Fake(conts={"bang-edam": AGENTS_UP["bang-gouda"]}, holder="bang-edam")
    assert main(["deploy", "eindhoven", "triage", "--yes"], f.ctx(tmp_path)) == 3
    assert "bang-edam" in capsys.readouterr().err
    assert TRIAGE_PLAY not in f.log and not [x for x in f.log if "fwdrain" in x]


def test_unreadable_containers_refuse_triage(tmp_path, capsys):
    f = Fake(conts={"bang-gouda": {"error": "docker: timeout"}})
    assert main(["deploy", "eindhoven", "triage", "--yes"], f.ctx(tmp_path)) == 3
    assert "mid-flash" in capsys.readouterr().err
    assert TRIAGE_PLAY not in f.log


def test_ssh_keys_runs_jumphost_playbook_tag_only(tmp_path):
    """Keys without the network: apply_jumphost_config.yml --tags ssh_keys, never untagged."""
    f = Fake()
    assert main(["deploy", "eindhoven", "ssh_keys", "--yes"], f.ctx(tmp_path)) == 0
    [argv] = deploys(f)
    assert argv[-1].endswith("apply_jumphost_config.yml")
    assert argv[argv.index("--tags") + 1] == "ssh_keys"


# ---- Phase 5: triage builds from the operator's clone, ships, stages, verifies, records
def _src(argv):
    return next(a.split("=", 1)[1] for a in argv if a.startswith("apply_triage_compose_src="))


def test_triage_plan_names_commit_build_and_ship(tmp_path, capsys):
    f = Fake()
    assert main(["deploy", "eindhoven", "triage"], f.ctx(tmp_path)) == 0
    out = capsys.readouterr().out
    assert TSHA[:12] in out and "build on bang-gouda" in out and "ship bang-edam" in out
    assert f.plays == [] and not [x for x in f.log if x.startswith("triage ")]


def test_triage_deploy_builds_ships_passes_src_and_records_commit(tmp_path):
    f = Fake()
    seen = {}
    real = f.play

    def play(argv, env):
        if "apply_bang_services.yml" in argv[-1]:
            d = Path(_src(argv))
            seen["dir"], seen["env"] = d, (d / ".env").read_text()
            seen["files"] = sorted(p.name for p in d.iterdir())
        return real(argv, env)
    ctx = f.ctx(tmp_path)
    ctx.play_runner = play
    assert main(["deploy", "eindhoven", "triage", "--yes"], ctx) == 0
    assert f.log.index("triage build tester@bang-gouda") < f.log.index("triage ship 192.168.88.22") \
        < f.log.index(TRIAGE_PLAY) < f.log.index("triage verify flax@bang-gouda")
    assert seen["files"] == [".env", ".env_prod", "docker-compose-prod.yml"]
    assert f"TRIAGE_TAG={TSHA[:12]}\n" in seen["env"] and "BMC_FW_DISABLE_PORTS=et5b1," in seen["env"]
    assert "BMC_FW_DISABLE_PORTS=et5b1," in f.build_cmd
    assert not seen["dir"].exists()
    for bang in ("bang-gouda", "bang-edam"):
        [rec] = f.stdin[bang]
        assert rec["triage"] == TSHA and rec["image"] == f"prod-*:{TSHA[:12]}"


def test_triage_refuses_when_standby_still_lacks_images(tmp_path):
    f = Fake()
    f.ship_loads = False
    assert main(["deploy", "eindhoven", "triage", "--yes"], f.ctx(tmp_path)) == 3
    assert TRIAGE_PLAY not in f.log


def test_triage_tempdir_removed_on_failure(tmp_path):
    f = Fake(play_rc={"apply_bang_services.yml": 2})
    assert main(["deploy", "eindhoven", "triage", "--yes"], f.ctx(tmp_path)) == 1
    [argv] = deploys(f)
    assert not Path(_src(argv)).exists()
    assert not [x for x in f.log if x.startswith("record")]


def test_triage_tempdir_removed_on_refusal(tmp_path, monkeypatch):
    made = []
    import tempfile
    real = tempfile.mkdtemp
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **kw: made.append(real(**kw)) or made[-1])
    f = Fake()
    f.ship_loads = False
    assert main(["deploy", "eindhoven", "triage", "--yes"], f.ctx(tmp_path)) == 3
    assert made and not any(Path(d).exists() for d in made)


def test_triage_verify_failure_exits_1(tmp_path, capsys):
    f = Fake()
    f.ps = "prod-api-1 prod-api:latest running\n"
    assert main(["deploy", "eindhoven", "triage", "--yes"], f.ctx(tmp_path)) == 1
    assert "prod-api-1: runs prod-api:latest" in capsys.readouterr().out


def test_triage_dormant_standby_not_shipped(tmp_path, capsys):
    f = Fake()
    assert main(["deploy", "braintree", "triage", "--yes"], f.ctx(tmp_path, "braintree")) == 0
    assert "bang-siesta: dormant, not shipped" in capsys.readouterr().out
    assert not [x for x in f.log if x.startswith("triage ship")]
    assert "BMC_FW_DISABLE_PORTS=''" in f.build_cmd


def test_triage_ref_uses_existing_images_and_records_it(tmp_path):
    f = Fake()
    f.tstate = {"ref": TOLD, "refok": 1}
    f.ref_tag = TOLD[:12]
    f.timg = {t: {f"prod-{s}:{TOLD[:12]}" for s in TSVCS} for t in ("tester@bang-gouda", "flax@bang-edam")}
    assert main(["deploy", "eindhoven", "triage", "--yes", "--triage-ref", TOLD[:7]], f.ctx(tmp_path)) == 0
    assert not [x for x in f.log if x.startswith(("triage build", "triage ship"))]
    assert f.stdin["bang-gouda"][0]["triage"] == TOLD


def test_triage_flags_without_triage_exit_2(tmp_path):
    f = Fake()
    assert main(["deploy", "eindhoven", "flax_post", "--triage-ref", "abc"], f.ctx(tmp_path)) == 2
    assert f.plays == []


def test_triage_vip_moving_during_the_build_refuses_the_play(tmp_path, capsys):
    """The gate and the drain targets are re-read after the (long) build: a flap to edam
    meanwhile must not let the play down the live stack there."""
    f = Fake()
    f.flap_on_build = "bang-edam"
    assert main(["deploy", "eindhoven", "triage", "--yes"], f.ctx(tmp_path)) == 3
    assert TRIAGE_PLAY not in f.log and not [x for x in f.log if "fwdrain" in x]


def test_triage_checks_the_standby_resolves_the_tags(tmp_path):
    f = Fake()
    assert main(["deploy", "eindhoven", "triage", "--yes"], f.ctx(tmp_path)) == 0
    assert f.log.index(TRIAGE_PLAY) < f.log.index("triage standby-check flax@bang-edam")


def test_triage_yes_prints_build_and_ship_progress(tmp_path, capsys):
    f = Fake()
    assert main(["deploy", "eindhoven", "triage", "--yes"], f.ctx(tmp_path)) == 0
    out = capsys.readouterr().out
    assert out.count("build on bang-gouda") == 2 and out.count("ship bang-edam") == 2


# ---- deferred minors (2026-11-09)
def test_all_scope_a_refused_site_does_not_stop_the_later_sites(tmp_path, capsys):
    """eindhoven and lavergne refuse at the ship (their standbys never get the images);
    braintree (dormant standby, nothing shipped) must still deploy."""
    f = Fake()
    f.ship_loads = False
    assert main(["deploy", "all", "triage", "--yes"],
                f.ctx(tmp_path, "eindhoven", "lavergne", "braintree")) == 3
    out = capsys.readouterr().out
    assert "eindhoven: refused:" in out and "lavergne: refused:" in out
    assert "bang-fiesta" in f.stdin


def test_triage_tempdir_removed_when_the_deploy_is_killed(tmp_path, monkeypatch):
    import signal
    import tempfile
    made, real = [], tempfile.mkdtemp
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **kw: made.append(real(**kw)) or made[-1])
    before = signal.getsignal(signal.SIGTERM)
    f = Fake()
    f.term_on_build = True
    assert main(["deploy", "eindhoven", "triage", "--yes"], f.ctx(tmp_path)) == 1   # "interrupted"
    assert made and not any(Path(d).exists() for d in made)
    assert signal.getsignal(signal.SIGTERM) is before


PRUNE_LIST = "".join(f"prod-{s} {t} {d} +0000 UTC\n" for s in TSVCS for t, d in (
    (TSHA[:12], "2026-11-09 13:00:00"), ("704d5bac3ac1", "2026-11-08 10:00:00"),
    ("1111aaaa2222", "2026-11-01 10:00:00"), ("rollback-x", "2026-09-01 10:00:00")))


def test_triage_prunes_old_builds_on_each_live_bang_after_a_good_deploy(tmp_path, capsys):
    f = Fake()
    f.listing = PRUNE_LIST
    assert main(["deploy", "eindhoven", "triage", "--yes"], f.ctx(tmp_path)) == 0
    rmi = [x for x in f.log if x.startswith("triage rmi")]
    want = " ".join(sorted(f"prod-{s}:1111aaaa2222" for s in TSVCS))
    assert rmi == [f"triage rmi flax@bang-gouda {want}", f"triage rmi flax@bang-edam {want}"]
    assert f.log.index("triage verify flax@bang-gouda") < f.log.index(rmi[0])
    assert "pruned bang-gouda: 5 old prod-* images" in capsys.readouterr().out


def test_triage_prune_skips_the_dormant_standby(tmp_path):
    f = Fake()
    f.listing = PRUNE_LIST
    assert main(["deploy", "braintree", "triage", "--yes"], f.ctx(tmp_path, "braintree")) == 0
    assert [x.split()[2] for x in f.log if x.startswith("triage rmi")] == ["flax@bang-fiesta"]


def test_triage_no_prune_when_verify_fails(tmp_path):
    f = Fake()
    f.listing = PRUNE_LIST
    f.ps = "prod-api-1 prod-api:latest running\n"
    assert main(["deploy", "eindhoven", "triage", "--yes"], f.ctx(tmp_path)) == 1
    assert not [x for x in f.log if x.startswith("triage rmi")]
