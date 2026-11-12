"""Tests for flax_classify.feeder.derive_targets."""


def test_derive_targets_emits_bmc_and_host_per_port():
    """observe_state row with both bmc_mac + nic_mac -> two targets."""
    from flax_classify.feeder import derive_targets

    observe_rows = [{
        "switch": "rabbit-lorax", "port": "et6b1",
        "resolved": {
            "bmc_mac": "1c:34:da:7f:b3:a4",
            "nic_mac": "1c:34:da:7f:b3:a2",
        },
    }]
    switch_facts = {"rabbit-lorax": {"ports": {
        "Ethernet6/1": {"access_vid": 17}}}}
    out = derive_targets(observe_rows, switch_facts, [], fp_to_vid={}, geom_tokens={}, no_steer=set())
    macs = {(t["mac"], t["kind"]) for t in out}
    assert macs == {
        ("1c:34:da:7f:b3:a4", "bmc"),
        ("1c:34:da:7f:b3:a2", "host"),
    }
    for t in out:
        assert t["switch"] == "rabbit-lorax"
        assert t["port"] == "et6b1"  # internal form preserved
        assert t["vid"] == 17


def test_derive_targets_skips_when_no_bmc_mac():
    """No bmc_mac resolved -> skip the entire row (port not enrolled)."""
    from flax_classify.feeder import derive_targets

    rows = [{"switch": "rabbit-lorax", "port": "et6b3",
             "resolved": {"bmc_mac": None, "nic_mac": None}}]
    facts = {"rabbit-lorax": {"ports": {
        "Ethernet6/3": {"access_vid": 17}}}}
    assert derive_targets(rows, facts, [], fp_to_vid={}, geom_tokens={}, no_steer=set()) == []


def test_derive_targets_emits_bmc_only_when_no_nic_mac():
    from flax_classify.feeder import derive_targets

    rows = [{"switch": "rabbit-lorax", "port": "et7b1",
             "resolved": {"bmc_mac": "aa:bb:cc:dd:ee:ff", "nic_mac": None}}]
    facts = {"rabbit-lorax": {"ports": {
        "Ethernet7/1": {"access_vid": 18}}}}
    out = derive_targets(rows, facts, [], fp_to_vid={}, geom_tokens={}, no_steer=set())
    assert len(out) == 1
    assert out[0]["mac"] == "aa:bb:cc:dd:ee:ff"
    assert out[0]["kind"] == "bmc"
    assert out[0]["vid"] == 18


def test_derive_targets_skips_when_port_is_trunk_or_no_vid():
    """No access_vid -> skip (trunks aren't reservable; we cannot derive IP
    from a multi-vlan port)."""
    from flax_classify.feeder import derive_targets

    rows = [{"switch": "rabbit-lorax", "port": "et1b1",
             "resolved": {"bmc_mac": "aa:bb:cc:dd:ee:ff"}}]
    facts = {"rabbit-lorax": {"ports": {"Ethernet1/1": {}}}}  # no access_vid
    assert derive_targets(rows, facts, [], fp_to_vid={}, geom_tokens={}, no_steer=set()) == []


def test_derive_targets_skips_when_switch_missing_from_facts():
    """observe_state row references a switch flax-switch-sense isn't
    polling -> skip (no vid available)."""
    from flax_classify.feeder import derive_targets

    rows = [{"switch": "rabbit-mystery", "port": "et6b1",
             "resolved": {"bmc_mac": "aa:bb:cc:dd:ee:ff"}}]
    facts = {"rabbit-lorax": {"ports": {}}}
    assert derive_targets(rows, facts, [], fp_to_vid={}, geom_tokens={}, no_steer=set()) == []


def test_family_joined_from_devices():
    from flax_classify.feeder import derive_targets
    observe = [{"switch": "rabbit-x", "port": "et6b1",
                "resolved": {"bmc_mac": "aa:bb:cc:00:00:64",
                             "nic_mac": "aa:bb:cc:00:00:62"}}]
    facts = {"rabbit-x": {"ports": {"Ethernet6/1": {"access_vid": 16}}}}
    devices = [{"mac": "aa:bb:cc:00:00:64", "kind": "bmc",
                "switch": "rabbit-x", "port": "et6b1",
                "latched": {"family": "leopard"}},
               {"mac": "aa:bb:cc:00:00:62", "kind": "host",
                "switch": "rabbit-x", "port": "et6b1",
                "latched": {"family": "leopard"}}]
    targets = derive_targets(observe, facts, devices, fp_to_vid={}, geom_tokens={}, no_steer=set())
    fams = {t["mac"]: t["family"] for t in targets}
    assert fams["aa:bb:cc:00:00:64"] == "leopard"
    assert fams["aa:bb:cc:00:00:62"] == "leopard"


