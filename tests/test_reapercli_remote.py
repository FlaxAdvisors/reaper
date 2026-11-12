"""Fan-out isolates a dead bang; dormant bangs are never contacted (Review Focus 1)."""
import json
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli.lib.components import load_components  # noqa: E402
from reapercli.lib.inventory import site_hosts  # noqa: E402
from reapercli.lib.probe import probe_site  # noqa: E402
from reapercli.lib.remote import Result, fanout, ssh_argv  # noqa: E402
from reapercli.lib.sites import load_sites  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SITES = load_sites(ROOT)
COMPS = load_components(ROOT, SITES)


def test_ssh_argv_is_batch_and_bounded():
    argv = ssh_argv("bang-gouda", "python3.12 -")
    assert argv[0] == "ssh" and "BatchMode=yes" in argv and "ConnectTimeout=6" in argv
    assert argv[-2:] == ["bang-gouda", "python3.12 -"]


def test_fanout_timeout_isolated():
    def runner(argv, stdin, timeout):
        return Result(124, "", "timeout") if "dead" in argv else Result(0, "{}", "")
    out = fanout({"a": "alive", "b": "dead"}, "true", None, 5, runner)
    assert out["a"].rc == 0 and out["b"].rc == 124


def test_probe_site_skips_dormant_and_reports_dead():
    seen = []

    def runner(argv, stdin, timeout):
        seen.append(argv)
        if "flax@bang-fiesta" in argv:
            return Result(0, json.dumps({"host": {}, "vip": True}), "")
        return Result(255, "", "ssh: connect to host bang-siesta: No route to host")
    sh = site_hosts(SITES["braintree"], ROOT, "orbit")
    facts = probe_site(sh, COMPS, runner=runner)
    assert facts["bang-siesta"] == {"dormant": True}
    assert all("bang-siesta" not in a for a in seen)
    assert facts["bang-fiesta"]["vip"] is True


def test_probe_site_unreachable_and_bad_json():
    def runner(argv, stdin, timeout):
        return Result(255, "", "No route") if "flax@bang-edam" in argv else Result(0, "not json", "")
    facts = probe_site(site_hosts(SITES["eindhoven"], ROOT, "orbit"), COMPS, runner=runner)
    assert facts["bang-edam"] == {"error": "No route", "unreachable": True}
    assert facts["bang-gouda"]["error"] == "bad json" and "unreachable" not in facts["bang-gouda"]


def test_probe_site_quiet_ssh_failure_says_how_to_see_why():
    # fiesta 2026-11-10: LogLevel QUIET in ~/.ssh/config left rc 255 with an empty stderr
    def runner(argv, stdin, timeout):
        return Result(255, "", "") if "flax@bang-edam" in argv else Result(124, "", "timeout")
    facts = probe_site(site_hosts(SITES["eindhoven"], ROOT, "orbit"), COMPS, runner=runner)
    assert facts["bang-edam"]["unreachable"] and "ssh -v flax@bang-edam" in facts["bang-edam"]["error"]
    assert facts["bang-gouda"] == {"error": "ssh timeout", "unreachable": True}


def test_probe_that_ran_and_crashed_is_a_fail_not_unreachable():
    def runner(argv, stdin, timeout):
        return Result(1, "", "Traceback ...\nNameError: x")
    facts = probe_site(site_hosts(SITES["eindhoven"], ROOT, "orbit"), COMPS, runner=runner)
    assert facts["bang-edam"] == {"error": "probe rc=1: NameError: x"}


def test_probe_spec_carries_registry_and_freshness():
    got = {}

    def runner(argv, stdin, timeout):
        got["stdin"] = stdin
        return Result(0, "{}", "")
    probe_site(site_hosts(SITES["lavergne"], ROOT, "orbit"), COMPS, runner=runner)
    first = got["stdin"].splitlines()[0]
    assert first.startswith("SPEC = ")
    ns = {}
    exec(first, ns)          # the bang runs it as python: JSON's false/true would NameError
    spec = ns["SPEC"]
    assert spec["toolchain"] is False
    assert "flax-post" in spec["units"] and "kea-dhcp4" in spec["containers"]
    assert spec["freshness"]["observe_state"] == "last_polled"
    assert "def collect(" in got["stdin"]
