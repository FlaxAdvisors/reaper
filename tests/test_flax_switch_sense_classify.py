"""Tests for flax_switch_sense.classify (lifted from switchportrecond)."""


def test_classify_single_mac_returns_bmc_and_synthesized_nic():
    from flax_switch_sense.classify import classify_macs, ClassifiedMacs
    result = classify_macs(["98:03:9b:a6:fc:24"])
    assert isinstance(result, ClassifiedMacs)
    assert result.bmc == "98:03:9b:a6:fc:24"
    assert result.nics == ["98:03:9b:a6:fc:22"]   # bmc - 2
    assert result.junk == []
    assert result.classification_source == "single_mac"
    assert result.lldp_disagreement is False


def test_classify_empty_macs_returns_none():
    from flax_switch_sense.classify import classify_macs
    result = classify_macs([])
    assert result.bmc is None
    assert result.nics == []
    assert result.junk == []


def test_classify_normalizes_format_inputs():
    from flax_switch_sense.classify import classify_macs
    result = classify_macs(["98-03-9B-A6-FC-24", "9803.9bA6.fc22"])
    assert result.bmc == "98:03:9b:a6:fc:24"
    assert "98:03:9b:a6:fc:22" in result.nics


def test_classify_lldp_single_bmc_with_paired_nic():
    from flax_switch_sense.classify import classify_macs
    macs = ["98:03:9b:a6:fc:24", "98:03:9b:a6:fc:22"]
    lldp = [{"mac": "98:03:9b:a6:fc:24", "sysname": "tiogapass"}]
    result = classify_macs(macs, lldp)
    assert result.bmc == "98:03:9b:a6:fc:24"
    assert result.nics == ["98:03:9b:a6:fc:22"]
    assert result.junk == []
    assert result.classification_source == "lldp"
    assert result.lldp_disagreement is False


def test_classify_lldp_single_bmc_with_junk_mac():
    from flax_switch_sense.classify import classify_macs
    macs = ["98:03:9b:a6:fc:24", "98:03:9b:a7:00:be"]   # phantom MAC
    lldp = [{"mac": "98:03:9b:a6:fc:24", "sysname": "tiogapass"}]
    result = classify_macs(macs, lldp)
    assert result.bmc == "98:03:9b:a6:fc:24"
    assert result.nics == []
    assert len(result.junk) == 1
    assert result.junk[0]["mac"] == "98:03:9b:a7:00:be"
    assert result.junk[0]["reason"] == "not_paired_with_lldp_bmc"


def test_classify_lldp_disagreement_falls_back_to_numeric():
    from flax_switch_sense.classify import classify_macs
    macs = ["98:03:9b:a6:fc:24", "98:03:9b:a6:fc:30"]   # both LLDP-claimed
    lldp = [
        {"mac": "98:03:9b:a6:fc:24", "sysname": "tiogapass"},
        {"mac": "98:03:9b:a6:fc:30", "sysname": "tiogapass-2"},
    ]
    result = classify_macs(macs, lldp)
    # Numeric-highest wins
    assert result.bmc == "98:03:9b:a6:fc:30"
    assert result.classification_source == "numeric_fallback"
    assert result.lldp_disagreement is True
    # The other LLDP-claimed mac becomes junk
    assert any(j["mac"] == "98:03:9b:a6:fc:24"
               and j["reason"] == "multi_lldp_bmc_observed"
               for j in result.junk)


def test_classify_no_lldp_falls_back_to_numeric_paired():
    from flax_switch_sense.classify import classify_macs
    macs = ["98:03:9b:a6:fc:24", "98:03:9b:a6:fc:22"]
    result = classify_macs(macs, [])
    assert result.bmc == "98:03:9b:a6:fc:24"
    assert result.nics == ["98:03:9b:a6:fc:22"]
    assert result.junk == []
    assert result.classification_source == "numeric_fallback"


def test_classify_no_lldp_unpaired_extra_is_junk():
    from flax_switch_sense.classify import classify_macs
    macs = ["98:03:9b:a6:fc:24", "00:11:22:33:44:55"]
    result = classify_macs(macs, [])
    assert result.bmc == "98:03:9b:a6:fc:24"
    assert result.nics == []
    assert len(result.junk) == 1
    assert result.junk[0]["mac"] == "00:11:22:33:44:55"
    assert result.junk[0]["reason"] == "not_paired_with_numeric_bmc"


