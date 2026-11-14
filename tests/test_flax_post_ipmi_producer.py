# tests/test_flax_post_ipmi_producer.py
"""Owned IPMI producer: parsers + run_once writes post_state and upserts post_node."""
from flax_post.observe import ipmi

import pytest


@pytest.fixture(autouse=True)
def _fru0_hermetic(monkeypatch):
    monkeypatch.setattr(ipmi, "_sleep", lambda s: None)
    monkeypatch.setattr(ipmi._fm_live, "current", lambda path=None: {})


@pytest.fixture(autouse=True)
def _reset_ipmi_pairing_state():
    """_PAIRING/_WORKING/_cipher_override are module-global (ipmi.py), so a
    prior test's configure_pairing/_remember call must never leak into the
    next one -- fix round 1, 2026-09-28 (task-7-brief minor #4)."""
    ipmi._PAIRING = {}
    ipmi._WORKING.clear()
    if hasattr(ipmi._cipher_override, "value"):
        del ipmi._cipher_override.value
    yield
    ipmi._PAIRING = {}
    ipmi._WORKING.clear()
    if hasattr(ipmi._cipher_override, "value"):
        del ipmi._cipher_override.value


_FRU = ("Product Manufacturer  : Wiwynn\nProduct Name : Tioga Pass\n"
        "Product Serial        : TPN12345678\n")
_POWER = "Chassis Power is on\n"
_SDR = ("CPU0 Temp        | 45 degrees C | ok\n"
        "HSC Input Power  | 210 Watts    | ok\n")
_SEL = "1 | 06/26/2026 | 11:02:14 | Processor CATERR | Asserted\n"


def _vouch(devs):
    """observe_identity as it would be for these devices: each port's bmc
    reservation IS the blade on it, paired with that port's host reservation.
    Tests that need a STALE reservation pass their own `observed` instead."""
    out = {}
    for d in devs:
        row = out.setdefault(d["port"], {"bmc_mac": None, "nic_mac": None, "chassis_sn": None})
        row["bmc_mac" if d.get("kind") == "bmc" else "nic_mac"] = d.get("mac")
    return out


def _runner(host, user, password, args, timeout=None):
    if args and args[0] == "exec":
        return _POWER + _SDR            # one combined session -> both blocks in stdout
    return {("fru", "print", "0"): _FRU, ("power", "status"): _POWER, "sdr": _SDR, ("sel", "elist"): _SEL}[
        tuple(args) if len(args) > 1 else args[0]]


def test_parsers():
    assert ipmi._fru.read_baseboard(_FRU, {})["serial"] == "TPN12345678"
    assert ipmi._parse_power(_POWER) == "on"
    assert ipmi._parse_watts(_SDR) == "210 W"
    assert ipmi._parse_sdr(_SDR)["HSC Input Power"]["value"] == "210 Watts"
    ev = ipmi._parse_sel(_SEL)[0]
    assert "CATERR" in ev["event"]
    assert ev["ts"] == "06/26/2026 11:02:14"          # timestamp preserved for the UI


def test_run_once_fans_out_all_bmcs():
    """run_once probes every BMC (the pool path), not just the first."""
    import threading
    states = {}
    lock = threading.Lock()
    devs = [{"port": f"et{n}b1", "kind": "bmc", "mac": f"aa:bb:{n}",
             "lease_ip": f"172.24.{n}.101", "switch": "rabbit-edam"} for n in range(6, 16)]

    def _set(port, **f):
        with lock:
            states.setdefault(port, {}).update(f)

    ipmi.run_once(devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}], ipmi_runner=_runner,
                  ping=lambda ip: True, set_state=_set, upsert_node=lambda mac, **f: None,
                  settings={"order_no": None}, workers=8, observed=_vouch(devs))
    assert len(states) == 10                            # all 10 BMCs processed concurrently
    assert all(s["serial"] == "TPN12345678" for s in states.values())


def test_probe_blade_collects_all():
    out = ipmi.probe_blade("172.24.6.101", [{"bmcuser": "u", "bmcpass": "p"}], _runner)
    assert out["serial"] == "TPN12345678" and out["power_on"] == "on" and out["watts"] == "210 W"
    assert len(out["sel"]) == 1


def test_probe_blade_uses_one_session_for_power_and_sdr():
    """power status + sdr must be ONE ipmitool call (`exec <script>`), not two separate
    RMCP+ sessions -- the fix for BMC session-table pressure choking the heavy pass."""
    calls = []

    def runner(host, user, password, args, timeout=None):
        calls.append((tuple(args[:1]), timeout))     # just the verb + timeout budget
        if args == ["fru", "print", "0"]:
            return _FRU
        if args[0] == "exec":
            return _POWER + _SDR
        if args == ["sel", "elist"]:
            return _SEL
        raise AssertionError("unexpected separate power/sdr call: %r" % (args,))

    out = ipmi.probe_blade("172.24.6.101", [{"bmcuser": "u", "bmcpass": "p"}], runner)
    assert out["power_on"] == "on" and out["watts"] == "210 W"
    verbs = [c[0] for c in calls]
    assert ("fru",) in verbs and ("exec",) in verbs and ("sel",) in verbs
    assert len(calls) == 3                               # fru, exec(power+sdr), sel -- not 4
    exec_timeout = next(t for (v, t) in calls if v == ("exec",))
    assert exec_timeout == ipmi.SDR_TIMEOUT_SECS         # the slow leg gets the wider budget


def test_probe_blade_sdr_timeout_does_not_lose_power_status():
    """When the combined power+sdr session itself fails, the whole read is best-effort:
    power_on/watts/sdr all stay unset for this pass rather than partially populated."""
    def runner(host, user, password, args, timeout=None):
        if args == ["fru", "print", "0"]:
            return _FRU
        if args and args[0] == "exec":
            raise TimeoutError("sdr walk exceeded SDR_TIMEOUT_SECS")
        return _SEL

    out = ipmi.probe_blade("172.24.6.101", [{"bmcuser": "u", "bmcpass": "p"}], runner)
    assert out["serial"] == "TPN12345678"                # fru still independent
    assert out["power_on"] is None and out["watts"] is None and out["sdr"] == {}


def test_probe_power_reads_only_power():
    """The fast lane issues ONLY the cheap `power status` call — not fru/sdr/sel."""
    calls = []

    def runner(host, user, password, args):
        calls.append(tuple(args))
        return _POWER

    assert ipmi.probe_power("172.24.6.101", [{"bmcuser": "u", "bmcpass": "p"}], runner) == "on"
    assert calls == [("power", "status")]


def test_run_power_once_writes_power_and_ping_only():
    """run_power_once merges ONLY power_on + bmc_pinged (the heavy serial/SEL/watts
    are left to run_once so the fast pass stays cheap and can't clobber them)."""
    states = {}
    devs = [{"port": "et6b1", "kind": "bmc", "mac": "aa:bb", "lease_ip": "172.24.6.101",
             "switch": "rabbit-edam"}]
    ipmi.run_power_once(
        prior={}, devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}], ipmi_runner=_runner,
        ping=lambda ip: True,
        set_state=lambda port, **f: states.setdefault(port, {}).update(f), workers=1)
    s = states["et6b1"]
    assert s["power_on"] == "on" and s["bmc_pinged"] is True
    assert "serial" not in s and "sel" not in s and "watts" not in s


def test_post_switch_derives_from_geometry(monkeypatch):
    from flax_post import geometry
    monkeypatch.setattr(geometry, "load_geometry",
                        lambda *a, **k: {"racks": {"rabbit-lorax": {}}})
    assert ipmi._post_switch() == "rabbit-lorax"


def test_run_power_once_skips_probe_when_bmc_unpinged():
    """Finding 5 (fix wave): a dark BMC (no ping answer) must not run
    probe_power's ipmi attempts at all -- 2 extra paired calls per unpinged
    BMC every fast-lane cycle otherwise, for a read that can never answer.
    power_on is written as None, bmc_pinged False, and the ipmi runner is
    never called."""
    calls = []

    def runner(host, user, password, args, timeout=None):
        calls.append(args)
        return _POWER

    states = {}
    devs = [{"port": "et6b1", "kind": "bmc", "mac": "aa:bb", "lease_ip": "172.24.6.101",
             "switch": "rabbit-edam"}]
    ipmi.run_power_once(
        prior={}, devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}], ipmi_runner=runner,
        ping=lambda ip: False,
        set_state=lambda port, **f: states.setdefault(port, {}).update(f), workers=1)
    assert calls == []
    s = states["et6b1"]
    assert s["bmc_pinged"] is False
    assert s["power_on"] is None


def test_run_power_once_processes_derived_lorax_switch():
    """The braintree post switch is rabbit-lorax; the producer must process its
    BMCs (not filter them out against a hardcoded rabbit-edam) -- the Discover
    bmc-pinged/power freeze fix -- and STAMP the derived switch so the viewer
    (reading rabbit-lorax) finds the row."""
    states = {}
    devs = [{"port": "et5b1", "kind": "bmc", "mac": "cc:dd", "lease_ip": "172.24.5.101",
             "switch": "rabbit-lorax"}]
    ipmi.run_power_once(
        prior={}, devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}], ipmi_runner=_runner,
        ping=lambda ip: True,
        set_state=lambda port, **f: states.setdefault(port, {}).update(f),
        workers=1, switch="rabbit-lorax")
    assert states["et5b1"]["bmc_pinged"] is True
    assert states["et5b1"]["power_on"] == "on"
    assert states["et5b1"]["switch"] == "rabbit-lorax"


