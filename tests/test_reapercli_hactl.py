"""reaper ha quiesce | arm | keepalived-roll | reseed (Phase 3): the runbook's HA
procedures as guarded commands. The fake bangs keep VIP and keepalived state."""
import json
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli.__main__ import Context, main  # noqa: E402
from reapercli.lib.guards import Refused  # noqa: E402
from reapercli.lib.inventory import site_hosts  # noqa: E402
from reapercli.lib.remote import Result  # noqa: E402
from reapercli.lib.sites import load_sites  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SITES = load_sites(ROOT)
EHV, BTR = SITES["eindhoven"], SITES["braintree"]


class Bangs:
    """Logs keepalived stop/start and plays (`<playbook>@<limit>` when limited);
    answers the VIP probe and `systemctl is-active` from its own state."""

    def __init__(self, holder="bang-gouda", inactive=(), play_rc=None, after_play=None, ssh_rc=None):
        self.log, self.holder, self.argvs = [], holder, []
        self.active = {"bang-gouda": True, "bang-edam": True, "bang-fiesta": True}
        for b in inactive:
            self.active[b] = False
        self.play_rc = play_rc or {}        # "<playbook>[@limit]" -> rc
        self.after_play = after_play or {}  # "<playbook>[@limit]" -> new VIP holder (None = nobody)
        self.ssh_rc = ssh_rc or {}          # "stop bang-edam" -> rc

    def ssh(self, argv, stdin, timeout):
        host, cmd = argv[-2].replace("flax@", ""), argv[-1]
        if cmd.startswith("ip -o -4 addr"):
            return Result(0, "2: br0    inet 192.168.88.10/24 scope global" if host == self.holder else "", "")
        if "is-active keepalived" in cmd:
            on = self.active.get(host, True)
            return Result(0 if on else 3, "active\n" if on else "inactive\n", "")
        if "keepalived" in cmd:
            action = "stop" if " stop " in cmd else "start"
            entry = f"{action} {host}"
            self.log.append(entry)
            rc = self.ssh_rc.get(entry, 0)
            if rc == 0:
                self.active[host] = action == "start"
            return Result(rc, "", "boom" if rc else "")
        self.log.append(cmd)
        return Result(0, "", "")

    def play(self, argv, env):
        self.argvs.append(argv)
        name = argv[-1].rsplit("/", 1)[-1]
        key = f"{name}@{argv[argv.index('--limit') + 1]}" if "--limit" in argv else name
        self.log.append(key)
        if key in self.after_play:
            self.holder = self.after_play[key]
        return self.play_rc.get(key, 0)

    def ctx(self):
        return Context(root=ROOT, home=Path.home(), hostname="orbit", runner=self.ssh, play_runner=self.play,
                       sleep=lambda s: None)


def sh(site=EHV):
    return site_hosts(site, ROOT, "orbit")


def lines():
    out = []
    return out, (lambda *a: out.append(" ".join(str(x) for x in a)))


# ---- quiesce / arm ---------------------------------------------------------------

def test_quiesce_plan_only_without_yes():
    from reapercli.lib.hactl import quiesce
    b, (out, say) = Bangs(), lines()
    assert quiesce(EHV, sh(), b.ctx(), yes=False, out=say) == 0
    assert not [x for x in b.log if x.startswith("stop")]
    assert any("stop keepalived on bang-edam" in x for x in out)


def test_quiesce_stops_the_non_holder():
    from reapercli.lib.hactl import quiesce
    b = Bangs(holder="bang-gouda")
    assert quiesce(EHV, sh(), b.ctx(), yes=True, out=lambda *a: None) == 0
    assert b.log == ["stop bang-edam"]
    b = Bangs(holder="bang-edam")
    assert quiesce(EHV, sh(), b.ctx(), yes=True, out=lambda *a: None) == 0
    assert b.log == ["stop bang-gouda"]


def test_quiesce_refuses_without_single_holder():
    from reapercli.lib.hactl import quiesce
    b = Bangs(holder=None)
    with pytest.raises(Refused, match="VIP"):
        quiesce(EHV, sh(), b.ctx(), yes=True, out=lambda *a: None)
    assert b.log == []


def test_arm_starts_inactive_standby_then_checks():
    from reapercli.lib.hactl import arm
    b = Bangs(inactive=("bang-edam",))
    assert arm(EHV, sh(), b.ctx(), yes=True, out=lambda *a: None) == 0
    assert b.log[-2:] == ["start bang-edam", "pg_ha_check.yml"]