def test_family_defaults_unknown_when_no_device_row():
    from flax_classify.feeder import derive_targets
    observe = [{"switch": "rabbit-x", "port": "et6b1",
                "resolved": {"bmc_mac": "aa:bb:cc:00:00:64"}}]
    facts = {"rabbit-x": {"ports": {"Ethernet6/1": {"access_vid": 16}}}}
    targets = derive_targets(observe, facts, devices=[], fp_to_vid={}, geom_tokens={}, no_steer=set())
    assert targets[0]["family"] == "unknown"


def test_vm_targets_emitted_from_devices():
    from flax_classify.feeder import derive_targets
    observe = [{"switch": "rabbit-x", "port": "et6b1",
                "resolved": {"bmc_mac": "aa:bb:cc:00:00:64"}}]
    facts = {"rabbit-x": {"ports": {"Ethernet6/1": {"access_vid": 16}}}}
    devices = [{"mac": "02:00:00:00:00:01", "kind": "vm",
                "switch": "rabbit-x", "port": "et6b1",
                "latched": {"family": "leopard", "vm_n": 1}}]
    # geom_tokens empty -> phase "post"; steer leopard/post -> vid 16.
    targets = derive_targets(observe, facts, devices,
                             fp_to_vid={("leopard", "post"): 16},
                             geom_tokens={}, no_steer=set())
    vm = next(t for t in targets if t["kind"] == "vm")
    assert vm["vm_n"] == 1 and vm["family"] == "leopard" and vm["vid"] == 16


def test_bmc_only_family_suppresses_phantom_host():
    """RJ45-LOM (capri): single physical port = ONE MAC for both BMC + NIC.
    flax-observe still synthesizes a phantom nic_mac, but the BMC's family is
    bmc-only -> emit the BMC target ONLY, never the phantom host (it would never
    DHCP -> permanent lease!=reservation flap)."""
    from flax_classify.feeder import derive_targets
    observe = [{"switch": "rabbit-x", "port": "et6b1",
                "resolved": {"bmc_mac": "aa:bb:cc:00:00:64",
                             "nic_mac": "aa:bb:cc:00:00:62"}}]
    facts = {"rabbit-x": {"ports": {"Ethernet6/1": {"access_vid": 16}}}}
    devices = [{"mac": "aa:bb:cc:00:00:64", "kind": "bmc",
                "switch": "rabbit-x", "port": "et6b1",
                "latched": {"family": "capri"}}]
    targets = derive_targets(observe, facts, devices, fp_to_vid={},
                             geom_tokens={}, no_steer=set(),
                             bmc_only={"capri"})
    kinds = {(t["mac"], t["kind"]) for t in targets}
    assert kinds == {("aa:bb:cc:00:00:64", "bmc")}


def test_non_bmc_only_family_still_emits_host_and_bmc():
    """A normal dual-MAC / non-bmc-only family (leopard) is unaffected by the
    bmc-only set: both host + bmc targets still emitted."""
    from flax_classify.feeder import derive_targets
    observe = [{"switch": "rabbit-x", "port": "et6b1",
                "resolved": {"bmc_mac": "aa:bb:cc:00:00:64",
                             "nic_mac": "aa:bb:cc:00:00:62"}}]
    facts = {"rabbit-x": {"ports": {"Ethernet6/1": {"access_vid": 16}}}}
    devices = [{"mac": "aa:bb:cc:00:00:64", "kind": "bmc",
                "switch": "rabbit-x", "port": "et6b1",
                "latched": {"family": "leopard"}},
               {"mac": "aa:bb:cc:00:00:62", "kind": "host",
                "switch": "rabbit-x", "port": "et6b1",
                "latched": {"family": "leopard"}}]
    targets = derive_targets(observe, facts, devices, fp_to_vid={},
                             geom_tokens={}, no_steer=set(),
                             bmc_only={"capri"})
    kinds = {(t["mac"], t["kind"]) for t in targets}
    assert kinds == {("aa:bb:cc:00:00:64", "bmc"),
                     ("aa:bb:cc:00:00:62", "host")}