def test_run_power_once_isolates_other_site_switch():
    """Cross-site guard: a rabbit-lorax post site must NOT process a rabbit-edam
    device (and vice-versa)."""
    states = {}
    devs = [{"port": "et5b1", "kind": "bmc", "mac": "cc:dd", "lease_ip": "172.24.5.101",
             "switch": "rabbit-edam"}]
    ipmi.run_power_once(
        prior={}, devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}], ipmi_runner=_runner,
        ping=lambda ip: True,
        set_state=lambda port, **f: states.setdefault(port, {}).update(f),
        workers=1, switch="rabbit-lorax")
    assert states == {}


def test_run_power_once_fans_out_all_bmcs():
    import threading
    states = {}
    lock = threading.Lock()
    devs = [{"port": f"et{n}b1", "kind": "bmc", "mac": f"aa:{n}",
             "lease_ip": f"172.24.{n}.101", "switch": "rabbit-edam"} for n in range(6, 16)]

    def _set(port, **f):
        with lock:
            states.setdefault(port, {}).update(f)

    ipmi.run_power_once(prior={}, devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}], ipmi_runner=_runner,
                        ping=lambda ip: True, set_state=_set, workers=8)
    assert len(states) == 10
    assert all(s["power_on"] == "on" and s["bmc_pinged"] is True for s in states.values())


def test_run_once_writes_state_and_node():
    states, nodes = {}, {}
    devs = [{"port": "et6b1", "kind": "bmc", "mac": "aa:bb", "lease_ip": "172.24.6.101",
             "reservation_ip": "172.24.6.101", "switch": "rabbit-edam"},
            {"port": "et6b1", "kind": "host", "mac": "cc:dd", "lease_ip": "172.24.7.101",
             "reservation_ip": "172.24.7.101", "switch": "rabbit-edam"}]
    ipmi.run_once(
        devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}], ipmi_runner=_runner,
        ping=lambda ip: True,
        set_state=lambda port, **f: states.setdefault(port, {}).update(f),
        upsert_node=lambda mac, **f: nodes.setdefault(mac, {}).update(f),
        settings={"order_no": "RRORD-003", "population": None}, observed=_vouch(devs))
    assert states["et6b1"]["serial"] == "TPN12345678"
    assert "power_on" not in states["et6b1"]              # live power is the fast lane's job now
    assert "bmc_pinged" not in states["et6b1"]            # ditto bmc liveness
    assert states["et6b1"]["host_pinged"] is True
    assert states["et6b1"]["order_no"] == "RRORD-003"
    assert nodes["aa:bb"]["serial"] == "TPN12345678"      # durable node keyed by bmc_mac
    assert nodes["aa:bb"]["power_on"] == "on"             # durable power still recorded by the heavy pass
    assert nodes["aa:bb"]["last_port"] == "et6b1"


def test_run_once_set_state_failure_does_not_skip_upsert_node():
    """A set_state exception must NOT prevent upsert_node from being called (two-tier independence)."""
    nodes = {}
    devs = [{"port": "et6b1", "kind": "bmc", "mac": "aa:bb", "lease_ip": "172.24.6.101",
             "reservation_ip": "172.24.6.101", "switch": "rabbit-edam"}]

    def _boom(*a, **k):
        raise RuntimeError("boom")

    ipmi.run_once(
        devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}], ipmi_runner=_runner,
        ping=lambda ip: True,
        set_state=_boom,
        upsert_node=lambda mac, **f: nodes.setdefault(mac, {}).update(f),
        settings={"order_no": "RRORD-003", "population": None}, observed=_vouch(devs))
    assert "aa:bb" in nodes, "upsert_node must be called even when set_state raises"
    assert nodes["aa:bb"]["last_port"] == "et6b1"


def test_run_once_no_creds_is_noop():
    states = {}
    ipmi.run_once(devices=[{"port": "et6b1", "kind": "bmc", "mac": "aa:bb",
                            "lease_ip": "1.2.3.4", "switch": "rabbit-edam"}],
                  creds=[], ipmi_runner=_runner, ping=lambda ip: True,
                  set_state=lambda port, **f: states.setdefault(port, f),
                  upsert_node=lambda mac, **f: None, settings={})
    assert states == {}


_FRU_TEXT = """Product Name          : Test Platform
Product Serial        : SN123
Board Mfg             : TestCo
"""


def test_fru0_fields_are_the_baseboard_block_only():
    from flax_post.observe import ipmi
    fru = ipmi._fru0_fields(_FRU_TEXT)
    assert fru["Product Name"] == "Test Platform"
    assert fru["Product Serial"] == "SN123"
    assert fru["Board Mfg"] == "TestCo"
    assert ipmi._fru0_fields(_FRU_BLOCKS) == {"Chassis Serial": "C.M.B60.00X1C.001109201900Z2",
                                              "Product Serial": "WTH49152ZGM1A"}
    assert ipmi._fru0_fields(_FRU0_ABSENT) == {}


def test_probe_blade_returns_fru_dict():
    from flax_post.observe import ipmi

    def runner(ip, u, p, args, timeout=None):
        if args == ["fru", "print", "0"]:
            return _FRU_TEXT
        raise RuntimeError("other calls fail")

    fields = ipmi.probe_blade("10.0.0.1", [{"bmcuser": "u", "bmcpass": "p"}], runner)
    assert fields["serial"] == "SN123"
    assert fields["fru"]["Product Name"] == "Test Platform"


def _run_once_kwargs(recorded, devs):
    """Common run_once fakes: records collector + no-op state writers."""

    def runner(ip, u, p, args, timeout=None):
        if args == ["fru", "print", "0"]:
            return _FRU_TEXT
        return ""

    return dict(
        devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}],
        ipmi_runner=runner, ping=lambda ip: False,
        set_state=lambda *a, **k: None, upsert_node=lambda *a, **k: None,
        settings={"order_no": "RRORD-T1", "population": None, "customer": "acme"},
        orders={"RRORD-T1": {"customer": "acme"}},
        workers=1,
        record_observation=lambda **kw: recorded.append(kw),
        observed=_vouch(devs),
    )


def test_run_once_records_observation_with_keys_and_host_mac():
    from flax_post.observe import ipmi
    recorded = []
    devs = [
        {"port": "et1b1", "kind": "bmc", "mac": "aa:01", "switch": "rabbit-edam",
         "reservation_ip": "10.0.0.1"},
        {"port": "et1b1", "kind": "host", "mac": "aa:00", "switch": "rabbit-edam",
         "reservation_ip": "10.0.6.1"},
    ]
    ipmi.run_once(**_run_once_kwargs(recorded, devs))
    assert len(recorded) == 1
    kw = recorded[0]
    assert kw["p0_mac"] == "aa:00"                       # host mac, not bmc mac
    assert kw["serial"] == "SN123"
    assert kw["fru"]["Product Name"] == "Test Platform"
    assert kw["keys"] == {"order": "RRORD-T1", "customer": "acme"}


def test_run_once_record_failure_does_not_break_pass():
    from flax_post.observe import ipmi
    devs = [{"port": "et1b1", "kind": "bmc", "mac": "aa:01",
             "switch": "rabbit-edam", "reservation_ip": "10.0.0.1"}]
    kwargs = _run_once_kwargs([], devs)
    kwargs["record_observation"] = lambda **kw: (_ for _ in ()).throw(RuntimeError("db down"))
    ipmi.run_once(**kwargs)                               # must not raise


class _FakeRedfish:
    """RedfishClient stand-in for the post-observe IPMI fallback."""
    def __init__(self, serial=None, power=None, watts=None):
        self._serial, self._power, self._watts = serial, power, watts
    def get_serial(self): return (self._serial, "ok")
    def get_power_state(self): return (self._power, "ok")
    def get_power_watts(self): return (self._watts, "ok")


def _ipmi_dead(ip, u, p, args, timeout=None):
    raise RuntimeError("no route to host")     # every IPMI call fails (Redfish-only AMI board)


def test_probe_blade_redfish_fallback_when_ipmi_dead():
    """A Redfish-only AMI board answers no IPMI; the fallback fills serial + power
    from Redfish so the blade doesn't stall at Discover serial / power-read."""
    fields = ipmi.probe_blade("172.25.25.102", [{"bmcuser": "u", "bmcpass": "p"}],
                              _ipmi_dead, redfish_client=_FakeRedfish(serial="WTH9", power="Off"))
    assert fields["serial"] == "WTH9"
    assert fields["power_on"] == "off"


def test_probe_blade_redfish_not_used_when_ipmi_works():
    """No regression: when IPMI answers, the Redfish fallback is never consulted."""
    called = {"n": 0}

    class _Boom:
        def get_serial(self): called.__setitem__("n", called["n"] + 1); return ("X", "")
        def get_power_state(self): called.__setitem__("n", called["n"] + 1); return ("On", "")

    fields = ipmi.probe_blade("172.24.6.101", [{"bmcuser": "u", "bmcpass": "p"}],
                              _runner, redfish_client=_Boom())
    assert fields["serial"] == "TPN12345678"     # from IPMI FRU
    assert called["n"] == 0                       # Redfish untouched


def test_probe_power_redfish_fallback_when_ipmi_dead():
    got = ipmi.probe_power("172.25.25.102", [{"bmcuser": "u", "bmcpass": "p"}],
                           _ipmi_dead, redfish_client=_FakeRedfish(power="On"))
    assert got == "on"


