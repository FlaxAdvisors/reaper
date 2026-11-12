"""Factory-logic tests for flax_reconcile.drivers.load_switches.

These exercise the wiring only -- which driver CLASS is built per switches.json
entry, how the credential JSONs are parsed and returned, and that an
unsupported driver raises clearly. The eAPI/ssh transport is never invoked
against a real switch: we either pass a temp credentials.json on disk or
inspect the returned closures without calling them. No network, no DB.

Cred source: both switch transport creds (eosuser/eospass, cisco_user/cisco_pass)
AND the openbmc ssh creds (obmcuser/obmcpass) come from credentials.json
(switch_creds_path). credentials-bmc.json is the IPMI list used only by the
IPMI/observe path and is NOT consumed by flax-reconcile.
"""
import json

import pytest

from flax_reconcile import drivers
from flax_reconcile.drivers import (
    AristaEAPI,
    IOS,
    Cumulus,
    load_switches,
    _make_eapi_caller,
    _make_ios_runner,
    _make_cumulus_runner,
    _ios_to_iface,
    SwitchUnreachable,
)


def _write(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj))
    return str(p)


def _switch_creds(tmp_path, extra=None):
    """Write a credentials.json with all keys load_switches needs."""
    creds = {
        "eosuser": "admin", "eospass": "test-only", "eoshost": "rabbit-x",
        "cisco_user": "admin", "cisco_pass": "ios-only",
        "cumuser": "cumulus", "cumpass": "CumulusLinux!",
        "obmcuser": "root", "obmcpass": "test-obmc-pw",
    }
    if extra:
        creds.update(extra)
    return _write(tmp_path, "credentials.json", creds)


def test_eos_entry_builds_arista_driver(tmp_path):
    sw_path = _write(tmp_path, "switches.json", [
        {"name": "rabbit-fiesta", "driver": "eos", "host": "rabbit-fiesta",
         "credentials_key": "eos"},
    ])
    creds_path = _switch_creds(tmp_path)
    host_path = _write(tmp_path, "credentials-host.json",
                       [{"user": "flax", "pass": "test-only"}])

    switches, obmc_user, obmc_pass, host_creds = load_switches(
        sw_path, creds_path, host_path)

    assert set(switches) == {"rabbit-fiesta"}
    assert isinstance(switches["rabbit-fiesta"], AristaEAPI)
    assert switches["rabbit-fiesta"].name == "rabbit-fiesta"
    # caller is wired (callable), not the test-injected lambda
    assert callable(switches["rabbit-fiesta"]._call)


def test_ios_entry_builds_ios_driver(tmp_path):
    sw_path = _write(tmp_path, "switches.json", [
        {"name": "turtle-gouda", "driver": "ios", "host": "turtle-gouda",
         "credentials_key": "cisco"},
    ])
    creds_path = _switch_creds(tmp_path)
    host_path = _write(tmp_path, "credentials-host.json", [])

    switches, _u, _p, _h = load_switches(sw_path, creds_path, host_path)

    assert isinstance(switches["turtle-gouda"], IOS)
    assert switches["turtle-gouda"].name == "turtle-gouda"
    # to_iface is the lifted swp<N> -> Gi mapper
    assert switches["turtle-gouda"]._to_iface("swp6") == "GigabitEthernet1/0/6"


def test_mixed_eos_and_ios(tmp_path):
    sw_path = _write(tmp_path, "switches.json", [
        {"name": "rabbit-gouda", "driver": "eos", "host": "rabbit-gouda"},
        {"name": "turtle-gouda", "driver": "ios", "host": "turtle-gouda"},
    ])
    creds_path = _switch_creds(tmp_path)
    host_path = _write(tmp_path, "credentials-host.json",
                       [{"user": "flax", "pass": "x"}])

    switches, _u, _p, _h = load_switches(sw_path, creds_path, host_path)

    assert isinstance(switches["rabbit-gouda"], AristaEAPI)
    assert isinstance(switches["turtle-gouda"], IOS)


def test_mixed_eos_ios_cumulus(tmp_path):
    sw_path = _write(tmp_path, "switches.json", [
        {"name": "rabbit-gouda", "driver": "eos", "host": "rabbit-gouda"},
        {"name": "turtle-gouda", "driver": "ios", "host": "turtle-gouda"},
        {"name": "turtle-rhythm", "driver": "cumulus", "host": "turtle-rhythm"},
    ])
    creds_path = _switch_creds(tmp_path)
    host_path = _write(tmp_path, "credentials-host.json",
                       [{"user": "flax", "pass": "x"}])

    switches, _u, _p, _h = load_switches(sw_path, creds_path, host_path)

    assert isinstance(switches["rabbit-gouda"], AristaEAPI)
    assert isinstance(switches["turtle-gouda"], IOS)
    assert isinstance(switches["turtle-rhythm"], Cumulus)


def test_cumulus_entry_builds_cumulus_driver(tmp_path):
    """cumulus driver now builds a Cumulus instance (was: skipped)."""
    sw_path = _write(tmp_path, "switches.json", [
        {"name": "rabbit-rhythm", "driver": "eos", "host": "rabbit-rhythm"},
        {"name": "turtle-rhythm", "driver": "cumulus", "host": "turtle-rhythm"},
    ])
    creds_path = _switch_creds(tmp_path)
    host_path = _write(tmp_path, "credentials-host.json", [])

    switches, _ou, _op, _hc = load_switches(sw_path, creds_path, host_path)
    assert "rabbit-rhythm" in switches              # eos kept
    assert "turtle-rhythm" in switches              # cumulus now built
    assert isinstance(switches["turtle-rhythm"], Cumulus)
    assert switches["turtle-rhythm"].name == "turtle-rhythm"
    # runner is a callable wired by _make_cumulus_runner (not yet invoked)
    assert callable(switches["turtle-rhythm"].runner)


