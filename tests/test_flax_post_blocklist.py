"""blocklist: known-bad DIMM signatures fail the population check and paint the tables red (ruling 2026-09-12)."""
import json
import os

import pytest

from flax_post import blocklist
from flax_post.observe import host_qual

_DIMMSUM = ("  64 GB  DIMM A0  _Node0_Channel0_Dimm0  2666   Samsung  39E16042  M386A4G40DM0-CPB     \n"
            "  64 GB  DIMM A1  _Node0_Channel1_Dimm0  2666   Samsung  39F0A232  M386A8K40BM2-CTD     \n"
            "  No Module Installed  DIMM A3  _Node0_Channel3_Dimm0 \n"
            "  64 GB  DIMM B0  _Node1_Channel0_Dimm0  2666   Samsung  39E16042  M386A4G40DM0-CPB     \n")


def test_parse_dimmsum_reads_populated_slots_only():
    d = blocklist.parse_dimmsum(_DIMMSUM)
    assert [x["slot"] for x in d] == ["DIMM A0", "DIMM A1", "DIMM B0"]
    assert d[0] == {"slot": "DIMM A0", "size": "64 GB", "speed": "2666", "mfg": "Samsung",
                    "serial": "39E16042", "part": "M386A4G40DM0-CPB"}


_CLONE = {"serial": "39E16042", "part": "M386A4G40DM0-CPB", "mfg": "Samsung",
          "reason": "cloned SPD (triage finding 2026-09-10)"}


@pytest.fixture
def parts_file(tmp_path, monkeypatch):
    """blocklist-parts.json in a private profile dir (the shared triage/post
    file, spec 2026-10-02-triage-node-config-pop). Never the live one."""
    monkeypatch.setattr(blocklist.population, "PROFILE_DIR", str(tmp_path))
    monkeypatch.delenv("FLAX_POST_BLOCKLIST", raising=False)
    f = tmp_path / "blocklist-parts.json"

    def write(obj):
        f.write_text(obj if isinstance(obj, str) else json.dumps(obj))
    return write


def test_the_file_lives_beside_the_profiles(parts_file):
    assert blocklist.path() == os.path.join(blocklist.population.PROFILE_DIR, "blocklist-parts.json")


def test_the_site_file_catches_the_cloned_spd_dimm(parts_file):
    parts_file({"dimms": [_CLONE]})
    hits = blocklist.check_dimmsum(_DIMMSUM)
    assert [h["slot"] for h in hits] == ["DIMM A0", "DIMM B0"]
    assert "cloned SPD" in hits[0]["reason"]
    assert blocklist.describe(hits[0]).startswith("blocked DIMM in DIMM A0: Samsung M386A4G40DM0-CPB SN 39E16042")


def test_a_missing_file_blocks_nothing(parts_file):
    assert blocklist.load() == []
    assert blocklist.check_dimmsum(_DIMMSUM) == []


@pytest.mark.parametrize("bad", ['{"dimms": [', [_CLONE], {"dimm": [_CLONE]}, {"dimms": _CLONE},
                                 {"dimms": ["x"]}, {"dimms": [_CLONE, {"serail": "39E16042"}]}])
def test_an_unreadable_file_fails_closed(parts_file, bad):
    """Broken JSON, the wrong shape, or an entry naming none of serial/part/mfg
    (a typo) must not quietly block nothing: the check reports one unreadable
    hit, so the population check goes red with a reason that says so."""
    parts_file(bad)
    hits = blocklist.check_dimmsum(_DIMMSUM)
    assert len(hits) == 1 and hits[0].get("error")
    assert blocklist.describe(hits[0]).startswith("blocklist unreadable:")
    assert "blocklist-parts.json" in blocklist.describe(hits[0])


def test_an_unreadable_file_turns_the_population_check_red(parts_file, monkeypatch):
    parts_file("{oops")
    monkeypatch.setattr(host_qual.population, "load_profile", lambda name: ["12 Memory Size: 64 GB"])
    pop = host_qual.population_check("12 Memory Size: 64 GB", "ovh-bare-tp",
                                     "  64 GB  DIMM A0  _Node0_Channel0_Dimm0  2666   Samsung  39F0A232  M386A8K40BM2-CTD\n")
    assert pop["verdict"] == "red" and pop["failed_rules"][0].startswith("blocklist unreadable:")


def test_match_needs_every_named_field_case_and_space_insensitive():
    sig = {"serial": "39e16042", "part": "m386a4g40dm0-cpb ", "mfg": "SAMSUNG"}
    assert blocklist.matches({"serial": "39E16042", "part": "M386A4G40DM0-CPB", "mfg": "Samsung"}, sig)
    assert not blocklist.matches({"serial": "39E16042", "part": "M386A8K40BM2-CTD", "mfg": "Samsung"}, sig)
    assert not blocklist.matches({"serial": "39E16042"}, {"reason": "no identity"})


def test_explicit_signatures_are_used_as_given():
    sigs = [{"serial": "DEADBEEF", "reason": "recalled lot"}]
    assert blocklist.check([{"slot": "DIMM C0", "serial": "DEADBEEF", "part": "X", "mfg": "Y"}], sigs)[0]["reason"] == "recalled lot"


def test_population_check_goes_red_on_a_blocked_dimm_even_when_every_rule_matches(parts_file, monkeypatch):
    parts_file({"dimms": [_CLONE]})
    monkeypatch.setattr(host_qual.population, "load_profile", lambda name: ["12 Memory Size: 64 GB"])
    pop = host_qual.population_check("12 Memory Size: 64 GB", "ovh-bare-tp", _DIMMSUM)
    assert pop["verdict"] == "red"
    assert len(pop["blocked_dimms"]) == 2 and pop["failed_rules"][0].startswith("blocked DIMM in DIMM A0")
    clean = host_qual.population_check("12 Memory Size: 64 GB", "ovh-bare-tp",
                                       "  64 GB  DIMM A0  _Node0_Channel0_Dimm0  2666   Samsung  39F0A232  M386A8K40BM2-CTD\n")
    assert clean["verdict"] == "green" and "blocked_dimms" not in clean


def test_poll_target_feeds_the_dimmsum_artifact_into_the_check(parts_file, monkeypatch):
    parts_file({"dimms": [_CLONE]})
    from tests.test_flax_post_host_qual import FakeAgent, _store, TARGET
    monkeypatch.setattr(host_qual.population, "load_profile", lambda name: ["12 Memory Size: 64 GB"])
    monkeypatch.setattr(host_qual, "_read_profile_for", lambda target, store: "p")
    agent = FakeAgent(stages=[{"name": "inventory", "status": "pass"}],
                      status_body={"status": "running", "current": "iperf", "done_n": 6, "total_n": 12, "verdict": None, "pct": 50},
                      arts={("inventory", "macinv"): "12 Memory Size: 64 GB", ("inventory", "dimmsum"): _DIMMSUM})
    store = _store()
    q = host_qual.poll_target(TARGET, make_client=lambda ip: agent, store=store)
    assert store.state["et6b1"]["pop"]["verdict"] == "red"
    assert q["steps"]["population-check"]["status"] == "fail"
    assert len(store.state["et6b1"]["pop"]["blocked_dimms"]) == 2
