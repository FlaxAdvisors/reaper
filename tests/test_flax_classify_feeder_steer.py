"""Tests for feeder steering: guarded desired_vid + no-steer skip (Task CL2),
and the registry `resolve` seam (Task 4)."""
import json

from flax_classify.feeder import derive_targets
from flax_classify.role_registry import load_role_dir, validate_roles, resolve_role


def _sf(mask, access_vid):
    return {"rabbit-fiesta": {"ports": {"Ethernet6/1": {"mask": mask,
                                                        "access_vid": access_vid}}}}


def _registry_resolve(tmp_path):
    """Build a resolve() closure from a real roles.d whose universe is the
    parity equivalent of geom_tokens={"rabbit-fiesta": {"et6/1"}}: Ethernet6/1 -> triage (via
    ports_from geometry.json), everything else -> post (catch_all). Mirrors
    Task 2/3's fixture registry shape (see test_flax_classify_role_registry.py)."""
    geom_file = tmp_path / "geometry.json"
    geom_file.write_text(json.dumps([{"port": "Ethernet6/1", "switch": "rabbit-fiesta"}]))
    d = tmp_path / "roles.d"
    d.mkdir()
    (d / "triage.json").write_text(json.dumps({
        "role": "triage", "universe": {"ports_from": str(geom_file)}}))
    (d / "post.json").write_text(json.dumps({
        "role": "post", "universe": {"catch_all": True}}))
    defs = load_role_dir(str(d))
    validate_roles(defs)
    return lambda sw, p: resolve_role(defs, sw, p)


def test_known_family_target_carries_steered_vid():
    observe = [{"switch": "rabbit-fiesta", "port": "et6b1",
                "resolved": {"bmc_mac": "aa:bb:cc:00:00:01"}}]
    devices = [{"mac": "aa:bb:cc:00:00:01", "switch": "rabbit-fiesta",
                "port": "et6b1", "kind": "bmc", "latched": {"family": "leopard"}}]
    targets = derive_targets(observe, _sf("access", 17), devices,
                             fp_to_vid={("leopard", "triage"): 16},
                             geom_tokens={"rabbit-fiesta": {"et6/1"}}, no_steer=set())
    bmc = next(t for t in targets if t["kind"] == "bmc")
    assert bmc["vid"] == 16  # steered to leopard/triage, not the current 17


def test_no_steer_port_yields_no_targets():
    observe = [{"switch": "rabbit-fiesta", "port": "et6b1",
                "resolved": {"bmc_mac": "aa:bb:cc:00:00:01"}}]
    devices = [{"mac": "aa:bb:cc:00:00:01", "switch": "rabbit-fiesta",
                "port": "et6b1", "kind": "bmc", "latched": {"family": "leopard"}}]
    targets = derive_targets(observe, _sf("access", 31), devices,
                             fp_to_vid={("leopard", "triage"): 16},
                             geom_tokens={},
                             no_steer={("rabbit-fiesta", "Ethernet6/1")})
    assert targets == []  # excluded uplink -> no reservation, no desired_port


def test_resolve_none_is_byte_identical_to_legacy():
    """Passing resolve=None explicitly must produce the exact same output as
    the default (no resolve kwarg at all) -- the registry seam must not
    change behaviour when absent."""
    observe = [{"switch": "rabbit-fiesta", "port": "et6b1",
                "resolved": {"bmc_mac": "aa:bb:cc:00:00:01"}}]
    devices = [{"mac": "aa:bb:cc:00:00:01", "switch": "rabbit-fiesta",
                "port": "et6b1", "kind": "bmc", "latched": {"family": "leopard"}}]
    kwargs = dict(fp_to_vid={("leopard", "triage"): 16},
                 geom_tokens={"rabbit-fiesta": {"et6/1"}}, no_steer=set())
    default = derive_targets(observe, _sf("access", 17), devices, **kwargs)
    explicit_none = derive_targets(observe, _sf("access", 17), devices,
                                   resolve=None, **kwargs)
    assert default == explicit_none
    assert default[0]["vid"] == 16  # steered via legacy phase_for