def test_cumulus_runner_is_constructed_not_invoked(tmp_path):
    """_make_cumulus_runner returns a callable; building load_switches does not
    make any real ssh call -- inspecting the closure is enough."""
    runner = _make_cumulus_runner("turtle-x", "cumulus", "CumulusLinux!")
    assert callable(runner)


def test_unknown_driver_is_skipped_not_raised(tmp_path):
    """A genuinely unknown driver (e.g. 'foobar') is skipped with a warning
    so reconcile degrades gracefully when a new type appears in switches.json
    before its write transport is implemented."""
    sw_path = _write(tmp_path, "switches.json", [
        {"name": "rabbit-rhythm", "driver": "eos", "host": "rabbit-rhythm"},
        {"name": "mystery-sw", "driver": "foobar", "host": "mystery-sw"},
    ])
    creds_path = _switch_creds(tmp_path)
    host_path = _write(tmp_path, "credentials-host.json", [])

    switches, _ou, _op, _hc = load_switches(sw_path, creds_path, host_path)
    assert "rabbit-rhythm" in switches   # eos kept
    assert "mystery-sw" not in switches  # unknown driver skipped, no crash


def test_missing_required_key_raises(tmp_path):
    sw_path = _write(tmp_path, "switches.json", [
        {"name": "rabbit-x", "driver": "eos"},  # no host
    ])
    creds_path = _switch_creds(tmp_path)
    host_path = _write(tmp_path, "credentials-host.json", [])

    with pytest.raises(ValueError) as exc:
        load_switches(sw_path, creds_path, host_path)
    assert "host" in str(exc.value)


def test_switches_not_a_list_raises(tmp_path):
    sw_path = _write(tmp_path, "switches.json", {"not": "a list"})
    creds_path = _switch_creds(tmp_path)
    host_path = _write(tmp_path, "credentials-host.json", [])

    with pytest.raises(ValueError):
        load_switches(sw_path, creds_path, host_path)


def test_host_creds_returned_verbatim(tmp_path):
    sw_path = _write(tmp_path, "switches.json", [])
    creds_path = _switch_creds(tmp_path)
    host_creds = [{"user": "flax", "pass": "DontCh4ngeMe!"},
                  {"user": "root", "pass": "test-idrac-pw"}]
    host_path = _write(tmp_path, "credentials-host.json", host_creds)

    _switches, _u, _p, got = load_switches(sw_path, creds_path, host_path)
    assert got == host_creds


def test_obmc_creds_returned_from_credentials_json(tmp_path):
    """obmc_user/obmc_pass must come from credentials.json (obmcuser/obmcpass),
    NOT from credentials-bmc.json. Verified against reaper_leased.py line 1469:
    ssh_runner(ip, creds["obmcuser"], creds["obmcpass"]) where creds is
    credentials.json."""
    sw_path = _write(tmp_path, "switches.json", [])
    creds_path = _switch_creds(tmp_path,
                               extra={"obmcuser": "root", "obmcpass": "test-obmc-pw"})
    host_path = _write(tmp_path, "credentials-host.json", [])

    _s, obmc_user, obmc_pass, _h = load_switches(sw_path, creds_path, host_path)
    assert (obmc_user, obmc_pass) == ("root", "test-obmc-pw")


def test_obmc_creds_absent_yields_empty_strings(tmp_path):
    """Missing obmcuser/obmcpass in credentials.json yields ('', '') -- safe
    degrade since kick_via_bmc_ll no-ops on empty creds."""
    sw_path = _write(tmp_path, "switches.json", [])
    # credentials.json without obmcuser/obmcpass
    creds_path = _write(tmp_path, "credentials.json",
                        {"eosuser": "admin", "eospass": "pw"})
    host_path = _write(tmp_path, "credentials-host.json", [])

    _s, obmc_user, obmc_pass, _h = load_switches(sw_path, creds_path, host_path)
    assert (obmc_user, obmc_pass) == ("", "")


# --- transport-shape checks (no real network) ------------------------------

def test_ios_to_iface_rejects_bad_form():
    with pytest.raises(ValueError):
        _ios_to_iface("Ethernet6/1")


def test_eapi_caller_is_a_closure_not_invoked():
    # Building the caller must not touch the network; we only assert it is a
    # zero-side-effect callable taking a command list.
    caller = _make_eapi_caller("rabbit-x", "admin", "pw")
    assert callable(caller)


def test_eapi_caller_raises_switch_unreachable_on_transport_error(monkeypatch):
    # Force the HTTPSConnection to blow up so we exercise the lifted retry/raise
    # path without a real switch.
    import flax_reconcile.drivers as d

    class BoomConn:
        def __init__(self, *a, **kw):
            raise OSError("no route to host")

    monkeypatch.setattr(d.http.client, "HTTPSConnection", BoomConn)
    caller = _make_eapi_caller("rabbit-x", "admin", "pw")
    with pytest.raises(SwitchUnreachable):
        caller(["configure terminal", "interface Ethernet1", "shutdown", "end"])


def test_ios_runner_is_a_closure_not_invoked():
    runner = _make_ios_runner("turtle-x", "admin", "pw")
    assert callable(runner)
