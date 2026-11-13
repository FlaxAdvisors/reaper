"""reaper svc restart (spec §6, §8.4): the right restart for each kind, never postgres."""
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli.__main__ import Context, main  # noqa: E402
from reapercli.commands.svc_restart import plan_restart  # noqa: E402
from reapercli.lib.components import load_components  # noqa: E402
from reapercli.lib.guards import Refused  # noqa: E402
from reapercli.lib.inventory import site_hosts  # noqa: E402
from reapercli.lib.remote import Result  # noqa: E402
from reapercli.lib.sites import load_sites  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SITES = load_sites(ROOT)
COMPS = load_components(ROOT, SITES)
EHV = SITES["eindhoven"]
SH = site_hosts(EHV, ROOT, "orbit")


def plan(name, holder="bang-gouda", host=None):
    return plan_restart(name, COMPS, SH, EHV, ROOT, holder, host)[1]


def test_flax_post_restarts_units_on_holder_only():
    cmds = plan("flax_post")
    assert len(cmds) == 8  # + flax-post-fio-guide: a restart re-renders the guide now
    assert {b for b, _ in cmds} == {"bang-gouda"}
    assert all("sudo -n systemctl restart" in c for _, c in cmds)


def test_console_recreate_uses_env_file():
    cmds = plan("flax_console")
    assert sorted(b for b, _ in cmds) == ["bang-edam", "bang-gouda"]
    assert all("--env-file flax-stack.env" in c and "--force-recreate flax-control" in c for _, c in cmds)
    # /etc/flax/flax-stack is root 0750 and flax-stack.env root 0600 (bang-gouda 2026-11-08)
    assert all(c.startswith("sudo -n bash -c ") for _, c in cmds)


def test_postgres_names_refused():
    for name in ("postgres", "flax-stack-postgres-1", "flax-stack"):
        with pytest.raises(Refused):
            plan(name)
    cmds = plan("flax_stack")
    assert cmds and all("nginx" in c and "postgres" not in c for _, c in cmds)


def test_triage_bmc_fw_drains_only_itself_first():
    [(b1, drain), (b2, recreate)] = plan("prod-bmc_fw-1")
    assert b1 == b2 == "bang-gouda"
    assert drain == "docker exec prod-api-1 python fwdrain.py on bmc_fw"   # never bare: that drains all agents
    assert recreate.startswith("sudo -n bash -c") and "--force-recreate bmc_fw" in recreate


@pytest.mark.parametrize("name", ["docker", "keepalived", "firewalld", "triage_firewall"])
def test_dangerous_units_refused(name):
    with pytest.raises(Refused):
        plan(name)


def test_ha_sensitive_without_single_holder_refused():
    with pytest.raises(Refused, match="VIP"):
        plan("flax_console", holder=None)


def test_kea_host_standby_refused():
    with pytest.raises(Refused, match="VIP holder"):
        plan("kea", host="bang-edam")


class Fake:
    def __init__(self, holder="bang-gouda", fail=None):
        self.log, self.holder, self.fail, self.timeouts = [], holder, fail, {}

    def ssh(self, argv, stdin, timeout):
        host, cmd = argv[-2].replace("flax@bang-", "bang-"), argv[-1]
        if cmd.startswith("ip -o -4 addr"):
            self.log.append("vip-probe")
            return Result(0, "inet 192.168.88.10/24" if host == self.holder else "", "")
        if "keepalived" in cmd:
            self.log.append(f"{'stop' if ' stop ' in cmd else 'start'} {host}")
        else:
            self.log.append(f"{host}: {cmd}")
            self.timeouts[cmd] = timeout
            if self.fail and self.fail in cmd:
                return Result(1, "", "drain timed out")
        return Result(0, "", "")

    def play(self, argv, env):
        self.log.append(argv[-1].rsplit("/", 1)[-1])
        return 0

    def ctx(self, tmp_path):
        (tmp_path / ".vault_pass_eindhoven").write_text("x\n")
        return Context(root=ROOT, home=tmp_path, hostname="orbit", runner=self.ssh, play_runner=self.play)


def test_plan_without_yes_runs_nothing(tmp_path):
    f = Fake()
    assert main(["svc", "restart", "eindhoven", "flax_post"], f.ctx(tmp_path)) == 0
    assert set(f.log) == {"vip-probe"}


def test_ha_sensitive_on_holder_uses_guard(tmp_path):
    f = Fake()
    assert main(["svc", "restart", "eindhoven", "flax_console", "--yes"], f.ctx(tmp_path)) == 0
    log = [x for x in f.log if x != "vip-probe"]
    gouda = next(i for i, x in enumerate(log) if x.startswith("bang-gouda: "))
    assert log[:2] == ["pg_ha_check.yml", "stop bang-edam"]
    assert log.index("stop bang-edam") < gouda < log.index("start bang-edam")
    assert log.index("start bang-edam") < len(log) - 1 - log[::-1].index("pg_ha_check.yml")
    assert any(x.startswith("bang-edam: ") and "flax-control" in x for x in log)


def test_unknown_name_exits_2(tmp_path):
    assert main(["svc", "restart", "eindhoven", "nope"], Fake().ctx(tmp_path)) == 2


def test_drain_gets_long_timeout_and_gates_recreate(tmp_path):
    f = Fake()
    assert main(["svc", "restart", "eindhoven", "prod-bios_fw-1", "--yes"], f.ctx(tmp_path)) == 0
    drain = next(c for c in f.timeouts if "fwdrain" in c)
    assert f.timeouts[drain] >= 1800
    f = Fake(fail="fwdrain")
    assert main(["svc", "restart", "eindhoven", "prod-bios_fw-1", "--yes"], f.ctx(tmp_path)) == 1
    assert not [x for x in f.log if "--force-recreate" in x]


@pytest.mark.parametrize("fail", ["fwdrain.py on", "--force-recreate"])
def test_failed_drain_or_recreate_releases_the_agent(tmp_path, fail):
    """A timed-out `on` leaves the drain set, and a recreate that never happened never
    clears it: either way the agent would sit drained for ever."""
    f = Fake(fail=fail)
    assert main(["svc", "restart", "eindhoven", "prod-bios_fw-1", "--yes"], f.ctx(tmp_path)) == 1
    assert f.log[-1] == "bang-gouda: docker exec prod-api-1 python fwdrain.py off bios_fw"


def test_successful_recreate_needs_no_release(tmp_path):
    f = Fake()
    assert main(["svc", "restart", "eindhoven", "prod-bios_fw-1", "--yes"], f.ctx(tmp_path)) == 0
    assert "--force-recreate bios_fw" in f.log[-1]         # the new container clears its own drain
