from flax_reconcile.steer import compute_steers, load_no_steer


SF = {
    ("rabbit-fiesta", "Ethernet6/1"): {"mask": "access", "access_vid": 17, "link": "link"},
    ("rabbit-fiesta", "Ethernet9/1"): {"mask": "trunk", "access_vid": None, "link": "link"},
    ("rabbit-gouda",  "Ethernet32/1"): {"mask": "access", "access_vid": 31, "link": "link"},
}


def test_steer_when_access_and_vid_differs():
    desired = [{"switch": "rabbit-fiesta", "port": "Ethernet6/1",
                "desired_vid": 16, "occupants": {}}]
    out = compute_steers(desired, SF, no_steer=set())
    assert out == [{"switch": "rabbit-fiesta", "port": "Ethernet6/1",
                    "desired_vid": 16, "current_vid": 17,
                    "action": "steer", "reason": None}]


def test_no_action_when_vid_already_matches():
    desired = [{"switch": "rabbit-fiesta", "port": "Ethernet6/1",
                "desired_vid": 17, "occupants": {}}]
    assert compute_steers(desired, SF, no_steer=set()) == []


def test_refuse_on_trunk_port():
    desired = [{"switch": "rabbit-fiesta", "port": "Ethernet9/1",
                "desired_vid": 16, "occupants": {}}]
    out = compute_steers(desired, SF, no_steer=set())
    assert out[0]["action"] == "refuse" and out[0]["reason"] == "non_access_port"


def test_refuse_on_no_steer_listed_access_uplink():
    # access-presented uplink to a downstream peer (eindhoven v31). mask=access
    # would PASS -- only the no_steer list catches it.
    desired = [{"switch": "rabbit-gouda", "port": "Ethernet32/1",
                "desired_vid": 16, "occupants": {}}]
    out = compute_steers(desired, SF, no_steer={("rabbit-gouda", "Ethernet32/1")})
    assert out[0]["action"] == "refuse" and out[0]["reason"] == "no_steer_listed"


def test_load_no_steer_parses_list(tmp_path):
    p = tmp_path / "no-steer-ports.json"
    p.write_text('[{"switch":"rabbit-gouda","port":"Ethernet32/1","reason":"v31"}]')
    assert load_no_steer(str(p)) == {("rabbit-gouda", "Ethernet32/1")}


def test_load_no_steer_missing_file_is_empty(tmp_path):
    assert load_no_steer(str(tmp_path / "nope.json")) == set()


def _want(port="Ethernet7/1", vid=24):
    return [{"switch": "rabbit-x", "port": port, "desired_vid": vid, "occupants": {}}]


def test_no_steer_on_empty_port():
    sf = {("rabbit-x", "Ethernet7/1"): {"mask": "access", "access_vid": 25, "link": "nolink"}}
    assert compute_steers(_want(), sf, no_steer=set()) == []


def test_no_refuse_noise_on_empty_trunkless_port():
    sf = {("rabbit-x", "Ethernet7/1"): {"mask": None, "access_vid": None, "link": "nolink"}}
    assert compute_steers(_want(), sf, no_steer=set()) == []


def test_no_steer_when_link_unknown_or_missing():
    sf = {("rabbit-x", "Ethernet7/1"): {"mask": "access", "access_vid": 25, "link": "unknown"}}
    assert compute_steers(_want(), sf, no_steer=set()) == []
    assert compute_steers(_want("Ethernet9/9"), sf, no_steer=set()) == []


def test_steer_still_fires_on_linked_port():
    sf = {("rabbit-x", "Ethernet7/1"): {"mask": "access", "access_vid": 25, "link": "link"}}
    out = compute_steers(_want(), sf, no_steer=set())
    assert out[0]["action"] == "steer" and out[0]["current_vid"] == 25
