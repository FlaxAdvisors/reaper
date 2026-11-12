"""An ssh leg that never completed must not let the IPMI/Redfish fallback
name the vendor (et25b1 2026-10-08: a onetree BMC four minutes into its boot
missed the 3 s ssh login, answered IPMI as root and was shown as ami_legacy
with nothing left to re-check it)."""
import datetime as _dt

from flax_observe import bmc_probe
from flax_observe import bmc_vendor as _bv
from flax_observe import state_machine as sm
from flax_observe.bmc_probe import probe_bmc_kind

_OSREL = "ID=openbmc-phosphor\nFLAX_FBUTILS=no\n"
_CREDS = {"obmcuser": "u", "obmcpass": "p"}
_BMC = [{"bmcuser": "root", "bmcpass": "P"}]


def _ports(ssh=False, ipmi=False, redfish=False):
    return lambda h: {"ssh": ssh, "ipmi": ipmi, "redfish": redfish}


def _ipmi(ip, user, pw, args):
    return "Product Name         : Tioga Pass Single Side\n"


def _iso_ago(secs):
    return (_dt.datetime.utcnow() - _dt.timedelta(seconds=secs)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


# --- the probe ---------------------------------------------------------------

def test_a_failed_ssh_login_is_asked_once_more_and_then_identifies(monkeypatch):
    monkeypatch.setattr(bmc_probe, "_sleep", lambda s: None)
    calls = []

    def ssh(ip, user, pw, cmd):
        calls.append(cmd)
        if "os-release" in cmd and len(calls) == 1:
            raise RuntimeError("ssh: connect timed out")
        return _OSREL if "os-release" in cmd else ""

    out = probe_bmc_kind("1.2.3.4", _CREDS, _BMC, ssh_runner=ssh,
                         ipmi_runner=_ipmi, port_probe=_ports(ssh=True, ipmi=True))
    assert out["kind"] == "openbmc" and out["vendor"] == _bv.PHOSPHOR
    assert not out.get("ssh_inconclusive")


def test_the_default_runner_retry_uses_the_patient_timeouts(monkeypatch):
    monkeypatch.setattr(bmc_probe, "_sleep", lambda s: None)
    seen = []

    def check_output(argv, timeout=None, **kw):
        seen.append(([a for a in argv if a.startswith("ConnectTimeout")], timeout))
        if len(seen) == 1:
            raise RuntimeError("rc 255")
        return _OSREL

    monkeypatch.setattr(bmc_probe.subprocess, "check_output", check_output)
    out = probe_bmc_kind("1.2.3.4", _CREDS, [],
                         port_probe=_ports(ssh=True))
    assert out["vendor"] == _bv.PHOSPHOR
    assert seen[0] == (["ConnectTimeout=3"], bmc_probe.SSH_TIMEOUT_SECS)
    assert seen[1] == (["ConnectTimeout=%d" % bmc_probe.SSH_RETRY_CONNECT_SECS],
                       bmc_probe.SSH_RETRY_TIMEOUT_SECS)


def test_ssh_open_but_never_answering_flags_the_ipmi_fallback(monkeypatch):
    monkeypatch.setattr(bmc_probe, "_sleep", lambda s: None)

    def ssh(ip, user, pw, cmd):
        raise RuntimeError("ssh: connect timed out")

    out = probe_bmc_kind("1.2.3.4", _CREDS, _BMC, ssh_runner=ssh,
                         ipmi_runner=_ipmi, port_probe=_ports(ssh=True, ipmi=True))
    assert out["kind"] == "traditional"
    assert out["ssh_inconclusive"] is True


def test_ssh_open_but_never_answering_flags_the_redfish_fallback(monkeypatch):
    monkeypatch.setattr(bmc_probe, "_sleep", lambda s: None)

    def ssh(ip, user, pw, cmd):
        raise RuntimeError("ssh: connect timed out")

    out = probe_bmc_kind(
        "1.2.3.4", _CREDS, [], ssh_runner=ssh,
        port_probe=_ports(ssh=True, redfish=True),
        redfish_probe=lambda ip, creds: {"is_bmc": True, "product_name": "Ami Redfish",
                                         "redfish_version": "1.17.0"})
    assert out["kind"] == "redfish"
    assert out["ssh_inconclusive"] is True


def test_a_closed_ssh_port_is_a_conclusive_fallback():
    out = probe_bmc_kind("1.2.3.4", _CREDS, _BMC, ipmi_runner=_ipmi,
                         port_probe=_ports(ipmi=True))
    assert out["kind"] == "traditional" and out["vendor"] == _bv.AMI_LEGACY
    assert out["ssh_inconclusive"] is False


def test_an_ssh_that_answers_without_openbmc_is_a_conclusive_fallback():
    out = probe_bmc_kind("1.2.3.4", _CREDS, _BMC,
                         ssh_runner=lambda ip, u, p, cmd: "ID=ami\n",
                         ipmi_runner=_ipmi, port_probe=_ports(ssh=True, ipmi=True))
    assert out["kind"] == "traditional"
    assert out["ssh_inconclusive"] is False


# --- what the state machine caches -------------------------------------------

def _fallback(**over):
    p = {"kind": "traditional", "vendor": _bv.AMI_LEGACY,
         "product_name": "Tioga Pass Single Side",
         "creds_used": ("root", "P"), "ssh_inconclusive": True}
    p.update(over)
    return p


def test_a_conclusive_probe_passes_through_untouched():
    p = _fallback(ssh_inconclusive=False)
    assert sm._settle_inconclusive(p, {"for_mac": "m", "inconclusive": 3}, "m") is p


def test_first_inconclusive_fallback_publishes_unknown_not_ami_legacy():
    out = sm._settle_inconclusive(_fallback(), None, "m")
    assert out["vendor"] == _bv.UNKNOWN
    assert out["kind"] == "traditional"       # kind stays frozen
    assert out["inconclusive"] == 1


def test_the_count_restarts_for_a_different_bmc_mac():
    out = sm._settle_inconclusive(
        _fallback(), {"for_mac": "other", "inconclusive": 4,
                      "vendor": _bv.UNKNOWN}, "m")
    assert out["inconclusive"] == 1 and out["vendor"] == _bv.UNKNOWN


def test_the_fallback_is_accepted_after_enough_agreeing_probes():
    prior = None
    for n in range(1, sm.INCONCLUSIVE_SETTLE_PROBES + 1):
        out = sm._settle_inconclusive(_fallback(), prior, "m")
        assert out["inconclusive"] == n
        want = (_bv.AMI_LEGACY if n >= sm.INCONCLUSIVE_SETTLE_PROBES
                else _bv.UNKNOWN)
        assert out["vendor"] == want, n
        prior = dict(out, for_mac="m")


def test_a_known_openbmc_keeps_its_identity_through_a_missed_login():
    prior = {"for_mac": "m", "kind": "openbmc", "vendor": _bv.PHOSPHOR,
             "product_name": "Tioga Pass Single Side",
             "creds_used": ("u", "p"), "redfish_version": None}
    out = sm._settle_inconclusive(_fallback(), prior, "m")
    assert (out["kind"], out["vendor"], out["creds_used"]) == (
        "openbmc", _bv.PHOSPHOR, ("u", "p"))
    assert out["inconclusive"] == 1


def test_an_inconclusive_cache_is_reasked_fast_then_slow():
    fresh = {"kind": "traditional", "product_name": "x", "inconclusive": 1,
             "taxonomy_version": _bv.TAXONOMY_VERSION,
             "probed_at": _iso_ago(sm.INCONCLUSIVE_RETRY_SECS - 20)}
    assert not sm._reprobe_due(fresh)
    due = dict(fresh, probed_at=_iso_ago(sm.INCONCLUSIVE_RETRY_SECS + 5))
    assert sm._reprobe_due(due)
    settled = dict(due, inconclusive=sm.INCONCLUSIVE_SETTLE_PROBES)
    assert not sm._reprobe_due(settled)
    assert sm._reprobe_due(dict(
        settled, probed_at=_iso_ago(sm.PRODUCT_NAME_RETRY_SECS + 5)))


def test_a_conclusive_traditional_cache_is_never_reasked():
    c = {"kind": "traditional", "product_name": "x", "inconclusive": 0,
         "taxonomy_version": _bv.TAXONOMY_VERSION, "probed_at": _iso_ago(99999)}
    assert not sm._reprobe_due(c)
