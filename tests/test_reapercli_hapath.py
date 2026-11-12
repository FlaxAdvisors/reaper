"""The HA path (spec §8.3): check, quiesce the LIVE standby, body, ALWAYS re-arm, check."""
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli.__main__ import Context  # noqa: E402
from reapercli.lib.guards import Refused  # noqa: E402
from reapercli.lib.hapath import ha_guard  # noqa: E402
from reapercli.lib.inventory import site_hosts  # noqa: E402
from reapercli.lib.remote import Result  # noqa: E402
from reapercli.lib.sites import load_sites  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SITES = load_sites(ROOT)
EHV, BTR = SITES["eindhoven"], SITES["braintree"]


class Rec:
    """One ordered log of ssh calls, playbook runs and body markers."""

    def __init__(self, check_rcs=(0, 0), ssh_rc=None):
        self.log, self.check_rcs, self.ssh_rc = [], list(check_rcs), ssh_rc or {}
        self.raise_on, self.at_start = None, None

    def ssh(self, argv, stdin, timeout):
        host, cmd = argv[-2].replace("flax@bang-", "bang-"), argv[-1]
        if " start keepalived" in cmd:
            import signal
            self.at_start = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
        if self.raise_on and self.raise_on in cmd:
            self.log.append(f"raise {host}")
            raise KeyboardInterrupt
        if "keepalived" in cmd:
            self.log.append(f"{'stop' if ' stop ' in cmd else 'start'} {host}")
        else:
            self.log.append(cmd)
        for key, rc in self.ssh_rc.items():
            if key in cmd:
                return Result(rc, "", "boom")
        return Result(0, "", "")

    def play(self, argv, env):
        name = argv[-1].rsplit("/", 1)[-1]
        self.log.append(name)
        return self.check_rcs.pop(0) if name == "pg_ha_check.yml" else 0

    def ctx(self):
        return Context(root=ROOT, home=ROOT, hostname="orbit", runner=self.ssh, play_runner=self.play)


def sh(site):
    return site_hosts(site, ROOT, "orbit")


def guard(r, site=EHV, holder="bang-gouda", lines=None):
    return ha_guard(site, sh(site), holder, r.ctx(), out=(lines.append if lines is not None else lambda *a: None))


def test_order_check_stop_body_start_check():
    r = Rec()
    with guard(r) as g:
        r.log.append("body")
    assert r.log == ["pg_ha_check.yml", "stop bang-edam", "body", "start bang-edam", "pg_ha_check.yml"]
    assert g.ok is True


def test_rearm_after_body_raises():
    r = Rec()
    with pytest.raises(RuntimeError):
        with guard(r):
            raise RuntimeError("deploy blew up")
    assert r.log.index("start bang-edam") > r.log.index("stop bang-edam")


def test_rearm_after_keyboardinterrupt():
    r = Rec()
    with pytest.raises(KeyboardInterrupt):
        with guard(r):
            raise KeyboardInterrupt
    assert "start bang-edam" in r.log


def test_vip_on_declared_standby_stops_gouda():
    r = Rec()
    with guard(r, holder="bang-edam"):
        pass
    assert "stop bang-gouda" in r.log and "start bang-gouda" in r.log
    assert not [x for x in r.log if x.endswith("bang-edam")]


def test_no_holder_refuses_before_touching():
    r = Rec()
    with pytest.raises(Refused, match="VIP"):
        with guard(r, holder=None):
            pass
    assert not [x for x in r.log if x.startswith(("stop", "start"))]


def test_unhealthy_precheck_refuses():
    r = Rec(check_rcs=(2,))
    with pytest.raises(Refused, match="ha check"):
        with guard(r):
            pass
    assert r.log == ["pg_ha_check.yml"]


def test_failed_rearm_prints_manual_command():
    r, lines = Rec(ssh_rc={"start keepalived": 1}), []
    with guard(r, lines=lines) as g:
        pass
    assert g.ok is False
    assert any("systemctl start keepalived" in str(x) for x in lines)


def test_failed_postcheck_not_ok():
    r = Rec(check_rcs=(0, 2))
    with guard(r) as g:
        pass
    assert g.ok is False


def test_dormant_standby_skips():
    r, lines = Rec(), []
    with guard(r, site=BTR, holder="bang-fiesta", lines=lines) as g:
        r.log.append("body")
    assert r.log == ["body"]
    assert g.ok is True
    assert any("standby dormant, no HA guard" in str(x) for x in lines)


def test_interrupt_during_stop_still_rearms():
    r = Rec()
    r.raise_on = " stop keepalived"
    with pytest.raises(KeyboardInterrupt):
        with guard(r):
            r.log.append("body")
    assert "body" not in r.log and "start bang-edam" in r.log


def test_hup_and_term_mapped_during_body_and_restored():
    import signal
    before = (signal.getsignal(signal.SIGHUP), signal.getsignal(signal.SIGTERM))
    r, seen = Rec(), {}
    with guard(r):
        seen["hup"], seen["term"] = signal.getsignal(signal.SIGHUP), signal.getsignal(signal.SIGTERM)
    assert seen["hup"] not in (signal.SIG_DFL, before[0]) and seen["term"] not in (signal.SIG_DFL, before[1])
    assert (signal.getsignal(signal.SIGHUP), signal.getsignal(signal.SIGTERM)) == before


def test_rearm_is_shielded_from_signals():
    import signal
    r = Rec()
    with guard(r):
        pass
    sigint, sigterm = r.at_start
    assert sigint == signal.SIG_IGN and sigterm == signal.SIG_IGN