def test_run_power_once_uses_redfish_factory_for_dead_ipmi():
    """run_power_once builds a per-BMC Redfish client via make_redfish and the fast
    lane falls back to it when IPMI is dead — the AMI board's power resolves."""
    states = {}
    devs = [{"port": "et25b2", "kind": "bmc", "mac": "0c:8c", "lease_ip": "172.25.25.102",
             "switch": "rabbit-lorax"}]
    ipmi.run_power_once(
        prior={}, devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}], ipmi_runner=_ipmi_dead,
        ping=lambda ip: True,
        set_state=lambda port, **f: states.setdefault(port, {}).update(f),
        workers=1, switch="rabbit-lorax",
        make_redfish=lambda ip: _FakeRedfish(power="Off"))
    assert states["et25b2"]["power_on"] == "off"


def test_run_once_uses_redfish_factory_for_dead_ipmi_serial():
    states, nodes = {}, {}
    devs = [{"port": "et25b2", "kind": "bmc", "mac": "0c:8c", "lease_ip": "172.25.25.102",
             "reservation_ip": "172.25.25.102", "switch": "rabbit-lorax"}]
    ipmi.run_once(
        devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}], ipmi_runner=_ipmi_dead,
        ping=lambda ip: True,
        set_state=lambda port, **f: states.setdefault(port, {}).update(f),
        upsert_node=lambda mac, **f: nodes.setdefault(mac, {}).update(f),
        settings={"order_no": None}, workers=1, switch="rabbit-lorax",
        make_redfish=lambda ip: _FakeRedfish(serial="WTH9", power="Off"),
        observed=_vouch(devs))
    assert states["et25b2"]["serial"] == "WTH9"
    assert nodes["0c:8c"]["serial"] == "WTH9"


def test_run_once_does_not_clobber_watts_on_sdr_failure():
    """A failed/timed-out power+sdr session must NOT overwrite a previously-good
    watts/sdr reading with null -- set_state should not even receive those keys
    for this pass, so the JSONB merge (vars || EXCLUDED.vars) leaves them alone."""
    calls = []

    def flaky_runner(ip, u, p, args, timeout=None):
        if args == ["fru", "print", "0"]:
            return _FRU
        if args and args[0] == "exec":
            raise TimeoutError("sdr walk exceeded SDR_TIMEOUT_SECS")
        return _SEL

    def _set(port, **f):
        calls.append(f)

    devs = [{"port": "et6b1", "kind": "bmc", "mac": "aa:bb", "lease_ip": "172.24.6.101",
             "switch": "rabbit-edam"}]
    ipmi.run_once(devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}],
                  ipmi_runner=flaky_runner, ping=lambda ip: True, set_state=_set,
                  upsert_node=lambda mac, **f: None, settings={"order_no": None}, workers=1,
                  observed=_vouch(devs))
    assert len(calls) == 1
    written = calls[0]
    assert "watts" not in written and "sdr" not in written
    assert written["serial"] == "TPN12345678"            # fields that DID read stay written


def test_default_make_redfish_falls_back_to_bmc_creds(monkeypatch):
    """credentials-redfish.json is empty on eindhoven -- the factory must still build
    a usable client from the IPMI bmc_creds (verified 2026-09-10: this fleet's Redfish
    Basic auth accepts the same USERID/PASSW0RD pair as IPMI) rather than going inert."""
    from flax_post.fwd import redfish as redfish_mod
    seen = {}

    class _Recording:
        def __init__(self, ip, creds):
            seen["ip"], seen["creds"] = ip, creds

    monkeypatch.setattr(redfish_mod, "RedfishClient", _Recording)
    bmc_creds = [{"bmcuser": "USERID", "bmcpass": "sekrit"}]
    make = ipmi._default_make_redfish([], bmc_creds=bmc_creds)
    client = make("172.24.6.101")
    assert isinstance(client, _Recording)
    assert seen["creds"] == bmc_creds


def test_default_make_redfish_prefers_dedicated_redfish_creds(monkeypatch):
    from flax_post.fwd import redfish as redfish_mod
    seen = {}

    class _Recording:
        def __init__(self, ip, creds):
            seen["creds"] = creds

    monkeypatch.setattr(redfish_mod, "RedfishClient", _Recording)
    rf_creds = [{"bmcuser": "Administrator", "bmcpass": "rf-pass"}]
    bmc_creds = [{"bmcuser": "USERID", "bmcpass": "ipmi-pass"}]
    make = ipmi._default_make_redfish(rf_creds, bmc_creds=bmc_creds)
    make("172.24.6.101")
    assert seen["creds"] == rf_creds


def test_run_once_default_recorder_is_disabled():
    """record_observation=None (the default) must not touch records/DB."""
    from flax_post.observe import ipmi
    devs = [{"port": "et1b1", "kind": "bmc", "mac": "aa:01",
             "switch": "rabbit-edam", "reservation_ip": "10.0.0.1"}]
    kwargs = _run_once_kwargs([], devs)
    del kwargs["record_observation"]
    ipmi.run_once(**kwargs)                               # no DB pool, no raise


# ---- completion-latch clearing (spec 2026-09-11-post-completion-latch §2.3) ----
_LATCHED = {"bmc_mac": "aa:bb", "power_on": "off",
            "done": {"verdict": "pass", "identify": "done", "power_off": "done"},
            "qual": {"run_id": "r1"}, "pop": {"verdict": "green"}}


def test_clear_fields_off_to_on_on_latched_row():
    out = ipmi.clear_fields_for(_LATCHED, "aa:bb", "on")
    assert {k: out[k] for k in ("done", "qual", "pop")} == {"done": {}, "qual": {}, "pop": {}}
    assert "ladder" in out


def test_clear_fields_fail_verdict_is_latched_too():
    row = dict(_LATCHED, done={"verdict": "fail"})
    out = ipmi.clear_fields_for(row, "aa:bb", "on")
    assert {k: out[k] for k in ("done", "qual", "pop")} == {"done": {}, "qual": {}, "pop": {}}
    assert "ladder" in out


def test_clear_fields_no_transition_keeps_latch():
    assert ipmi.clear_fields_for(dict(_LATCHED, power_on="on"), "aa:bb", "on") == {}
    assert ipmi.clear_fields_for(_LATCHED, "aa:bb", "off") == {}
    assert ipmi.clear_fields_for(_LATCHED, "aa:bb", None) == {}


def test_clear_fields_human_off_to_on_resets_ladder_on_any_row():
    row = {"bmc_mac": "aa:bb", "power_on": "off", "done": {},
           "ladder": {"rung": "power-on", "power_on_pending": False, "fault": {"rung": "x"}}}
    out = ipmi.clear_fields_for(row, "aa:bb", "on", now=50.0)
    assert out["ladder"]["rung"] == "tftp-seen" and out["ladder"]["power_on_at"] == 50.0
    assert out["ladder"]["human_power_on"] is True
    assert out["done"] == {} and out["qual"] == {} and out["pop"] == {}


def test_clear_fields_latched_row_gets_ladder_too():
    out = ipmi.clear_fields_for(_LATCHED, "aa:bb", "on", now=50.0)
    assert set(out) == {"done", "qual", "pop", "checks", "ladder", "ladder_reset_at", "ladder_reset_kind"}


def test_clear_fields_stamps_the_uncontended_reset_scalar():
    """`ladder` is a contended key (the worker writes it too, and set_state
    merges vars at the top level); `ladder_reset_at` is written only here, so
    the worker can tell a reset from its own stale cache."""
    out = ipmi.clear_fields_for(_LATCHED, "aa:bb", "on", now=50.0)
    assert out["ladder_reset_at"] == 50.0
    assert out["ladder"]["power_on_at"] == 50.0


def test_clear_fields_worker_issued_power_on_is_not_a_reset():
    row = {"bmc_mac": "aa:bb", "power_on": "off",
           "ladder": {"rung": "power-on", "power_on_pending": True, "fault": None}}
    assert ipmi.clear_fields_for(row, "aa:bb", "on", now=50.0) == {}


def test_clear_fields_recent_engine_power_attempt_is_not_a_human_reset():
    """Belt and braces for the flag write racing this pass's `prior` read:
    inside ENGINE_POWER_WINDOW_S of the worker's own attempt, off->on is the
    engine's however stale power_on_pending looks."""
    row = {"bmc_mac": "aa:bb", "power_on": "off",
           "ladder": {"rung": "power-on", "power_on_pending": False, "fault": None,
                      "last_power_attempt": 50.0}}
    w = ipmi.ENGINE_POWER_WINDOW_S
    assert ipmi.clear_fields_for(row, "aa:bb", "on", now=50.0 + w - 1) == {}
    assert ipmi.clear_fields_for(row, "aa:bb", "on", now=50.0 + w) == {}
    # ...and outside the window it is a human again
    out = ipmi.clear_fields_for(row, "aa:bb", "on", now=50.0 + w + 1)
    assert out["ladder"]["human_power_on"] is True and out["ladder_reset_at"] == 50.0 + w + 1


def test_clear_fields_rearm_cycle_is_not_a_second_human_reset():
    """spec 2026-11-11 §6: after a human reset the ladder re-arms PXE with its own cycle,
    stamped last_power_attempt; that cycle's off->on is the engine's."""
    row = {"bmc_mac": "aa:bb", "power_last": "off",
           "ladder": {"rung": "tftp-seen", "power_on_pending": False, "fault": None,
                      "last_power_attempt": 100.0, "power_on_at": 100.0}}
    assert ipmi.clear_fields_for(row, "aa:bb", "on", now=130.0) == {}