def test_arm_nothing_to_arm():
    from reapercli.lib.hactl import arm
    b, (out, say) = Bangs(), lines()
    assert arm(EHV, sh(), b.ctx(), yes=True, out=say) == 0
    assert not [x for x in b.log if x.startswith("start")]
    assert any("nothing to arm" in x for x in out)


def test_arm_plan_only_without_yes():
    from reapercli.lib.hactl import arm
    b, (out, say) = Bangs(inactive=("bang-edam",)), lines()
    assert arm(EHV, sh(), b.ctx(), yes=False, out=say) == 0
    assert b.log == []
    assert any("start keepalived on bang-edam" in x for x in out)


def test_arm_refuses_failback():
    from reapercli.lib.hactl import arm
    b = Bangs(holder="bang-edam", inactive=("bang-gouda",))
    with pytest.raises(Refused, match="postgres-failback"):
        arm(EHV, sh(), b.ctx(), yes=True, out=lambda *a: None)
    assert not [x for x in b.log if x.startswith("start")]


def test_dormant_standby_refuses_quiesce_and_arm():
    b = Bangs(holder="bang-fiesta")
    for action in ("quiesce", "arm"):
        assert main(["ha", action, "braintree", "--yes"], b.ctx()) == 3
    from reapercli.lib.hactl import refuse_dormant
    with pytest.raises(Refused, match="dormant"):
        refuse_dormant(BTR, sh(BTR))
    assert b.log == []


def test_cli_quiesce_yes_and_usage():
    b = Bangs()
    assert main(["ha", "quiesce", "eindhoven", "--yes"], b.ctx()) == 0
    assert b.log == ["stop bang-edam"]
    assert main(["ha", "nope"], b.ctx()) == 2


# ---- keepalived-roll ---------------------------------------------------------------

def test_roll_order():
    from reapercli.lib.hactl import roll
    b = Bangs()
    assert roll(EHV, sh(), b.ctx(), yes=True, out=lambda *a: None) == 0
    assert b.log == ["pg_ha_check.yml", "stop bang-edam", "apply_bang_services.yml@bang-gouda",
                     "apply_bang_services.yml@bang-edam", "start bang-edam", "pg_ha_check.yml"]
    for argv in b.argvs:
        if argv[-1].endswith("apply_bang_services.yml"):
            assert argv[argv.index("--tags") + 1] == "keepalived"


def test_roll_refuses_when_vip_not_on_declared_master():
    from reapercli.lib.hactl import roll
    b = Bangs(holder="bang-edam")
    with pytest.raises(Refused, match="bang-gouda"):
        roll(EHV, sh(), b.ctx(), yes=True, out=lambda *a: None)
    assert b.log == []


def test_roll_refuses_unhealthy_pair_before_quiesce():
    from reapercli.lib.hactl import roll
    b = Bangs(play_rc={"pg_ha_check.yml": 2})
    with pytest.raises(Refused, match="ha check"):
        roll(EHV, sh(), b.ctx(), yes=True, out=lambda *a: None)
    assert b.log == ["pg_ha_check.yml"]


def test_roll_master_never_takes_vip_leaves_standby_quiesced():
    from reapercli.lib.hactl import roll
    b, (out, say) = Bangs(after_play={"apply_bang_services.yml@bang-gouda": None}), lines()
    assert roll(EHV, sh(), b.ctx(), yes=True, out=say) == 1
    assert "start bang-edam" not in b.log
    assert "apply_bang_services.yml@bang-edam" not in b.log
    assert any("left quiesced" in x for x in out)
    assert any("reaper ha arm eindhoven --yes" in x for x in out)


def test_roll_master_apply_fails_after_vip_back_rearms_standby():
    from reapercli.lib.hactl import roll
    b = Bangs(play_rc={"apply_bang_services.yml@bang-gouda": 2})
    assert roll(EHV, sh(), b.ctx(), yes=True, out=lambda *a: None) == 1
    assert "start bang-edam" in b.log
    assert "apply_bang_services.yml@bang-edam" not in b.log


def test_roll_interrupt_without_vip_on_master_does_not_arm():
    from reapercli.lib.hactl import roll
    b = Bangs()

    def play(argv, env):
        if argv[-1].endswith("apply_bang_services.yml"):
            b.holder = None
            raise KeyboardInterrupt
        return Bangs.play(b, argv, env)
    ctx = b.ctx()
    ctx.play_runner = play
    with pytest.raises(KeyboardInterrupt):
        roll(EHV, sh(), ctx, yes=True, out=lambda *a: None)
    assert "start bang-edam" not in b.log


