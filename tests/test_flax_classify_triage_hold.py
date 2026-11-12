"""Part 2 / A: the triage reservation hold (flax_classify.triage_hold)."""
from flax_classify.triage_hold import TriageHold, DEFAULT_HOLD_SECS

BMC = "1c:34:da:7f:b3:a4"
NIC = "1c:34:da:7f:b3:a2"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _row(mac, owner="triage", switch="rabbit-lorax", port="et6b1"):
    return {"mac": mac, "owner_role": owner, "switch": switch, "port": port}


def _obs(bmc=BMC, nic=NIC, sn="SN1", switch="rabbit-lorax", port="et6b1"):
    return {"switch": switch, "port": port,
            "resolved": {"bmc_mac": bmc, "nic_mac": nic, "chassis_sn": sn}}


def test_default_is_900():
    assert DEFAULT_HOLD_SECS == 900.0


def test_target_rows_are_never_reported_held():
    h = TriageHold(clock=Clock())
    held = h.held_macs(desired_rows=[_row(BMC), _row(NIC)],
                       observe_rows=[_obs()], target_macs={BMC, NIC})
    assert held == set()


def test_missing_target_with_latched_identity_is_held():
    """Link blip: the port lost its access_vid, so no targets -- but observe
    still names the same MACs with the serial latched."""
    c = Clock()
    h = TriageHold(clock=c)
    h.held_macs(desired_rows=[_row(BMC), _row(NIC)], observe_rows=[_obs()],
                target_macs={BMC, NIC})
    c.t += 30
    held = h.held_macs(desired_rows=[_row(BMC), _row(NIC)],
                       observe_rows=[_obs()], target_macs=set())
    assert held == {BMC, NIC}


def test_empty_fdb_observe_nulled_macs_but_serial_latched_is_held():
    """R1: pre-B observe nulls bmc_mac/nic_mac on an empty FDB but keeps
    chassis_sn -> still held."""
    c = Clock()
    h = TriageHold(clock=c)
    h.held_macs(desired_rows=[_row(BMC)], observe_rows=[_obs()], target_macs={BMC})
    c.t += 60
    held = h.held_macs(desired_rows=[_row(BMC)],
                       observe_rows=[_obs(bmc=None, nic=None)], target_macs=set())
    assert held == {BMC}


def test_no_serial_is_not_held():
    """Committed link-down: observe's _forget_identity cleared chassis_sn."""
    c = Clock()
    h = TriageHold(clock=c)
    h.held_macs(desired_rows=[_row(BMC)], observe_rows=[_obs()], target_macs={BMC})
    c.t += 10
    held = h.held_macs(desired_rows=[_row(BMC)],
                       observe_rows=[_obs(bmc=None, nic=None, sn=None)],
                       target_macs=set())
    assert held == set()


def test_observe_names_other_macs_is_not_held():
    """A different identity took the slot (B adopted a flip or a new blade)."""
    c = Clock()
    h = TriageHold(clock=c)
    h.held_macs(desired_rows=[_row(BMC)], observe_rows=[_obs()], target_macs={BMC})
    c.t += 10
    held = h.held_macs(desired_rows=[_row(BMC)],
                       observe_rows=[_obs(bmc="1c:34:da:7f:b3:a6",
                                          nic="1c:34:da:7f:b3:a4")],
                       target_macs={"1c:34:da:7f:b3:a6"})
    # BMC is now observe's nic_mac -> still "ours"; the point of this test is
    # a MAC observe no longer names at all:
    h2 = TriageHold(clock=c)
    h2.held_macs(desired_rows=[_row(NIC)], observe_rows=[_obs()], target_macs={NIC})
    c.t += 10
    held2 = h2.held_macs(desired_rows=[_row(NIC)],
                         observe_rows=[_obs(bmc="02:00:00:00:00:09",
                                            nic="02:00:00:00:00:07")],
                         target_macs={"02:00:00:00:00:09", "02:00:00:00:00:07"})
    assert held2 == set()
    assert BMC in held  # still named by observe (as the nic) -> held


def test_hold_expires_after_hold_secs():
    c = Clock()
    h = TriageHold(hold_secs=900, clock=c)
    h.held_macs(desired_rows=[_row(BMC)], observe_rows=[_obs()], target_macs={BMC})
    c.t += 899
    assert h.held_macs(desired_rows=[_row(BMC)], observe_rows=[_obs()],
                       target_macs=set()) == {BMC}
    c.t += 1
    assert h.held_macs(desired_rows=[_row(BMC)], observe_rows=[_obs()],
                       target_macs=set()) == set()


def test_restart_seeds_existing_rows_as_just_seen():
    """Review Focus 4: a fresh process has no memory; every existing triage
    row counts as seen NOW, so nothing is swept on the first cycle, and a row
    still absent is released hold_secs later."""
    c = Clock()
    h = TriageHold(hold_secs=900, clock=c)
    assert h.held_macs(desired_rows=[_row(BMC)], observe_rows=[_obs()],
                       target_macs=set()) == {BMC}
    c.t += 900
    assert h.held_macs(desired_rows=[_row(BMC)], observe_rows=[_obs()],
                       target_macs=set()) == set()


def test_other_owner_rows_are_ignored():
    h = TriageHold(clock=Clock())
    assert h.held_macs(desired_rows=[_row(BMC, owner="post")],
                       observe_rows=[_obs()], target_macs=set()) == set()


def test_macs_are_normalised():
    c = Clock()
    h = TriageHold(clock=c)
    h.held_macs(desired_rows=[_row(BMC.upper())], observe_rows=[_obs()],
                target_macs={BMC.upper()})
    c.t += 5
    assert h.held_macs(desired_rows=[_row(BMC.upper())],
                       observe_rows=[_obs(bmc=BMC.upper())],
                       target_macs=set()) == {BMC}


def test_memory_is_pruned_for_rows_that_are_gone():
    c = Clock()
    h = TriageHold(clock=c)
    h.held_macs(desired_rows=[_row(BMC)], observe_rows=[_obs()], target_macs={BMC})
    h.held_macs(desired_rows=[], observe_rows=[_obs()], target_macs=set())
    assert BMC not in h._last_target