def test_clear_fields_rearm_dark_bmc_130s_after_first_stamp_is_not_a_human():
    """review I1: stamped at advance (101), re-stamped after the cycle (141); the AMI/onetree
    BMC is dark ~90 s; the first definite `on` at 231 is still the engine's."""
    row = {"bmc_mac": "aa:bb", "power_last": "off",
           "ladder": {"rung": "tftp-seen", "fault": None, "power_on_at": 101.0,
                      "last_power_attempt": 141.0}}
    assert ipmi.clear_fields_for(row, "aa:bb", "on", now=231.0) == {}


def test_clear_fields_empty_rows_untouched():
    assert ipmi.clear_fields_for(None, "aa:bb", "on") == {}
    assert ipmi.clear_fields_for({}, "aa:bb", "on") == {}


def test_clear_fields_bare_row_resets_on_first_human_power_on():
    """A bare row (no ladder, no verdict) on first human power-on before the worker
    has touched it should still reset: the ladder key is absent until the worker's
    first advance."""
    row = {"bmc_mac": "aa:bb", "power_on": "off"}
    out = ipmi.clear_fields_for(row, "aa:bb", "on", now=50.0)
    assert out["ladder"]["rung"] == "tftp-seen" and out["ladder"]["power_on_at"] == 50.0
    assert out["done"] == {} and out["qual"] == {} and out["pop"] == {}


def test_clear_fields_different_mac_is_not_a_reset():
    """A blade swap leaves two BMC reservations on the port for a while; the lane
    then sees a different MAC every pass. That must never wipe the row -- the GC
    retires a departed blade from the switch FDB instead (2026-09-11: et25b3 was
    reset 116 times in an hour by an occupant-change rule)."""
    assert ipmi.clear_fields_for(dict(_LATCHED, power_on="on"), "cc:dd", "on") == {}
    assert ipmi.clear_fields_for(_LATCHED, "cc:dd", "off") == {}
    # ...but the off->on latch clear still applies whatever the MAC says
    out = ipmi.clear_fields_for(_LATCHED, "cc:dd", "on")
    assert {k: out[k] for k in ("done", "qual", "pop")} == {"done": {}, "qual": {}, "pop": {}}
    assert "ladder" in out


def test_run_power_once_clears_latch_on_power_on():
    states = {}
    devs = [{"port": "et6b1", "kind": "bmc", "mac": "aa:bb", "lease_ip": "172.24.6.101",
             "switch": "rabbit-edam"}]
    ipmi.run_power_once(
        devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}], ipmi_runner=_runner,
        ping=lambda ip: True, prior={"et6b1": dict(_LATCHED)},
        set_state=lambda port, **f: states.setdefault(port, {}).update(f), workers=1)
    s = states["et6b1"]
    assert s["power_on"] == "on" and s["done"] == {} and s["qual"] == {} and s["pop"] == {}


def test_run_power_once_leaves_unlatched_rows_alone():
    states = {}
    devs = [{"port": "et6b1", "kind": "bmc", "mac": "aa:bb", "lease_ip": "172.24.6.101",
             "switch": "rabbit-edam"}]
    ipmi.run_power_once(
        devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}], ipmi_runner=_runner,
        ping=lambda ip: True, prior={"et6b1": {"bmc_mac": "aa:bb", "power_on": "off"}},
        set_state=lambda port, **f: states.setdefault(port, {}).update(f), workers=1)
    s = states["et6b1"]
    # off->on on a bare row resets the ladder and latch slices (spec 2026-09-11 post-slot-ladder §5)
    assert s["power_on"] == "on" and s["done"] == {} and s["qual"] == {} and s["pop"] == {}
    assert "ladder" in s


def test_run_power_once_reads_prior_state_once_when_not_given(monkeypatch):
    reads = []
    monkeypatch.setattr(ipmi.state, "read_state", lambda: reads.append(1) or {"et6b1": dict(_LATCHED)})
    states = {}
    devs = [{"port": "et6b1", "kind": "bmc", "mac": "aa:bb", "lease_ip": "172.24.6.101",
             "switch": "rabbit-edam"},
            {"port": "et6b2", "kind": "bmc", "mac": "aa:bc", "lease_ip": "172.24.6.102",
             "switch": "rabbit-edam"}]
    ipmi.run_power_once(
        devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}], ipmi_runner=_runner,
        ping=lambda ip: True,
        set_state=lambda port, **f: states.setdefault(port, {}).update(f), workers=1)
    assert reads == [1]
    assert states["et6b1"]["done"] == {} and "done" not in states["et6b2"]


def test_clear_fields_bridges_an_unreadable_gap_with_the_last_definite_reading():
    """et24b3 2026-09-12: operator power-on, the AMI BMC went dark, the lane
    read off -> None -> on and the strict off->on test never fired, so the old
    fail verdict re-latched. power_last (the last definite reading) bridges it."""
    dark = dict(_LATCHED, power_on=None, power_last="off")
    assert set(ipmi.clear_fields_for(dark, "aa:bb", "on", now=5000.0)) == {"done", "qual", "pop", "checks", "ladder", "ladder_reset_at", "ladder_reset_kind"}
    # an on -> None -> on wobble is not a transition
    assert ipmi.clear_fields_for(dict(_LATCHED, power_on=None, power_last="on"), "aa:bb", "on", now=5000.0) == {}
    # no last definite reading at all: nothing to compare, keep the latch
    assert ipmi.clear_fields_for(dict(_LATCHED, power_on=None), "aa:bb", "on", now=5000.0) == {}


def test_power_lane_records_the_last_definite_reading():
    states = {}
    devs = [{"port": "et6b1", "kind": "bmc", "mac": "aa:bb", "lease_ip": "172.24.6.101", "switch": "rabbit-edam"}]
    ipmi.run_power_once(devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}], ipmi_runner=_runner,
                        ping=lambda ip: True, prior={"et6b1": {"bmc_mac": "aa:bb", "power_on": "off"}},
                        set_state=lambda port, **f: states.setdefault(port, {}).update(f), workers=1)
    assert states["et6b1"]["power_on"] == "on" and states["et6b1"]["power_last"] == "on"
    # an unreadable pass writes power_on=None but leaves power_last alone
    states.clear()
    ipmi.run_power_once(devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}],
                        ipmi_runner=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("timeout")),
                        ping=lambda ip: True, prior={"et6b1": {"bmc_mac": "aa:bb", "power_on": "on", "power_last": "on"}},
                        set_state=lambda port, **f: states.setdefault(port, {}).update(f), workers=1)
    assert states["et6b1"]["power_on"] is None and "power_last" not in states["et6b1"]


# ---- occupant identity (spec 2026-09-14-post-occupant-reset §3/§4) ----
from flax_post.observe import ladder as L

_OLD = ("98:03:9b:75:2b:12", "WTH49152ZGM1A")        # et24b2's latched run, 2026-09-14
_NEW = ("98:03:9b:a8:f6:08", "WTH491718UM1A")        # the blade actually in et24b2


def _occ(mac, serial, since=10.0, last_read=20.0):
    return {"serial": serial, "bmc_mac": mac, "since": since, "last_read": last_read}


def _cand(mac, serial, first_read=985.0, last_read=985.0):
    return {"serial": serial, "bmc_mac": mac, "first_read": first_read, "last_read": last_read, "reads": 1}


def test_occupant_no_answer_changes_nothing():
    row = {"occupant": _occ(*_OLD)}
    assert ipmi.occupant_change(row, [], 1000.0) == {}
    assert ipmi.occupant_change(row, [(_NEW[0], None)], 1000.0) == {}


def test_occupant_first_read_without_a_run_adopts_without_reset():
    assert ipmi.occupant_change({}, [_NEW], 1000.0) == {
        "occupant": {"serial": _NEW[1], "bmc_mac": _NEW[0], "since": 1000.0, "last_read": 1000.0},
        "occupant_candidate": None}


def test_occupant_same_identity_refreshes_last_read_and_drops_a_candidate():
    row = {"occupant": _occ(*_OLD), "occupant_candidate": _cand(*_NEW)}
    out = ipmi.occupant_change(row, [_OLD], 1000.0)
    assert out["occupant"] == _occ(*_OLD, since=10.0, last_read=1000.0)
    assert out["occupant_candidate"] is None and "ladder" not in out


def test_occupant_mac_case_does_not_matter():
    row = {"occupant": _occ(_OLD[0].upper(), _OLD[1])}
    assert "ladder" not in ipmi.occupant_change(row, [_OLD], 1000.0)
    assert ipmi.occupant_change(row, [_OLD], 1000.0)["occupant_candidate"] is None


def test_occupant_new_serial_needs_two_consecutive_reads():
    row = {"occupant": _occ(*_OLD), "done": {"verdict": "fail"}}
    out = ipmi.occupant_change(row, [_NEW], 1000.0)
    assert out == {"occupant_candidate": {"serial": _NEW[1], "bmc_mac": _NEW[0],
                                          "first_read": 1000.0, "last_read": 1000.0, "reads": 1}}
    row.update(out)
    out = ipmi.occupant_change(row, [_NEW], 1015.0)
    assert out["ladder_reset_kind"] == "occupant" and out["ladder_reset_at"] == 1015.0
    assert out["occupant"] == {"serial": _NEW[1], "bmc_mac": _NEW[0], "since": 1015.0, "last_read": 1015.0}


def test_occupant_reset_clears_the_old_blades_slices_and_is_born_at_its_last_read():
    row = {"occupant": _occ(*_OLD, since=10.0, last_read=500.0), "occupant_candidate": _cand(*_NEW),
           "done": {"verdict": "fail"}, "qual": {"run_id": "r1"}, "pop": {"verdict": "red"},
           "power_last": "off", "power_on": "on", "ladder": {"rung": "done", "since": 400.0}}
    out = ipmi.occupant_change(row, [_NEW], 1000.0)
    assert out["done"] == {} and out["qual"] == {} and out["pop"] == {}
    assert out["power_last"] is None and out["power_on"] is None and out["occupant_candidate"] is None
    assert out["ladder_reset_born_at"] == 500.0
    assert out["ladder"] == L.occupant_reset(500.0, 1000.0)