def test_classify_sameport_family_multi_nic():
    """brycecanyon-style: one BMC + 4 host NICs at stride 2."""
    from flax_switch_sense.classify import classify_macs
    macs = [
        "98:03:9b:a6:fc:24",   # BMC
        "98:03:9b:a6:fc:22",   # NIC bmc - 2
        "98:03:9b:a6:fc:20",   # NIC bmc - 4
        "98:03:9b:a6:fc:1e",   # NIC bmc - 6
        "98:03:9b:a6:fc:1c",   # NIC bmc - 8
    ]
    result = classify_macs(macs, [])
    assert result.bmc == "98:03:9b:a6:fc:24"
    assert len(result.nics) == 4
    assert result.junk == []


def test_classify_lldp_no_match_treats_as_no_lldp():
    """LLDP advertises a MAC not on this port -> ignored, numeric fallback."""
    from flax_switch_sense.classify import classify_macs
    macs = ["98:03:9b:a6:fc:24", "98:03:9b:a6:fc:22"]
    lldp = [{"mac": "00:00:00:00:00:01"}]   # not in macs
    result = classify_macs(macs, lldp)
    assert result.bmc == "98:03:9b:a6:fc:24"
    assert result.classification_source == "numeric_fallback"
    assert result.lldp_disagreement is False


def test_classify_lldp_host_nic_inverts_to_higher_bmc():
    """Real-world et6b4 pattern: host OS LLDPs from its data NIC, BMC is
    silent. LLDP-matched MAC is the LOWER of a +2 pair -> the higher
    partner is the actual BMC. Without the inversion, the chassis is
    invisible to IPMI (we ping the NIC instead).
    """
    from flax_switch_sense.classify import classify_macs
    # leopard6b4: NIC :e4:06 advertises via LLDP, BMC :e4:08 does not.
    macs = ["98:03:9b:97:e4:06", "98:03:9b:97:e4:08"]
    lldp = [{"mac": "9803.9b97.e406",
             "sysname": "leopard6b4.brt.lab",
             "port_description": "ens1f0np0"}]
    r = classify_macs(macs, lldp)
    assert r.bmc == "98:03:9b:97:e4:08"      # actual BMC (was :06 pre-fix)
    assert r.nics == ["98:03:9b:97:e4:06"]
    assert r.junk == []
    assert r.classification_source == "lldp_nic_inverted"


def test_classify_lldp_host_nic_inverts_with_extra_junk_macs():
    """et6b4 with two virtualbox VMs DHCP-leaking onto the port.
    The +2 inversion still fires; the VM MACs become junk.
    """
    from flax_switch_sense.classify import classify_macs
    macs = [
        "08:00:27:32:48:80",   # VM
        "08:00:27:6f:7f:18",   # VM
        "98:03:9b:97:e4:06",   # NIC (LLDP-matched)
        "98:03:9b:97:e4:08",   # BMC
    ]
    lldp = [{"mac": "98:03:9b:97:e4:06", "sysname": "leopard6b4"}]
    r = classify_macs(macs, lldp)
    assert r.bmc == "98:03:9b:97:e4:08"
    assert r.nics == ["98:03:9b:97:e4:06"]
    assert {j["mac"] for j in r.junk} == {
        "08:00:27:32:48:80", "08:00:27:6f:7f:18"}
    assert all(j["reason"] == "not_paired_with_lldp_bmc" for j in r.junk)
    assert r.classification_source == "lldp_nic_inverted"


def test_classify_lldp_higher_partner_wins_when_multiple():
    """If LLDP-matched MAC has multiple +2k partners observed, the highest
    wins (brycecanyon-shaped case where a mid-rank NIC LLDPs).
    """
    from flax_switch_sense.classify import classify_macs
    # LLDP-matched is :fc:20; partners at :fc:22 (+2) and :fc:24 (+4)
    # both observed -> :fc:24 (highest) is the BMC.
    macs = [
        "98:03:9b:a6:fc:20",   # LLDP-matched (NIC bmc-4)
        "98:03:9b:a6:fc:22",   # NIC bmc-2
        "98:03:9b:a6:fc:24",   # BMC
    ]
    lldp = [{"mac": "98:03:9b:a6:fc:20", "sysname": "brycecanyon-nic"}]
    r = classify_macs(macs, lldp)
    assert r.bmc == "98:03:9b:a6:fc:24"
    assert "98:03:9b:a6:fc:20" in r.nics
    assert "98:03:9b:a6:fc:22" in r.nics
    assert r.junk == []
    assert r.classification_source == "lldp_nic_inverted"