def test_roll_plan_only_without_yes():
    from reapercli.lib.hactl import roll
    b, (out, say) = Bangs(), lines()
    assert roll(EHV, sh(), b.ctx(), yes=False, out=say) == 0
    assert b.log == []
    assert len([x for x in out if x.lstrip().startswith(("1.", "2.", "3.", "4.", "5."))]) == 5


def test_cli_keepalived_roll_dispatch():
    b = Bangs()
    assert main(["ha", "keepalived-roll", "eindhoven"], b.ctx()) == 0
    assert b.log == []


# ---- reseed ------------------------------------------------------------------------

def test_reseed_needs_confirm():
    from reapercli.lib.hactl import reseed
    b = Bangs()
    with pytest.raises(Refused, match="--confirm bang-edam"):
        reseed(EHV, sh(), b.ctx(), confirm=None, yes=True, out=lambda *a: None)
    assert b.log == []


def test_reseed_refuses_the_primary_as_target():
    from reapercli.lib.hactl import reseed
    b = Bangs()
    with pytest.raises(Refused, match="bang-gouda.*bang-edam|bang-edam.*bang-gouda"):
        reseed(EHV, sh(), b.ctx(), confirm="bang-gouda", yes=True, out=lambda *a: None)
    assert b.log == []


def test_reseed_refuses_when_standby_holds_vip():
    from reapercli.lib.hactl import reseed
    b = Bangs(holder="bang-edam")
    with pytest.raises(Refused, match="postgres-failback"):
        reseed(EHV, sh(), b.ctx(), confirm="bang-edam", yes=True, out=lambda *a: None)
    assert b.log == []


def test_reseed_refuses_with_no_vip_holder():
    from reapercli.lib.hactl import reseed
    b = Bangs(holder=None)
    with pytest.raises(Refused):
        reseed(EHV, sh(), b.ctx(), confirm="bang-edam", yes=True, out=lambda *a: None)
    assert b.log == []


def test_reseed_runs_bootstrap_then_check():
    from reapercli.lib.hactl import reseed
    b = Bangs()
    assert reseed(EHV, sh(), b.ctx(), confirm="bang-edam", yes=True, out=lambda *a: None) == 0
    assert b.log == ["pg_standby_bootstrap.yml", "pg_ha_check.yml"]
    argv = b.argvs[0]
    es = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert "target=bang-edam" in es and "pg_bootstrap_force=true" in es


def test_reseed_failed_bootstrap_exits_1_without_check():
    from reapercli.lib.hactl import reseed
    b = Bangs(play_rc={"pg_standby_bootstrap.yml": 2})
    assert reseed(EHV, sh(), b.ctx(), confirm="bang-edam", yes=True, out=lambda *a: None) == 1
    assert b.log == ["pg_standby_bootstrap.yml"]


def test_reseed_plan_only_without_yes():
    from reapercli.lib.hactl import reseed
    b, (out, say) = Bangs(), lines()
    assert reseed(EHV, sh(), b.ctx(), confirm="bang-edam", yes=False, out=say) == 0
    assert b.log == []
    assert any("WIPE postgres on bang-edam" in x for x in out)


def test_reseed_dormant_refuses():
    b = Bangs(holder="bang-fiesta")
    assert main(["ha", "reseed", "braintree", "--confirm", "bang-siesta", "--yes"], b.ctx()) == 3
    assert b.log == []


def test_cli_reseed_confirm_parsed():
    b = Bangs()
    assert main(["ha", "reseed", "eindhoven", "--confirm", "bang-gouda", "--yes"], b.ctx()) == 3
    assert main(["ha", "reseed", "eindhoven", "--confirm", "bang-edam"], b.ctx()) == 0
    assert b.log == []


def test_roll_vip_timeout_says_why():
    from reapercli.lib.hactl import roll
    b, (out, say) = Bangs(after_play={"apply_bang_services.yml@bang-gouda": None}), lines()
    assert roll(EHV, sh(), b.ctx(), yes=True, out=say) == 1
    assert any("did not take the VIP" in x for x in out)


def test_reseed_refuses_a_site_with_no_declared_standby():
    import dataclasses
    from reapercli.lib.hactl import reseed
    b = Bangs()
    with pytest.raises(Refused, match="no declared standby"):
        reseed(EHV, dataclasses.replace(sh(), standby=()), b.ctx(), confirm="bang-edam", yes=True, out=lambda *a: None)
    assert b.log == []