def test_occupant_reset_birth_falls_back_to_the_latched_ladder_then_now():
    row = {"occupant": {"serial": _OLD[1], "bmc_mac": _OLD[0], "since": None, "last_read": None},
           "occupant_candidate": _cand(*_NEW), "ladder": {"rung": "done", "since": 400.0}}
    assert ipmi.occupant_change(row, [_NEW], 1000.0)["ladder_reset_born_at"] == 400.0
    row.pop("ladder")
    assert ipmi.occupant_change(row, [_NEW], 1000.0)["ladder_reset_born_at"] == 1000.0


def test_occupant_same_serial_new_bmc_mac_is_a_new_occupant():
    """et25b1 2026-09-14: the chassis kept its serial, a new mezzanine brought a new BMC MAC."""
    row = {"occupant": _occ("98:03:9b:c6:49:e6", "WTH49282P7M1A")}
    new = ("98:03:9b:a6:ab:78", "WTH49282P7M1A")
    row.update(ipmi.occupant_change(row, [new], 1000.0))
    assert ipmi.occupant_change(row, [new], 1015.0)["ladder_reset_kind"] == "occupant"


def test_occupant_old_identity_read_between_candidate_reads_never_confirms():
    row = {"occupant": _occ(*_OLD)}
    for now, read in ((1000.0, _NEW), (1015.0, _OLD), (1030.0, _NEW), (1045.0, _OLD), (1060.0, _NEW)):
        out = ipmi.occupant_change(row, [read], now)
        assert "ladder" not in out
        row.update(out)


def test_occupant_both_identities_in_one_pass_never_reset():
    row = {"occupant": _occ(*_OLD)}
    for now in (1000.0, 1015.0, 1030.0):
        out = ipmi.occupant_change(row, [_NEW, _OLD], now)
        assert "ladder" not in out and out["occupant_candidate"] is None
        row.update(out)


def test_occupant_two_new_identities_in_one_pass_is_ambiguous():
    other = ("98:03:9b:00:00:01", "WTHOTHER")
    assert ipmi.occupant_change({"occupant": _occ(*_OLD)}, [_NEW, other], 1000.0) == {}


def test_occupant_reset_is_rate_limited_after_a_recent_adoption():
    row = {"occupant": _occ(*_OLD, since=900.0, last_read=990.0), "occupant_candidate": _cand(*_NEW)}
    now = 1000.0
    while now < 900.0 + ipmi.OCCUPANT_RESET_MIN_S:
        out = ipmi.occupant_change(row, [_NEW], now)
        assert "ladder" not in out and out["occupant_candidate"]["last_read"] == now
        row.update(out)
        now += 15.0
    out = ipmi.occupant_change(row, [_NEW], 900.0 + ipmi.OCCUPANT_RESET_MIN_S)
    assert out["ladder_reset_kind"] == "occupant"


def test_occupant_backfill_from_the_latched_run_owner_resets_a_foreign_latch():
    """et24b2 2026-09-14: no occupant stamped yet, the latched run belongs to another blade."""
    row = {"done": {"verdict": "fail"}, "qual": {"run_id": "de0598"}, "pop": {"verdict": "red"},
           "ladder": {"rung": "done", "since": 1789376576.0}}
    owner = lambda run_id: _OLD if run_id == "de0598" else None
    now = 1789400000.0
    out = ipmi.occupant_change(row, [_NEW], now, run_owner=owner)
    assert set(out) == {"occupant_candidate"}
    row.update(out)
    out = ipmi.occupant_change(row, [_NEW], now + 15, run_owner=owner)
    assert out["ladder_reset_kind"] == "occupant" and out["qual"] == {} and out["done"] == {}
    assert out["ladder_reset_born_at"] == 1789376576.0


def test_occupant_backfill_matching_the_current_blade_just_stamps_it():
    row = {"done": {"verdict": "pass"}, "qual": {"run_id": "r9"}, "ladder": {"rung": "done", "since": 500.0}}
    out = ipmi.occupant_change(row, [_NEW], 1000.0, run_owner=lambda r: _NEW)
    assert out == {"occupant": {"serial": _NEW[1], "bmc_mac": _NEW[0], "since": 500.0, "last_read": 1000.0},
                   "occupant_candidate": None}


def test_occupant_backfill_with_no_artifacts_adopts_the_current_read():
    out = ipmi.occupant_change({"qual": {"run_id": "gone"}}, [_NEW], 1000.0, run_owner=lambda r: None)
    assert out["occupant"]["serial"] == _NEW[1] and "ladder" not in out


def test_occupant_candidate_not_read_within_the_confirm_gap_starts_over():
    row = {"occupant": _occ(*_OLD)}
    row.update(ipmi.occupant_change(row, [_NEW], 1000.0))
    assert ipmi.occupant_change(row, [], 1015.0) == {}                 # nothing answered
    late = 1000.0 + ipmi.OCCUPANT_CONFIRM_GAP_S + 1
    out = ipmi.occupant_change(row, [_NEW], late)
    assert "ladder" not in out
    assert out["occupant_candidate"]["reads"] == 1 and out["occupant_candidate"]["first_read"] == late


def test_occupant_candidate_read_again_within_the_confirm_gap_confirms():
    row = {"occupant": _occ(*_OLD)}
    row.update(ipmi.occupant_change(row, [_NEW], 1000.0))
    out = ipmi.occupant_change(row, [_NEW], 1000.0 + ipmi.OCCUPANT_CONFIRM_GAP_S)
    assert out["ladder_reset_kind"] == "occupant"


def test_occupant_reset_waits_until_the_stored_blade_is_quiet_for_the_gap():
    row = {"occupant": _occ(*_OLD)}
    for now, reads in ((1000.0, [_NEW]), (1015.0, [_OLD]), (1030.0, [_NEW]), (1045.0, []), (1060.0, [_NEW])):
        out = ipmi.occupant_change(row, reads, now)
        assert "ladder" not in out
        row.update(out)
    out = ipmi.occupant_change(row, [_NEW], 1015.0 + ipmi.OCCUPANT_CONFIRM_GAP_S + 1)
    assert out["ladder_reset_kind"] == "occupant"


def _occupant_pass(devs, prior, serial_by_ip, run_owner=lambda r: None):
    """One heavy pass with fakes; a BMC maps to None when it does not answer."""
    writes = []

    def runner(ip, u, p, args, timeout=None):
        if args == ["fru", "print", "0"] and serial_by_ip.get(ip):
            return "Product Serial        : %s\n" % serial_by_ip[ip]
        return ""

    ipmi.run_once(devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}], ipmi_runner=runner,
                  ping=lambda ip: False, set_state=lambda port, **f: writes.append((port, f)),
                  upsert_node=lambda *a, **k: None,
                  settings={"order_no": None, "population": None, "customer": None},
                  workers=1, make_redfish=lambda ip: None, prior=prior, run_owner=run_owner,
                  observed=_vouch(devs))
    return writes


_SWAP_DEVS = [{"port": "et25b3", "kind": "bmc", "mac": _OLD[0], "reservation_ip": "10.0.0.1",
               "switch": "rabbit-edam"},
              {"port": "et25b3", "kind": "bmc", "mac": _NEW[0], "reservation_ip": "10.0.0.2",
               "switch": "rabbit-edam"}]


def test_run_once_decides_the_occupant_once_per_port():
    prior = {"et25b3": {"occupant": _occ(*_NEW)}}
    writes = _occupant_pass(_SWAP_DEVS, prior, {"10.0.0.1": None, "10.0.0.2": _NEW[1]})
    occ = [f for _, f in writes if "occupant" in f or "occupant_candidate" in f]
    assert len(occ) == 1 and occ[0]["occupant"]["last_read"] > 20.0 and "ladder" not in occ[0]


def test_run_once_et25b3_replay_two_reservations_never_reset_the_new_blade():
    row = {"occupant": _occ(*_NEW), "ladder": {"rung": "tftp-seen", "since": 15.0}}
    for _ in range(10):
        for _, f in _occupant_pass(_SWAP_DEVS, {"et25b3": row}, {"10.0.0.1": None, "10.0.0.2": _NEW[1]}):
            assert "ladder" not in f and f.get("ladder_reset_kind") is None
            if "occupant" in f:
                row = dict(row, occupant=f["occupant"], occupant_candidate=f["occupant_candidate"])


def test_run_once_resets_a_slot_whose_blade_changed_after_two_passes():
    devs = [{"port": "et24b2", "kind": "bmc", "mac": _NEW[0], "reservation_ip": "10.0.0.2",
             "switch": "rabbit-edam"}]
    row = {"occupant": _occ(*_OLD), "done": {"verdict": "fail"}}
    resets = []
    for _ in range(2):
        for _, f in _occupant_pass(devs, {"et24b2": row}, {"10.0.0.2": _NEW[1]}):
            if "occupant_candidate" in f:
                row = dict(row, **f)
            if f.get("ladder_reset_kind") == "occupant":
                resets.append(f)
    assert len(resets) == 1 and resets[0]["done"] == {} and resets[0]["occupant"]["serial"] == _NEW[1]


