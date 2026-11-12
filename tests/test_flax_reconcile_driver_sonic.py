# tests/test_flax_reconcile_driver_sonic.py
"""flax_reconcile.drivers.Sonic -- the SONiC write driver (capability only: no
SONiC switch is in any reconcile switches.json; customer rack switches are never
a managed set). Runners are injected; nothing talks to a switch. Fake values."""
import json
import subprocess

import pytest

from flax_reconcile import drivers
from flax_reconcile.drivers import (
    Sonic, SwitchUnreachable, _make_sonic_runner, load_switches,
)

VLAN_BRIEF = """\
+-----------+--------------+------------+----------------+
|   VLAN ID | IP Address   | Ports      | Port Tagging   |
+===========+==============+============+================+
|        17 |              | Ethernet4  | untagged       |
|           |              | Ethernet8  | tagged         |
+-----------+--------------+------------+----------------+
|        25 |              | Ethernet8  | untagged       |
+-----------+--------------+------------+----------------+
"""


class FakeSwitch:
    def __init__(self, vlan_brief=VLAN_BRIEF):
        self.cmds = []
        self.vlan_brief = vlan_brief

    def __call__(self, cmd):
        self.cmds.append(cmd)
        return self.vlan_brief if cmd.startswith("show vlan brief") else ""


def test_set_access_vlan_moves_untagged_and_saves():
    """SONiC refuses a second untagged membership, so the old untagged one is
    dropped right before the add, and a refused add re-adds it."""
    sw = FakeSwitch()
    Sonic("fake-sw", runner=sw).set_access_vlan("Ethernet4", 25)
    assert sw.cmds == [
        "show vlan brief </dev/null",
        "sudo -n config vlan member del 17 Ethernet4 </dev/null && "
        "{ sudo -n config vlan member add -u 25 Ethernet4 </dev/null || "
        "{ sudo -n config vlan member add -u 17 Ethernet4 </dev/null; false; }; } && "
        "sudo -n config save -y </dev/null",
    ]


def test_set_access_vlan_strips_trunk_membership_keeps_wanted_untagged():
    sw = FakeSwitch()
    Sonic("fake-sw", runner=sw).set_access_vlan("Ethernet8", 25)
    assert sw.cmds[1] == ("sudo -n config vlan member del 17 Ethernet8 </dev/null && "
                          "sudo -n config save -y </dev/null")


def test_set_access_vlan_adds_before_removing_tagged_memberships():
    brief = VLAN_BRIEF.replace("|        17 |              | Ethernet4  | untagged       |",
                               "|        17 |              | Ethernet4  | tagged         |")
    sw = FakeSwitch(brief)
    Sonic("fake-sw", runner=sw).set_access_vlan("Ethernet4", 30)
    assert sw.cmds[1] == ("sudo -n config vlan member add -u 30 Ethernet4 </dev/null && "
                          "sudo -n config vlan member del 17 Ethernet4 </dev/null && "
                          "sudo -n config save -y </dev/null")


def _run_chain(chain, fail_add_vid=None):
    """Run a set_access_vlan chain in a local bash against a fake `sudo` that
    keeps the port's memberships in a file. Returns (exit, memberships)."""
    import os
    import tempfile
    d = tempfile.mkdtemp()
    state = os.path.join(d, "state")
    with open(state, "w") as f:
        f.write("17 u\n")
    fake = os.path.join(d, "sudo")
    with open(fake, "w") as f:
        f.write("""#!/bin/bash
shift  # -n
[ "$1 $2" = "config save" ] && { echo saved >> %(d)s/saved; exit 0; }
op=$4; shift 4
if [ "$1" = "-u" ]; then t=u; shift; else t=t; fi
v=$1
if [ "$op" = add ]; then
  [ "$v" = "%(fail)s" ] && exit 1
  [ $t = u ] && grep -q " u$" %(s)s && exit 1   # SONiC: one untagged only
  echo "$v $t" >> %(s)s
else
  grep -v "^$v " %(s)s > %(s)s.n; mv %(s)s.n %(s)s
fi
""" % {"d": d, "s": state, "fail": fail_add_vid or "none"})
    os.chmod(fake, 0o755)
    rc = subprocess.call(["bash", "-c", chain.replace("</dev/null", "")],
                         env=dict(os.environ, PATH=d + ":" + os.environ["PATH"]))
    with open(state) as f:
        members = sorted(f.read().split("\n"))
    return rc, [m for m in members if m], os.path.exists(os.path.join(d, "saved"))