def test_wedge_sonic_nic_mac_emits_host_via_normal_path():
    """wedge100s port: macmath/distinct_oui already classified the foreign-OUI
    SONiC mgmt mac as the nic upstream, so observe_state.nic_mac IS the SONiC
    mac. derive_targets emits a HOST target for it via the NORMAL nic_mac path
    (no SAMEPORT junk-promotion needed); the BMC stays bmc."""
    from flax_classify.feeder import derive_targets
    bmc = "3c:2c:99:58:9d:02"
    sonic = "00:90:fb:61:da:a0"
    observe = [{"switch": "rabbit-hare", "port": "et23b1",
                "resolved": {"bmc_mac": bmc, "nic_mac": sonic}}]
    facts = {"rabbit-hare": {"ports": {"Ethernet23/1": {
        "access_vid": 26}}}}
    devices = [{"mac": bmc, "kind": "bmc", "switch": "rabbit-hare",
                "port": "et23b1", "latched": {"family": "wedge100s"}},
               {"mac": sonic, "kind": "host", "switch": "rabbit-hare",
                "port": "et23b1", "latched": {"family": "wedge100s"}}]
    targets = derive_targets(observe, facts, devices,
                             fp_to_vid={("wedge100s", "post"): 26},
                             geom_tokens={}, no_steer=set())
    kinds = {(t["mac"], t["kind"]) for t in targets}
    assert kinds == {(bmc, "bmc"), (sonic, "host")}
    host = next(t for t in targets if t["kind"] == "host")
    assert host["mac"] == sonic
    assert host["vid"] == 26
    assert host["family"] == "wedge100s"


def test_wedge_sonic_host_ip_is_port_host_slot_not_bmc():
    """End-to-end via the normal nic_mac path: the wedge SONiC host gets
    172.26.0.<N> while the BMC gets 172.26.0.<N+100> on the same swp/vid."""
    from flax_classify.feeder import derive_targets
    from flax_classify.formula import classify_one
    bmc = "3c:2c:99:58:9d:02"
    sonic = "00:90:fb:61:da:a0"
    # The wedge100s BMC + SONiC mgmt land on TURTLE swp23 (vid 26).
    observe = [{"switch": "turtle-1", "port": "swp23",
                "resolved": {"bmc_mac": bmc, "nic_mac": sonic}}]
    facts = {"turtle-1": {"ports": {"swp23": {"access_vid": 26}}}}
    devices = [{"mac": bmc, "kind": "bmc", "switch": "turtle-1",
                "port": "swp23", "latched": {"family": "wedge100s"}},
               {"mac": sonic, "kind": "host", "switch": "turtle-1",
                "port": "swp23", "latched": {"family": "wedge100s"}}]
    # turtle -> phase "post"; steer wedge100s/post to vid 26 (braintree).
    targets = derive_targets(observe, facts, devices,
                             fp_to_vid={("wedge100s", "post"): 26},
                             geom_tokens={}, no_steer=set())
    ips = {}
    for t in targets:
        r = classify_one("turtle-1", "swp23", t["mac"], t["kind"],
                         t["vid"], family=t.get("family", "unknown"))
        ips[t["kind"]] = r["ipv4_address"]
    assert ips["host"] == "172.26.0.23"
    assert ips["bmc"] == "172.26.0.123"


def test_wedge_bmc_only_no_nic_mac_no_phantom_host():
    """wedge100s port with ONLY the BMC (nic_mac None, e.g. macmath found no
    foreign-OUI mac) -> no phantom host target."""
    from flax_classify.feeder import derive_targets
    bmc = "3c:2c:99:58:9d:02"
    observe = [{"switch": "rabbit-hare", "port": "et23b1",
                "resolved": {"bmc_mac": bmc, "nic_mac": None}}]
    facts = {"rabbit-hare": {"ports": {"Ethernet23/1": {
        "access_vid": 26}}}}
    devices = [{"mac": bmc, "kind": "bmc", "switch": "rabbit-hare",
                "port": "et23b1", "latched": {"family": "wedge100s"}}]
    targets = derive_targets(observe, facts, devices, fp_to_vid={},
                             geom_tokens={}, no_steer=set())
    kinds = {(t["mac"], t["kind"]) for t in targets}
    assert kinds == {(bmc, "bmc")}


def test_load_bmc_only_families_absent_file(tmp_path):
    from flax_classify.vlan_policy import load_bmc_only_families
    assert load_bmc_only_families(str(tmp_path / "nope.json")) == set()


def test_load_bmc_only_families_dir_path(tmp_path):
    """A docker bind-mount of a missing path creates an empty dir -> tolerate."""
    from flax_classify.vlan_policy import load_bmc_only_families
    d = tmp_path / "bmc-only-families.json"
    d.mkdir()
    assert load_bmc_only_families(str(d)) == set()


def test_load_bmc_only_families_valid_list(tmp_path):
    from flax_classify.vlan_policy import load_bmc_only_families
    p = tmp_path / "bmc-only-families.json"
    p.write_text('["Capri", "leopard"]')
    # normalized (lowercased/stripped) for robust comparison.
    assert load_bmc_only_families(str(p)) == {"capri", "leopard"}