def test_run_once_without_prior_and_with_injected_writers_skips_the_occupant_check():
    """Fakes that inject set_state never touch the DB for prior state."""
    writes = []
    ipmi.run_once(devices=_SWAP_DEVS[1:], creds=[{"bmcuser": "u", "bmcpass": "p"}],
                  ipmi_runner=lambda ip, u, p, a, timeout=None: "Product Serial : X\n" if a == ["fru", "print", "0"] else "",
                  ping=lambda ip: False, set_state=lambda port, **f: writes.append(f),
                  upsert_node=lambda *a, **k: None,
                  settings={"order_no": None, "population": None, "customer": None}, workers=1,
                  make_redfish=lambda ip: None)
    assert writes and not any("occupant" in f or "occupant_candidate" in f for f in writes)


def _serial(text):
    return ipmi._fru.read_baseboard(text, {})["serial"]


def test_ship_serial_never_falls_back_to_the_chassis_serial():
    assert _serial("Chassis Serial : C.M.B60.00X1C\n") is None
    assert _serial("Product Serial : WTH1\nProduct Serial : NICSN9\n") == "WTH1"
    assert _serial("Product Serial :   \nProduct Serial : WTH2\n") == "WTH2"


def test_probe_blade_product_serial_is_never_redfish_filled():
    class _RF:
        def get_serial(self): return ("SYS-SN", "ok")
        def get_power_state(self): return (None, "no power")
        def get_power_watts(self): return (None, "no watts")

    def runner(ip, u, p, args, timeout=None):
        raise RuntimeError("ipmi dead")

    out = ipmi.probe_blade("10.0.0.9", [{"bmcuser": "u", "bmcpass": "p"}], runner, redfish_client=_RF())
    assert out["serial"] == "SYS-SN" and out["product_serial"] is None


def test_run_once_chassis_serial_only_is_not_an_identity_read():
    devs = [{"port": "et24b2", "kind": "bmc", "mac": _NEW[0], "reservation_ip": "10.0.0.2",
             "switch": "rabbit-edam"}]
    writes = []
    ipmi.run_once(devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}],
                  ipmi_runner=lambda ip, u, p, a, timeout=None: "Chassis Serial : C.M.B60.00X1C\n" if a == ["fru", "print", "0"] else "",
                  ping=lambda ip: False, set_state=lambda port, **f: writes.append(f),
                  upsert_node=lambda *a, **k: None,
                  settings={"order_no": None, "population": None, "customer": None}, workers=1,
                  make_redfish=lambda ip: None, prior={"et24b2": {"occupant": _occ(*_OLD)}},
                  run_owner=lambda r: None)
    assert not any("occupant" in f or "occupant_candidate" in f or "ladder" in f for f in writes)


def test_clear_fields_off_to_on_across_occupants_is_still_a_human_reset():
    """Final review 2026-09-14: suppressing it consumed the off->on for a successor that never gives an
    identity read (Redfish-only board). The later occupant reset overrides this one."""
    row = dict(_LATCHED, power_on=None, power_last="off",
               occupant={"serial": "WTH491800OM1A", "bmc_mac": "98:03:9b:a6:fb:fc", "last_read": 45.0})
    out = ipmi.clear_fields_for(row, "98:03:9b:c6:49:e2", "on", now=50.0)
    assert out["ladder_reset_kind"] == "human" and out["done"] == {}


def test_clear_fields_the_occupants_own_off_to_on_is_a_human_reset():
    row = dict(_LATCHED, occupant={"serial": "SN", "bmc_mac": "AA:BB"})
    out = ipmi.clear_fields_for(row, "aa:bb", "on", now=50.0)
    assert out["ladder_reset_kind"] == "human" and out["ladder"]["human_power_on"] is True


def test_clear_fields_stale_occupant_keeps_the_human_power_on_reset():
    """A successor that never gives an identity read (Redfish-only board) keeps the operator's power-cycle reset."""
    row = dict(_LATCHED, power_on=None, power_last="off",
               occupant={"serial": "WTH491800OM1A", "bmc_mac": "98:03:9b:a6:fb:fc", "last_read": 50.0})
    out = ipmi.clear_fields_for(row, "98:03:9b:c6:49:e2", "on", now=50.0 + ipmi.OCCUPANT_RESET_MIN_S + 1)
    assert out["ladder_reset_kind"] == "human" and out["ladder"]["human_power_on"] is True


def test_clear_fields_occupant_without_last_read_keeps_the_human_reset():
    row = dict(_LATCHED, occupant={"serial": "X", "bmc_mac": "98:03:9b:a6:fb:fc"})
    assert ipmi.clear_fields_for(row, "aa:bb", "on", now=50.0)["ladder_reset_kind"] == "human"


def test_occupant_reset_after_a_human_reset_leaves_no_off_reading_for_another():
    """The occupant reset nulls power_on/power_last, so the next on reading is not an off->on."""
    row = {"occupant": _occ(*_OLD, since=10.0, last_read=500.0), "occupant_candidate": _cand(*_NEW),
           "done": {}, "power_last": "on", "power_on": "on", "ladder": {"rung": "tftp-seen", "since": 900.0}}
    fields = ipmi.occupant_change(row, [_NEW], 1000.0)
    row.update(fields)
    assert fields["ladder_reset_kind"] == "occupant"
    assert ipmi.clear_fields_for(row, _NEW[0], "on", now=1006.0) == {}


def test_occupant_backfill_trusts_the_run_owner_serial_not_its_mac():
    row = {"done": {"verdict": "pass"}, "qual": {"run_id": "r9"}, "ladder": {"rung": "done", "since": 500.0}}
    owner_other_reservation = ("98:03:9b:00:00:99", _NEW[1])
    for now in (1000.0, 1015.0, 1030.0):
        out = ipmi.occupant_change(row, [_NEW], now, run_owner=lambda r: owner_other_reservation)
        assert "ladder" not in out
        row.update(out)
    assert row["occupant"] == {"serial": _NEW[1], "bmc_mac": _NEW[0], "since": 500.0, "last_read": 1030.0}


_FRU_BLOCKS = ("FRU Device Description : Builtin FRU Device (ID 0)\n"
               " Chassis Serial        : C.M.B60.00X1C.001109201900Z2\n"
               " Product Serial        : WTH49152ZGM1A\n\n"
               "FRU Device Description : HPE Eth 10/25Gb  (ID 86)\n"
               " Product Serial        : 1I02270LSS\n")
_FRU0_ABSENT = ("FRU Device Description : Builtin FRU Device (ID 0)\n"
                " Device not present (Requested sensor, data, or record not found)\n\n"
                "FRU Device Description : HPE Eth 10/25Gb  (ID 86)\n"
                " Product Serial        : 1I02270LSS\n")


def test_ship_serial_is_the_baseboard_block_only():
    assert _serial(_FRU_BLOCKS) == "WTH49152ZGM1A"
    assert _serial(_FRU0_ABSENT) is None
    nic_first = ("FRU Device Description : HPE Eth 10/25Gb  (ID 86)\n Product Serial : 1I02270LSS\n\n"
                 "FRU Device Description : Builtin FRU Device (ID 0)\n Product Serial : WTH1\n")
    assert _serial(nic_first) == "WTH1"
    assert _serial("FRU Device Description : Builtin FRU Device (ID 0)\n Chassis Serial : C.M.B60\n") is None


def test_run_once_fru0_absent_is_not_an_identity_read():
    devs = [{"port": "et24b2", "kind": "bmc", "mac": _NEW[0], "reservation_ip": "10.0.0.2",
             "switch": "rabbit-edam"}]
    writes = []
    ipmi.run_once(devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}],
                  ipmi_runner=lambda ip, u, p, a, timeout=None: _FRU0_ABSENT if a == ["fru", "print", "0"] else "",
                  ping=lambda ip: False, set_state=lambda port, **f: writes.append(f),
                  upsert_node=lambda *a, **k: None,
                  settings={"order_no": None, "population": None, "customer": None}, workers=1,
                  make_redfish=lambda ip: None, prior={"et24b2": {"occupant": _occ(*_OLD)}},
                  run_owner=lambda r: None)
    assert not any("occupant" in f or "occupant_candidate" in f or "ladder" in f for f in writes)


import pathlib as _pl
_FRU_FIX = _pl.Path(__file__).parent / "fixtures" / "fru"


def _fix(name):
    return (_FRU_FIX / name).read_text()


def _runner_for(fru_text, calls=None):
    def run(ip, u, p, args, timeout=None):
        if calls is not None:
            calls.append(list(args))
        if args == ["fru", "print", "0"]:
            return fru_text
        if args and args[0] == "exec":
            return _POWER + _SDR
        return _SEL if args == ["sel", "elist"] else ""
    return run


def test_probe_blade_wistron_three_block_is_the_blade_not_the_nic():
    out = ipmi.probe_blade("10.0.0.1", [{"bmcuser": "u", "bmcpass": "p"}],
                           _runner_for(_fix("wistron_three_block.txt")))
    assert out["serial"] == out["product_serial"] == "WTH49152ZGM1A"
    assert out["fru"]["Board Mfg"] == "Wistron" and "1I02270LSS" not in out["fru"].values()


def test_probe_blade_fru0_absent_rereads_then_serial_is_none_not_redfish():
    class _RF:
        def get_serial(self): return ("SYS-SN", "ok")
        def get_power_state(self): return (None, "")
        def get_power_watts(self): return (None, "")
    calls = []
    out = ipmi.probe_blade("10.0.0.1", [{"bmcuser": "u", "bmcpass": "p"}],
                           _runner_for(_fix("id0_absent_limit_nic_ava.txt"), calls), redfish_client=_RF())
    assert calls.count(["fru", "print", "0"]) == ipmi.FRU0_READS
    assert out["serial"] is None and out["product_serial"] is None and out["fru"] == {}
    assert out["power_on"] == "on"                  # the rest of the pass still ran