def test_classify_lldp_bmc_with_no_partner_keeps_lldp_source():
    """When LLDP-matched MAC has NO higher partner, no inversion. The
    existing 'lldp' classification stands. Regression guard for et6b1
    (single-NIC chassis where LLDP correctly names the BMC).
    """
    from flax_switch_sense.classify import classify_macs
    macs = ["1c:34:da:7f:b3:a2", "1c:34:da:7f:b3:a4"]
    lldp = [{"mac": "1c:34:da:7f:b3:a4", "sysname": "leopard6b1"}]
    r = classify_macs(macs, lldp)
    assert r.bmc == "1c:34:da:7f:b3:a4"   # unchanged
    assert r.nics == ["1c:34:da:7f:b3:a2"]
    assert r.classification_source == "lldp"


# ---------------------------------------------------------------------------
# macmath parameter tests
# ---------------------------------------------------------------------------

def test_classify_macmath_none_is_backward_compatible_single_mac():
    """macmath=None -> identical to calling without the parameter."""
    from flax_switch_sense.classify import classify_macs
    result_default = classify_macs(["98:03:9b:a6:fc:24"])
    result_explicit = classify_macs(["98:03:9b:a6:fc:24"], macmath=None)
    assert result_default == result_explicit


def test_classify_macmath_offset_leopard_bmc_plus2():
    """offset scheme: P0 and P0+2 observed -> bmc=P0+2, nics=[P0], source=macmath_offset."""
    from flax_switch_sense.classify import classify_macs
    cfg = {"scheme": "offset", "bmc_offset": 2, "host_offsets": [0]}
    macs = ["aa:bb:cc:00:00:00", "aa:bb:cc:00:00:02"]
    r = classify_macs(macs, macmath=cfg)
    assert r.bmc == "aa:bb:cc:00:00:02"
    assert r.nics == ["aa:bb:cc:00:00:00"]
    assert r.junk == []
    assert r.classification_source == "macmath_offset"


def test_classify_macmath_offset_computed_bmc_absent_falls_through_to_legacy():
    """offset scheme: if computed bmc is not observed, fall through to legacy path."""
    from flax_switch_sense.classify import classify_macs
    cfg = {"scheme": "offset", "bmc_offset": 2, "host_offsets": [0]}
    # Only P0 present, not P0+2 -> fallback
    macs = ["aa:bb:cc:00:00:00"]
    r = classify_macs(macs, macmath=cfg)
    # Legacy single_mac path: bmc = the one mac, synthesized nic = bmc - 2
    assert r.classification_source in ("single_mac", "numeric_fallback", "lldp")
    # NOT macmath_offset (because the computed bmc was absent)
    assert r.classification_source != "macmath_offset"


def test_classify_macmath_distinct_oui_wedge():
    """distinct_oui scheme (wedge cfg): bmc=highest, host=foreign-OUI, junk=same-OUI extra."""
    from flax_switch_sense.classify import classify_macs
    cfg = {"scheme": "distinct_oui"}
    macs = [
        "3c:2c:99:58:9d:00",
        "3c:2c:99:58:9d:02",
        "00:90:fb:61:da:a0",
    ]
    r = classify_macs(macs, macmath=cfg)
    assert r.bmc == "3c:2c:99:58:9d:02"
    assert r.nics == ["00:90:fb:61:da:a0"]
    assert r.junk == [{"mac": "3c:2c:99:58:9d:00", "reason": "macmath_same_oui_extra"}]
    assert r.classification_source == "macmath_distinct_oui"


def test_classify_macmath_unknown_scheme_falls_to_legacy():
    """Unknown/missing scheme -> treat as macmath=None (legacy path)."""
    from flax_switch_sense.classify import classify_macs
    cfg = {"scheme": "some_future_scheme"}
    macs = ["98:03:9b:a6:fc:24", "98:03:9b:a6:fc:22"]
    r = classify_macs(macs, macmath=cfg)
    # Legacy numeric_fallback since no LLDP
    assert r.classification_source == "numeric_fallback"
    assert r.bmc == "98:03:9b:a6:fc:24"


def test_classify_macmath_offset_input_normalization():
    """Inputs in non-colon form are normalized before macmath dispatch."""
    from flax_switch_sense.classify import classify_macs
    cfg = {"scheme": "offset", "bmc_offset": 2, "host_offsets": [0]}
    # same macs but in dash + cisco-dot format
    macs = ["aa-bb-cc-00-00-00", "aabb.cc00.0002"]
    r = classify_macs(macs, macmath=cfg)
    assert r.bmc == "aa:bb:cc:00:00:02"
    assert r.nics == ["aa:bb:cc:00:00:00"]
    assert r.classification_source == "macmath_offset"