def test_registry_resolve_matches_legacy_phase_for(tmp_path):
    """Same fixture rows, resolved once via legacy phase_for (resolve=None)
    and once via a Task 2/3 fixture registry's resolve_role -- identical
    derive_targets output."""
    observe = [{"switch": "rabbit-fiesta", "port": "et6b1",
                "resolved": {"bmc_mac": "aa:bb:cc:00:00:01"}}]
    devices = [{"mac": "aa:bb:cc:00:00:01", "switch": "rabbit-fiesta",
                "port": "et6b1", "kind": "bmc", "latched": {"family": "leopard"}}]
    kwargs = dict(fp_to_vid={("leopard", "triage"): 16},
                 geom_tokens={"rabbit-fiesta": {"et6/1"}}, no_steer=set())
    legacy = derive_targets(observe, _sf("access", 17), devices,
                            resolve=None, **kwargs)
    resolve = _registry_resolve(tmp_path)
    registry = derive_targets(observe, _sf("access", 17), devices,
                              resolve=resolve, **kwargs)
    assert legacy == registry
    assert registry[0]["vid"] == 16


def test_targets_carry_resolved_phase_triage():
    """Every emitted target carries the resolved phase, so the triage cycle
    can scope its owner=triage reservation write. Legacy geometry path: a
    geometry-member port resolves to triage."""
    observe = [{"switch": "rabbit-fiesta", "port": "et6b1",
                "resolved": {"bmc_mac": "aa:bb:cc:00:00:01"}}]
    devices = [{"mac": "aa:bb:cc:00:00:01", "switch": "rabbit-fiesta",
                "port": "et6b1", "kind": "bmc", "latched": {"family": "leopard"}}]
    targets = derive_targets(observe, _sf("access", 17), devices,
                             fp_to_vid={("leopard", "triage"): 16},
                             geom_tokens={"rabbit-fiesta": {"et6/1"}}, no_steer=set())
    assert targets and all(t["phase"] == "triage" for t in targets)


def test_post_resolved_port_still_emits_targets_tagged_post():
    """A port that resolves to POST must STILL emit targets (with the post
    steered vid) so flax_reconcile keeps steering it -- but each target is
    tagged phase='post' so the triage cycle can skip the reservation write.
    This is the rabbit-edam et10 leak: switch+port resolves to post, the vid
    is the post vid, but ownership must NOT be triage's."""
    observe = [{"switch": "rabbit-edam", "port": "et10b1",
                "resolved": {"bmc_mac": "98:03:9b:a8:25:72",
                             "nic_mac": "98:03:9b:a8:25:70"}}]
    sf = {"rabbit-edam": {"ports": {"Ethernet10/1": {"access_vid": 25}}}}
    targets = derive_targets(observe, sf, [],
                             fp_to_vid={}, geom_tokens={}, no_steer=set(),
                             resolve=lambda sw, p: "post")
    macs = {t["mac"] for t in targets}
    assert macs == {"98:03:9b:a8:25:72", "98:03:9b:a8:25:70"}  # steering preserved
    assert all(t["phase"] == "post" for t in targets)
    assert all(t["vid"] == 25 for t in targets)


def test_resolve_returning_none_skips_target_like_no_steer():
    """resolve(sw, port) -> None (unassigned, no catch_all) must skip the
    target exactly like a no-steer port -- no reservation, no desired_port."""
    observe = [{"switch": "rabbit-fiesta", "port": "et6b1",
                "resolved": {"bmc_mac": "aa:bb:cc:00:00:01"}}]
    devices = [{"mac": "aa:bb:cc:00:00:01", "switch": "rabbit-fiesta",
                "port": "et6b1", "kind": "bmc", "latched": {"family": "leopard"}}]
    targets = derive_targets(observe, _sf("access", 17), devices,
                             fp_to_vid={("leopard", "triage"): 16},
                             geom_tokens={"rabbit-fiesta": {"et6/1"}}, no_steer=set(),
                             resolve=lambda sw, p: None)
    assert targets == []