def test_probe_blade_no_serial_board_is_none():
    out = ipmi.probe_blade("10.0.0.1", [{"bmcuser": "u", "bmcpass": "p"}],
                           _runner_for(_fix("wistron_tioga_pass_no_serial_et23b2.txt")))
    assert out["serial"] is None and out["fru"]["Board Mfg"] == "Wistron"


def test_probe_blade_leopard_identity_is_the_chassis_serial(monkeypatch):
    from flax_discover.family_map import compile_family_map_text
    monkeypatch.setattr(ipmi._fm_live, "current",
                        lambda path=None: {"leopard": compile_family_map_text("Leopard ORv2-DDR4\n")})
    out = ipmi.probe_blade("10.0.0.1", [{"bmcuser": "u", "bmcpass": "p"}],
                           _runner_for(_fix("leopard_orv2.txt")))
    assert out["serial"] == out["product_serial"] == "QCFF0617260650"


# --- identity gate: observe_state vouches for the blade before we write who it is ---
# Design: CONTINUATION-2026-09-20-process-blade-identity.md, decisions 1-4.
# The bug: a departed BMC keeps its kea reservation but loses its lease, so the
# reservation_ip fallback dials an IP the SUCCESSOR now answers on, and
# hosts[port] (slot-keyed) pairs it with the successor's NIC. The write lands
# under the departed blade -> a ghost dut row.

def _gate_kwargs(devs, recorded, states, nodes, observed):
    """run_once fakes that capture all three write tiers plus the observe view."""

    def runner(ip, u, p, args, timeout=None):
        if args == ["fru", "print", "0"]:
            return _FRU_TEXT
        if args and args[0] == "exec":
            return _POWER + _SDR
        return _SEL if args == ["sel", "elist"] else ""

    return dict(
        devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}],
        ipmi_runner=runner, ping=lambda ip: True,
        set_state=lambda port, **f: states.setdefault(port, {}).update(f),
        upsert_node=lambda mac, **f: nodes.setdefault(mac, {}).update(f),
        settings={"order_no": "RRORD-T1", "population": None, "customer": "acme"},
        orders={"RRORD-T1": {"customer": "acme"}},
        workers=1,
        record_observation=lambda **kw: recorded.append(kw),
        observed=observed,
    )


def _slot(bmc_mac, host_mac):
    """A BMC reservation and a host reservation on the same slot."""
    return [
        {"port": "et1b1", "kind": "bmc", "mac": bmc_mac, "switch": "rabbit-edam",
         "reservation_ip": "10.0.0.1"},
        {"port": "et1b1", "kind": "host", "mac": host_mac, "switch": "rabbit-edam",
         "reservation_ip": "10.0.6.1"},
    ]


def test_stale_reservation_writes_no_identity_but_still_writes_telemetry():
    """observe says another BMC holds the port -> the reservation is a departed
    blade. Probe it if you like, but never attribute the answer to it."""
    from flax_post.observe import ipmi
    recorded, states, nodes = [], {}, {}
    observed = {"et1b1": {"bmc_mac": "bb:99", "nic_mac": "bb:98", "chassis_sn": "SUCCESSOR"}}
    ipmi.run_once(**_gate_kwargs(_slot("aa:01", "aa:00"), recorded, states, nodes, observed))

    assert recorded == [], "a stale reservation must not mint a dut row"
    assert nodes == {}, "a stale reservation must not upsert post_node"
    assert "serial" not in states["et1b1"], "serial is the probe's answer, not the slot's"
    assert states["et1b1"]["sel"], "slot telemetry must still be written"
    assert states["et1b1"]["watts"] == "210 W"


def test_p0_mac_comes_from_observe_not_from_the_slot_lookup():
    """hosts[port] is slot-keyed and follows the CURRENT occupant. Pair the NIC
    by identity instead: observe's nic_mac for the row we just vouched for."""
    from flax_post.observe import ipmi
    recorded, states, nodes = [], {}, {}
    # hosts[port] says cc:00 (a successor's NIC); observe says the vouched blade's is aa:00
    observed = {"et1b1": {"bmc_mac": "aa:01", "nic_mac": "aa:00", "chassis_sn": "SN123"}}
    ipmi.run_once(**_gate_kwargs(_slot("aa:01", "cc:00"), recorded, states, nodes, observed))

    assert len(recorded) == 1
    assert recorded[0]["p0_mac"] == "aa:00", "p0_mac must come from observe, not hosts[port]"
    assert nodes["aa:01"]["host_mac"] == "aa:00"


def test_observe_unknown_port_writes_no_identity():
    """Decision 4 (strict): no observe row for the port -> record nothing about
    who this is. A genuinely new blade stays unrecorded until observe catches up."""
    from flax_post.observe import ipmi
    recorded, states, nodes = [], {}, {}
    ipmi.run_once(**_gate_kwargs(_slot("aa:01", "aa:00"), recorded, states, nodes, observed={}))

    assert recorded == [] and nodes == {}
    assert "serial" not in states["et1b1"]


def test_vouched_blade_is_unaffected():
    """The normal path must be byte-for-byte what it was before the gate."""
    from flax_post.observe import ipmi
    recorded, states, nodes = [], {}, {}
    observed = {"et1b1": {"bmc_mac": "aa:01", "nic_mac": "aa:00", "chassis_sn": "SN123"}}
    ipmi.run_once(**_gate_kwargs(_slot("aa:01", "aa:00"), recorded, states, nodes, observed))

    assert len(recorded) == 1
    kw = recorded[0]
    assert kw["p0_mac"] == "aa:00" and kw["serial"] == "SN123"
    assert kw["keys"] == {"order": "RRORD-T1", "customer": "acme"}
    assert states["et1b1"]["serial"] == "SN123"
    assert nodes["aa:01"]["serial"] == "SN123"


def test_bmc_mac_match_is_case_insensitive():
    """kea and observe both store colon-lowercase, but never gate identity on
    an accident of case."""
    from flax_post.observe import ipmi
    recorded, states, nodes = [], {}, {}
    observed = {"et1b1": {"bmc_mac": "AA:01", "nic_mac": "aa:00", "chassis_sn": "SN123"}}
    ipmi.run_once(**_gate_kwargs(_slot("aa:01", "aa:00"), recorded, states, nodes, observed))
    assert len(recorded) == 1


def test_stale_reservation_contributes_no_occupant_read():
    """apply_occupants already treats an absent read as 'did not answer'. A
    reservation observe won't vouch for must look the same, or a stale mac could
    trigger or confirm an occupant change (spec 2026-09-14 §3)."""
    from flax_post.observe import ipmi
    observed = {"et1b1": {"bmc_mac": "bb:99", "nic_mac": "bb:98", "chassis_sn": "X"}}

    def runner(ip, u, p, args, timeout=None):
        return _FRU_TEXT if args == ["fru", "print", "0"] else ""

    port, mac, serial = ipmi._process_blade(
        {"port": "et1b1", "kind": "bmc", "mac": "aa:01", "reservation_ip": "10.0.0.1"},
        {}, [{"bmcuser": "u", "bmcpass": "p"}], runner, lambda ip: False,
        lambda *a, **k: None, lambda *a, **k: None, None,
        observed=observed)
    assert (port, mac) == ("et1b1", "aa:01")
    assert serial is None, "an unvouched read must not reach the occupant decision"


def test_observe_row_without_nic_mac_writes_no_identity():
    """46 of 48 rows carry nic_mac; the 2 that don't cannot key a dut row."""
    from flax_post.observe import ipmi
    recorded, states, nodes = [], {}, {}
    observed = {"et1b1": {"bmc_mac": "aa:01", "nic_mac": None, "chassis_sn": "SN123"}}
    ipmi.run_once(**_gate_kwargs(_slot("aa:01", "aa:00"), recorded, states, nodes, observed))
    assert recorded == [], "no trusted p0_mac means no dut key"


_PC = [{"bmcuser": "USERID", "bmcpass": "a"}, {"bmcuser": "root", "bmcpass": "b"},
       {"bmcuser": "admin", "bmcpass": "c"}]


def test_configure_pairing_and_default_runner_cipher(monkeypatch):
    import subprocess
    from flax_post.observe import ipmi
    assert ipmi.configure_pairing(_PC) is True
    calls = []
    monkeypatch.setattr(ipmi.subprocess, "run", lambda argv, **k: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0, stdout=b"Chassis Power is on\n"))
    ipmi._default_ipmi_runner("1.2.3.4", "root", "b", ["power", "status"])
    ipmi._default_ipmi_runner("1.2.3.4", "USERID", "a", ["power", "status"])
    assert calls[0][:3] == ["ipmitool", "-C", "17"] and calls[1][:3] == ["ipmitool", "-C", "3"]
    assert len(calls) == 2


def test_configure_pairing_reordered_is_off(caplog):
    from flax_post.observe import ipmi
    with caplog.at_level("ERROR"):
        assert ipmi.configure_pairing([_PC[1], _PC[0]]) is False
    assert ipmi._PAIRING == {}
    assert any("ipmi_login_order_mismatch" in r.message for r in caplog.records)
    ipmi.configure_pairing(_PC)


