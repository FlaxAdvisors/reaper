from flax_observe.role_confirm import confirm_roles, RoleVerdict


def _ev(**kw):
    # kw: mac -> (bmc_kind, host_ok)
    return {m: {"bmc_kind": b, "host_ok": h} for m, (b, h) in kw.items()}


def test_primary_confirmed_bmc_unchanged():
    # healthy node: primary probes openbmc -> confirmed, nic preserved
    v = confirm_roles("aa:aa:aa:aa:aa:02", ["aa:aa:aa:aa:aa:00"],
                      _ev(**{"aa:aa:aa:aa:aa:02": ("openbmc", None)}))
    assert v.bmc_mac == "aa:aa:aa:aa:aa:02"
    assert v.nic_mac == "aa:aa:aa:aa:aa:00"
    assert v.source == "probe_confirmed"
    assert v.multi_bmc == []


def test_traditional_primary_also_confirmed():
    v = confirm_roles("aa:aa:aa:aa:aa:02", ["aa:aa:aa:aa:aa:00"],
                      _ev(**{"aa:aa:aa:aa:aa:02": ("traditional", None)}))
    assert v.bmc_mac == "aa:aa:aa:aa:aa:02"
    assert v.source == "probe_confirmed"


def test_redfish_primary_also_confirmed():
    # A Redfish-identified BMC (braintree TiogaPass AMI) is a first-class
    # confirmed BMC kind -- must not fall through to heuristic/flip.
    v = confirm_roles("aa:aa:aa:aa:aa:02", ["aa:aa:aa:aa:aa:00"],
                      _ev(**{"aa:aa:aa:aa:aa:02": ("redfish", None)}))
    assert v.bmc_mac == "aa:aa:aa:aa:aa:02"
    assert v.source == "probe_confirmed"


def test_capri_flip_primary_is_host_no_bmc():
    # single visible mac: bmc probe unknown, host login ok -> host, no bmc
    mac = "0c:42:a1:7b:e4:2d"
    v = confirm_roles(mac, ["0c:42:a1:7b:e4:2b"],
                      _ev(**{mac: ("unknown", "ok")}))
    assert v.bmc_mac is None
    assert v.nic_mac == mac
    assert v.source == "probe_flip_host"


def test_wedge_promote_other_mac_to_bmc():
    # heuristic picked the SONiC host as bmc; the real OpenBMC is the other mac
    sonic = "00:90:fb:61:da:a0"
    obmc = "3c:2c:99:58:9d:02"
    v = confirm_roles(sonic, [obmc],
                      _ev(**{sonic: ("unknown", "ok"), obmc: ("openbmc", None)}))
    assert v.bmc_mac == obmc
    assert v.nic_mac == sonic
    assert v.source == "probe_promote_bmc"


def test_unconfirmed_primary_kept_when_no_host_signal():
    # transient: bmc probe unknown, host probe fail -> keep heuristic bmc
    v = confirm_roles("aa:aa:aa:aa:aa:02", ["aa:aa:aa:aa:aa:00"],
                      _ev(**{"aa:aa:aa:aa:aa:02": ("unknown", "fail")}))
    assert v.bmc_mac == "aa:aa:aa:aa:aa:02"
    assert v.source == "heuristic"


def test_multi_bmc_fault_surfaced():
    a, b = "aa:aa:aa:aa:aa:02", "bb:bb:bb:bb:bb:02"
    v = confirm_roles(a, [], _ev(**{a: ("openbmc", None), b: ("openbmc", None)}))
    assert set(v.multi_bmc) == {a, b}
    assert v.bmc_mac == a  # primary kept among the confirmed set


def test_no_evidence_falls_back_to_heuristic():
    v = confirm_roles("aa:aa:aa:aa:aa:02", ["aa:aa:aa:aa:aa:00"], {})
    assert v.bmc_mac == "aa:aa:aa:aa:aa:02"
    assert v.nic_mac == "aa:aa:aa:aa:aa:00"
    assert v.source == "heuristic"


def test_primary_none_with_confirmed_host_only():
    mac = "0c:42:a1:7b:e4:2d"
    v = confirm_roles(None, [], _ev(**{mac: ("unknown", "ok")}))
    assert v.bmc_mac is None
    assert v.nic_mac == mac


def test_primary_none_promotes_confirmed_bmc():
    # classifier returned no bmc, but a visible mac probes as OpenBMC ->
    # promote it (the promote-from-None branch).
    obmc = "3c:2c:99:58:9d:02"
    v = confirm_roles(None, [], _ev(**{obmc: ("openbmc", None)}))
    assert v.bmc_mac == obmc
    assert v.source == "probe_promote_bmc"
    assert v.multi_bmc == []
