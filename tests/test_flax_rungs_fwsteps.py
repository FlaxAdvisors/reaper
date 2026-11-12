"""flax_rungs.fwsteps is a copy of Post's firmware step maps; it must stay equal."""
import itertools

from flax_post import blades
from flax_post.nicd import classify as nic_classify
from flax_rungs import fwsteps


def test_bmc_and_bios_maps_equal_posts():
    assert fwsteps.FW_BMC == blades._FW_BMC
    assert fwsteps.FW_BIOS == blades._FW_BIOS


def test_step_names_are_posts():
    assert fwsteps.FW_STEPS == ("bmc-checked", "bmc-updated", "bios-checked",
                                "bios-updated", "mlx-checked", "mlx-updated")


def test_unknown_phase_defaults_like_post():
    assert fwsteps.bmc_steps(None) == ("pending", "pending")
    assert fwsteps.bios_steps("nonsense") == ("pending", "pending")
    assert fwsteps.bmc_steps("unsupported") == ("done", "done")
    assert fwsteps.bios_steps("unsupported") == ("done", "fault")


_PHASES = ["up_to_date", "needs_update", "flashing", "unsupported", "fault", "checking", None]


def _device_lists():
    yield []
    for n in (1, 2):
        for combo in itertools.product(_PHASES, repeat=n):
            for cur in itertools.product((None, "16.35.1012"), repeat=n):
                yield [{"phase": p, "current": c} for p, c in zip(combo, cur)]


def test_nic_aggregate_equals_nicd():
    for devs in _device_lists():
        assert fwsteps.nic_aggregate(devs) == nic_classify.aggregate(devs), devs


def test_nic_steps_equals_blades():
    slices = [{}, {"fw_nic": None}, {"fw_nic": {}},
              {"fw_nic": {"phase": "unreachable"}}, {"fw_nic": {"phase": "fault"}}]
    for devs in _device_lists():
        slices.append({"fw_nic": {"phase": "checking", "devices": devs}})
        slices.append({"fw_nic": {"devices": devs}})
    for st in slices:
        assert fwsteps.nic_steps(st) == blades._nic_steps(st), st