def test_attempts_tries_root_then_userid_then_memoises():
    """Finding 4 (fix wave): _login_order is dead code post-e878358 --
    _attempts is what every production caller (bmc_data_check, probe_blade,
    probe_power) actually walks. Same coverage, through the live function:
    no memo -> paired root (-C 17) first, then paired USERID (-C 3); once a
    (ip, cred) pair is remembered, its user leads the next call's attempts."""
    from flax_post.observe import ipmi
    ipmi.configure_pairing(_PC)
    ipmi._forget("1.2.3.4")
    attempts = ipmi._attempts("1.2.3.4", _PC)
    assert attempts[0][0]["bmcuser"] == "root" and attempts[0][1] == 17
    assert attempts[1][0]["bmcuser"] == "USERID" and attempts[1][1] == 3
    ipmi._remember("1.2.3.4", _PC[0])
    assert ipmi._attempts("1.2.3.4", _PC)[0][0]["bmcuser"] == "USERID"


def test_probe_power_second_read_uses_only_the_working_login():
    from flax_post.observe import ipmi
    ipmi.configure_pairing(_PC)
    ipmi._forget("9.9.9.9")
    users = []

    def runner(ip, u, p, args):
        users.append(u)
        if u != "USERID":
            raise RuntimeError("auth")
        return "Chassis Power is on\n"
    assert ipmi.probe_power("9.9.9.9", _PC, runner) is not None
    users.clear()
    ipmi.probe_power("9.9.9.9", _PC, runner)
    assert users == ["USERID"]


def test_probe_power_failure_clears_the_memo():
    from flax_post.observe import ipmi
    ipmi.configure_pairing(_PC)
    ipmi._remember("8.8.8.8", _PC[0])

    def dead(ip, u, p, args):
        raise RuntimeError("down")
    assert ipmi.probe_power("8.8.8.8", _PC, dead) is None
    assert ipmi._attempts("8.8.8.8", _PC)[0][0]["bmcuser"] == "root"


# --- Fix round 1 (2026-09-28): spec 2026-09-28-observe-bmc-load §3.5 wins over
# the brief's snippet -- attempts must be (root,17), then (USERID,3), then
# today's full walk over EVERY credential in file order with 3-then-auto, or a
# BMC needing another combination (e.g. root only via auto-negotiate) goes
# IPMI-dark once both paired attempts fail.

def test_configure_pairing_empty_creds_logs_warning_not_error(caplog):
    """Minor #2: an absent/empty creds file is the ordinary not-configured-yet
    case, not a data mismatch -- WARNING, not ERROR."""
    with caplog.at_level("WARNING"):
        assert ipmi.configure_pairing([]) is False
    assert ipmi._PAIRING == {}
    assert any("no BMC credentials; pairing off" in r.message for r in caplog.records)
    assert not any(r.levelname == "ERROR" for r in caplog.records)


def test_attempts_pairing_on_tries_paired_pair_then_full_walk():
    """Important #1: (root,17), (USERID,3), then the full file in order with
    cipher=None (today's plain 3-then-auto walk)."""
    ipmi.configure_pairing(_PC)
    ipmi._forget("3.3.3.3")
    got = ipmi._attempts("3.3.3.3", _PC)
    assert [(c["bmcuser"], cipher) for c, cipher in got] == [
        ("root", 17), ("USERID", 3),
        ("USERID", None), ("root", None), ("admin", None),
    ]


def test_attempts_pairing_off_walks_file_order_ignoring_memo():
    """Minor #3: with pairing off, a memo (however it got there) must not
    reorder the walk -- spec: "today's walk for every read"."""
    assert ipmi.configure_pairing([{"bmcuser": "x", "bmcpass": "p"}]) is False
    ipmi._remember("5.5.5.5", _PC[2])   # a stale memo, e.g. from before a restart
    got = ipmi._attempts("5.5.5.5", _PC)
    assert [c["bmcuser"] for c, _ in got] == ["USERID", "root", "admin"]
    assert all(cipher is None for _, cipher in got)


def test_probe_power_root_only_via_full_walk_still_reads():
    """Required test: a fake BMC that answers ONLY root, and only over the
    plain 3-then-auto path (the paired -C 17 attempt must fail) -- it must
    still be read via the fallback full walk."""
    ipmi.configure_pairing(_PC)
    ipmi._forget("7.7.7.7")

    def runner(ip, u, p, args):
        forced = getattr(ipmi._cipher_override, "value", "absent")
        if u == "root" and forced is None:
            return "Chassis Power is on\n"
        raise RuntimeError("auth")
    assert ipmi.probe_power("7.7.7.7", _PC, runner) == "on"


def test_probe_power_remembers_the_full_walk_pair_first():
    """...and the next read must try that exact (user, cipher) pair first,
    not the paired -C 17 attempt again."""
    ipmi.configure_pairing(_PC)
    ipmi._forget("7.7.7.7")
    calls = []

    def runner(ip, u, p, args):
        forced = getattr(ipmi._cipher_override, "value", "absent")
        calls.append((u, forced))
        if u == "root" and forced is None:
            return "Chassis Power is on\n"
        raise RuntimeError("auth")
    ipmi.probe_power("7.7.7.7", _PC, runner)   # warms the memo: (root, None)
    calls.clear()
    assert ipmi.probe_power("7.7.7.7", _PC, runner) == "on"
    assert calls == [("root", None)]


# ---- ship checks (spec 2026-10-29): a reset must not keep the last run's checks ----
def test_clear_fields_off_to_on_clears_checks():
    row = dict(_LATCHED, checks={"tpm-state": {"status": "fail", "reason": "no TPM device"}})
    assert ipmi.clear_fields_for(row, "aa:bb", "on")["checks"] == {}


def test_occupant_reset_clears_checks():
    row = {"checks": {"tpm-state": {"status": "fail"}}, "ladder": {"since": 1.0}}
    out = ipmi._occupant_reset_fields(row, {"last_read": 5.0}, "SN2", "cc:dd", 10.0)
    assert out["checks"] == {}


# --- effective (per-node) order, 2026-11-06 ---------------------------------
def _two_blades():
    return [{"port": "et7b1", "kind": "bmc", "mac": "aa:71", "lease_ip": "172.24.7.101",
             "reservation_ip": "172.24.7.101", "switch": "rabbit-edam"},
            {"port": "et7b1", "kind": "host", "mac": "aa:70", "reservation_ip": "172.24.7.1",
             "switch": "rabbit-edam"},
            {"port": "et8b1", "kind": "bmc", "mac": "aa:81", "lease_ip": "172.24.8.101",
             "reservation_ip": "172.24.8.101", "switch": "rabbit-edam"},
            {"port": "et8b1", "kind": "host", "mac": "aa:80", "reservation_ip": "172.24.8.1",
             "switch": "rabbit-edam"}]


def test_assigned_node_stamped_with_its_order_not_header():
    states, nodes, recorded = {}, {}, []
    devs = _two_blades()
    ipmi.run_once(
        devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}], ipmi_runner=_runner,
        ping=lambda ip: True, workers=1,
        set_state=lambda port, **f: states.setdefault(port, {}).update(f),
        upsert_node=lambda mac, **f: nodes.setdefault(mac, {}).update(f),
        record_observation=lambda **kw: recorded.append(kw),
        settings={"order_no": "RRORD-100", "population": None, "customer": "X"},
        observed=_vouch(devs),
        nodes={"aa:71": {"assigned_order_no": "RRORD-1033", "assigned_customer": "stale",
                         "assigned_serial": "TPN12345678"}},
        orders={"RRORD-1033": {"customer": "Acme"}, "RRORD-100": {"customer": "X"}})
    assert states["et7b1"]["order_no"] == "RRORD-1033" and states["et8b1"]["order_no"] == "RRORD-100"
    assert nodes["aa:71"]["order_no"] == "RRORD-1033" and nodes["aa:81"]["order_no"] == "RRORD-100"
    keys = {r["p0_mac"]: r["keys"] for r in recorded}
    assert keys["aa:70"] == {"order": "RRORD-1033", "customer": "Acme"}


def _stamp(orders, settings_customer="X"):
    recorded = []
    devs = _two_blades()
    ipmi.run_once(
        devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}], ipmi_runner=_runner,
        ping=lambda ip: True, workers=1,
        set_state=lambda port, **f: None, upsert_node=lambda mac, **f: None,
        record_observation=lambda **kw: recorded.append(kw),
        settings={"order_no": "RRORD-100", "population": None, "customer": settings_customer},
        observed=_vouch(devs),
        nodes={"aa:71": {"assigned_order_no": "RRORD-1033", "assigned_serial": "TPN12345678"}},
        orders=orders)
    return {r["p0_mac"]: r["keys"] for r in recorded}


def test_header_node_stamped_with_header_orders_customer():
    keys = _stamp({"RRORD-100": {"customer": "OVH"}, "RRORD-1033": {"customer": "Barivia"}},
                  settings_customer="hdr-stale")
    assert keys["aa:80"] == {"order": "RRORD-100", "customer": "OVH"}


def test_orders_unreadable_stamps_no_customer():
    keys = _stamp({})
    assert keys["aa:70"] == {"order": "RRORD-1033"}
    assert keys["aa:80"] == {"order": "RRORD-100"}


def test_unvouched_pass_does_not_stamp_order():
    states = {}
    devs = _two_blades()[:2]
    ipmi.run_once(
        devices=devs, creds=[{"bmcuser": "u", "bmcpass": "p"}], ipmi_runner=_runner,
        ping=lambda ip: True, workers=1,
        set_state=lambda port, **f: states.setdefault(port, {}).update(f),
        upsert_node=lambda mac, **f: None,
        settings={"order_no": "RRORD-100", "population": None}, observed={}, nodes={})
    assert states["et7b1"]["order_no"] is None
