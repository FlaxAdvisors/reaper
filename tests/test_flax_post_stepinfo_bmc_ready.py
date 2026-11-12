from flax_post import stepinfo


def test_bmc_ready_step_modal_shows_the_retry_budget_in_effect():
    lad = {"rung": "bmc-ready", "since": 1000.0, "power_on_at": 10.0, "marks": {}, "fault": None,
           "bmc_ready_attempts": [{"at": 700.0, "waited_s": 601.0}, {"at": 1000.0, "waited_s": 601.0}]}
    rec = {"ladder": lad, "ladder_view": {}, "bmc_ip": "10.0.0.6", "bmc_pinged": True}
    rows, _ = stepinfo._discover(rec, {}, "bmc-ready", "cur")
    d = {r[0]: r[1] for r in rows}
    assert d["budget"] == "720 s" and d["retries"] == "2 of 3"
