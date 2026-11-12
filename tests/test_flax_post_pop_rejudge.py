"""A latched population-only fail is re-judged when the node's profile changes
(operator 2026-11-10: LVQ et8b3/et8b4 assigned tec6-spare-tp after the header's
tec6-compute-tp had failed them; only a power cycle re-checked)."""
import pytest

from flax_post.observe import host_qual
from test_flax_post_host_qual import TARGET, _store

PROFILES = {"compute": ["12 Memory Size: 64 GB"], "spare": ["12 Memory Size: 32 GB"]}


class Dead:
    def health(self):
        raise AssertionError("a latched, finished run must not poll the agent")


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(host_qual.population, "load_profile", lambda name: PROFILES.get(name, []))
    monkeypatch.setattr(host_qual._deploy, "enabled", lambda: True)
    s = _store()
    s.profile = "compute"
    monkeypatch.setattr(host_qual, "_read_profile_for", lambda target, st: st.profile)
    s.updated = []
    s.update_result = lambda mac, result: s.updated.append(result)
    s.write_artifact("aa:bb", "r1", "inventory", "macinv", "raw", "12 Memory Size: 32 GB")
    s.set_state("et6b1", power_on="on",
                pop={"profile": "compute", "verdict": "red", "failed_rules": ["12 Memory Size: 64 GB"]},
                checks={"tpm-state": {"status": "pass"}, "smbios-serial": {"status": "skip"}},
                done={"verdict": "fail", "steps": {"Discover": {}, "Firmware": {}}},
                qual={"run_id": "r1", "agent": {"reachable": True},
                      "overall": {"status": "done", "verdict": "pass"},
                      "steps": {"inventory": {"status": "pass"}, "population-check": {"status": "fail"}}})
    return s


def _poll(s):
    return host_qual.poll_target(TARGET, make_client=lambda ip: Dead(), store=s)


def test_same_profile_stays_latched(store):
    _poll(store)
    assert store.state["et6b1"]["done"]["verdict"] == "fail" and store.updated == []


def test_new_profile_that_fits_flips_to_pass_for_deploy(store):
    store.profile = "spare"
    q = _poll(store)
    row = store.state["et6b1"]
    assert row["done"]["verdict"] == "pass" and row["done"]["deploy"] == "pending"
    assert row["done"]["rejudged"]["from"] == "compute" and row["done"]["rejudged"]["profile"] == "spare"
    assert row["done"]["steps"] == {"Discover": {}, "Firmware": {}}
    assert row["pop"]["verdict"] == "green" and row["pop"]["profile"] == "spare"
    assert q["steps"]["population-check"]["status"] == "pass"
    [res] = store.updated
    assert res["run_id"] == "r1" and res["verdict"] == "pass" and res["pop"]["profile"] == "spare"
    _poll(store)                                    # pass is latched: no second judge
    assert len(store.updated) == 1


def test_new_profile_still_red_is_stored_once_and_stays_fail(store):
    store.profile = "nope"
    PROFILES["nope"] = ["99 Memory Size: 1 TB"]
    _poll(store)
    row = store.state["et6b1"]
    assert row["done"]["verdict"] == "fail" and row["pop"]["profile"] == "nope"
    assert store.updated == []


@pytest.mark.parametrize("change", [
    {"qual": {"run_id": "r1", "overall": {"status": "done", "verdict": "fail"}, "steps": {}}},
    {"checks": {"tpm-state": {"status": "fail"}}},
    {"power_on": "off"},
])
def test_other_fails_and_off_nodes_are_never_rejudged(store, change):
    store.set_state("et6b1", **change)
    store.profile = "spare"
    _poll(store)
    assert store.state["et6b1"]["done"]["verdict"] == "fail" and store.updated == []


# --- the live path: a done-rung row is never polled, the worker re-judges it -----
def test_rejudge_target_flips_a_finished_fail_without_the_agent(store):
    store.profile = "spare"
    host_qual.rejudge_target(TARGET, store=store)
    assert store.state["et6b1"]["done"]["verdict"] == "pass"


def test_rejudge_target_leaves_a_fail_whose_battery_is_still_running(store):
    store.state["et6b1"]["qual"]["overall"]["status"] = "running"
    store.profile = "spare"
    assert host_qual.rejudge_target(TARGET, store=store) is None
    assert store.state["et6b1"]["done"]["verdict"] == "fail"


def _done_rec(**kw):
    from test_flax_post_worker import REC
    return {**REC, "power_on": "on", "verdict": "fail", "battery_done": True,
            "ladder": {"rung": "done", "since": 1.0}, **kw}


@pytest.mark.parametrize("rec, called", [
    (_done_rec(), True),
    (_done_rec(verdict="pass"), False),
    (dict(_done_rec(), battery_done=False), False),
    (dict(_done_rec(), power_on="off"), False),
])
def test_worker_rejudges_only_finished_fails_at_the_done_rung(rec, called):
    from flax_post.observe import worker
    from test_flax_post_worker import Deps
    d = Deps(rec)
    d.rejudged = []
    d.rejudge = lambda r: d.rejudged.append(r["port"])
    worker.iterate_once("et6b1", d, True, 100.0)
    assert d.rejudged == (["et6b1"] if called else []) and d.polls == []


def test_real_deps_rejudge_hands_the_record_to_host_qual(monkeypatch):
    from flax_post.observe import worker
    from test_flax_post_worker import FakeFeed, FakeStore
    got = {}
    monkeypatch.setattr(worker.host_qual, "rejudge_target",
                        lambda target, **kw: got.update(target=target, **kw))
    store = FakeStore()
    worker.RealDeps(FakeFeed(), store=store).rejudge(_done_rec(population="spare"))
    assert got["target"]["port"] == "et6b1" and got["target"]["population"] == "spare"
    assert got["store"] is store