def test_set_access_vlan_chain_moves_the_port():
    sw = FakeSwitch()
    Sonic("fake-sw", runner=sw).set_access_vlan("Ethernet4", 25)
    assert _run_chain(sw.cmds[1]) == (0, ["25 u"], True)


def test_set_access_vlan_failed_add_leaves_port_as_it_was_unsaved():
    sw = FakeSwitch()
    Sonic("fake-sw", runner=sw).set_access_vlan("Ethernet4", 25)
    rc, members, saved = _run_chain(sw.cmds[1], fail_add_vid=25)
    assert rc != 0 and members == ["17 u"] and not saved


def test_set_access_vlan_already_access_is_noop():
    sw = FakeSwitch()
    Sonic("fake-sw", runner=sw).set_access_vlan("Ethernet4", 17)
    assert sw.cmds == ["show vlan brief </dev/null"]


def test_set_access_vlan_unmembered_port_adds():
    sw = FakeSwitch()
    Sonic("fake-sw", runner=sw).set_access_vlan("Ethernet12", "17")
    assert sw.cmds[1] == ("sudo -n config vlan member add -u 17 Ethernet12 </dev/null && "
                          "sudo -n config save -y </dev/null")


def test_flap_shutdown_sleep_startup_one_command_not_saved():
    sw = FakeSwitch()
    Sonic("fake-sw", runner=sw).flap("Ethernet4", hold_seconds=3)
    assert sw.cmds == [
        "sudo -n config interface shutdown Ethernet4 </dev/null && sleep 3 && "
        "sudo -n config interface startup Ethernet4 </dev/null"]


def test_set_admin_up_startup_only():
    sw = FakeSwitch()
    Sonic("fake-sw", runner=sw).set_admin_up("Ethernet4")
    assert sw.cmds == ["sudo -n config interface startup Ethernet4 </dev/null"]


def test_non_ethernet_port_rejected():
    with pytest.raises(ValueError):
        Sonic("fake-sw", runner=FakeSwitch()).flap("Ethernet4; reboot")


def test_runner_password_in_env_not_argv_empty_stdin(monkeypatch):
    seen = {}

    def fake(argv, **kw):
        seen.update(argv=argv, **kw)
        return ""

    monkeypatch.setattr(drivers.subprocess, "check_output", fake)
    _make_sonic_runner("fake-sw", "admin", "fake-pw")("show vlan brief </dev/null")
    assert seen["argv"][:3] == ["sshpass", "-e", "ssh"]
    assert "fake-pw" not in seen["argv"]
    assert seen["env"]["SSHPASS"] == "fake-pw" and seen["input"] == ""
    assert "admin@fake-sw" in seen["argv"]


def test_runner_failure_is_unreachable_without_password(monkeypatch):
    def boom(argv, **kw):
        raise subprocess.CalledProcessError(255, argv)

    monkeypatch.setattr(drivers.subprocess, "check_output", boom)
    with pytest.raises(SwitchUnreachable) as exc:
        _make_sonic_runner("fake-sw", "admin", "fake-pw")("sudo -n config save -y")
    assert "fake-pw" not in str(exc.value)


def _load(tmp_path, entry):
    (tmp_path / "s.json").write_text(json.dumps([entry]))
    (tmp_path / "c.json").write_text(json.dumps(
        {"sonicuser": "admin", "sonicpass": "fake-pw"}))
    (tmp_path / "h.json").write_text("[]")
    switches, _u, _p, _h = load_switches(
        str(tmp_path / "s.json"), str(tmp_path / "c.json"), str(tmp_path / "h.json"))
    return switches


def test_load_switches_builds_sonic_only_on_explicit_opt_in(tmp_path):
    switches = _load(tmp_path, {"name": "fake-sw", "driver": "sonic",
                                "host": "fake-sw", "reconcile": True})
    assert isinstance(switches["fake-sw"], Sonic)


@pytest.mark.parametrize("extra", [{}, {"reconcile": False}, {"reconcile": "yes"}])
def test_load_switches_skips_read_only_sonic_entry(tmp_path, extra):
    """switches.json is shared with switch-sense: a SONiC entry added for READ
    must not make reconcile a writer for it."""
    entry = dict({"name": "fake-sw", "driver": "sonic", "host": "fake-sw"}, **extra)
    assert _load(tmp_path, entry) == {}
