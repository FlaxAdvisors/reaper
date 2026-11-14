"""fio judge: history baseline + sibling check; labels only."""
from flax_post import fio_judge as fj


def _pj(**m):
    return {"rw": "read", "bs": "1M", "iodepth": 1, "err": None, "dirs": {"read": m}}


def _base(groups):
    return {"suite": "v1", "groups": groups}


def _g(job, metric, mean, std, n=50, controller=None, model="MODELX"):
    return {"model": model, "controller": controller, "job": job, "metric": metric,
            "n": n, "mean": mean, "std": std, "clipped": 0}


def test_normalise_model_matches_alldisks_and_smartctl_forms():
    assert fj.normalise_model("SAMSUNG_MZQL21T9HCJR-00A07") == fj.normalise_model("SAMSUNG MZQL21T9HCJR-00A07")
    assert fj.normalise_model("ST6000NM019B-2TG") == fj.normalise_model("ATA ST6000NM019B-2TG")
    assert fj.normalise_model(None) is None


def test_judge_flags_slow_drive_against_history():
    base = _base([_g("bw", "mbps", 240, 5)])
    disks = {"a": {"model": "ModelX", "controller": None}, "b": {"model": "ModelX", "controller": None}}
    r = fj.judge({"a": {"bw": _pj(mbps=220)}, "b": {"bw": _pj(mbps=250)}}, disks, base)
    assert r["a"]["verdict"] == "outside"
    assert "bw" in r["a"]["reasons"][0] and "220" in r["a"]["reasons"][0]
    assert r["a"]["expected"]["bw"].startswith("≥ 230")  # mean - 2σ
    assert r["b"]["verdict"] == "ok"
    assert fj.flagged(r) == ["a"]


def test_judge_better_than_normal_is_ok():
    base = _base([_g("lat", "lat_us", 40, 3), _g("lat", "p99_us", 60, 5)])
    r = fj.judge({"a": {"lat": _pj(lat_us=10, p99_us=15)}}, {"a": {"model": "modelx"}}, base)
    assert r["a"]["verdict"] == "ok"


def test_judge_falls_back_to_any_controller_group():
    base = _base([_g("bw", "mbps", 100, 1, n=2, controller="C1"), _g("bw", "mbps", 240, 5)])
    r = fj.judge({"a": {"bw": _pj(mbps=200)}}, {"a": {"model": "modelx", "controller": "C1"}}, base)
    assert r["a"]["verdict"] == "outside"


def test_judge_sibling_check_without_history():
    disks = {d: {"model": "M"} for d in "abcd"}
    vals = dict(zip("abcd", (240, 241, 239, 180)))
    r = fj.judge({d: {"bw": _pj(mbps=v)} for d, v in vals.items()}, disks, {})
    assert fj.flagged(r) == ["d"]
    assert r["a"]["verdict"] == "ok"
    r2 = fj.judge({d: {"bw": _pj(mbps=vals[d])} for d in "ab"}, {d: disks[d] for d in "ab"}, {})
    assert {x["verdict"] for x in r2.values()} == {"no baseline"}


def test_judge_without_baseline_file(tmp_path):
    assert fj.load_baseline(tmp_path / "missing.json") == {}
    r = fj.judge({"a": {"bw": _pj(mbps=1)}}, {"a": {"model": "M"}}, fj.load_baseline(tmp_path / "missing.json"))
    assert r["a"]["verdict"] == "no baseline"


def test_sata_model_key_equals_between_sysfs_and_smartctl_forms():
    # runtime alldisks prints the 16-char sysfs model (last token only when spaced);
    # smartctl gives the full name.  Same drive -> same key.
    for sysfs, full in (("ST6000NM019B-2TG", "ST6000NM019B-2TG103"),
                        ("HGST HUS726060AL", "HGST HUS726060ALE611")):
        assert fj.normalise_model(sysfs, "sdb") == fj.normalise_model(full, "sdb")
    # NVMe keeps the whole model
    assert fj.normalise_model("SAMSUNG MZQL21T9HCJR-00A07", "nvme0n1") == "SAMSUNG MZQL21T9HCJR-00A07"


def test_judge_uses_the_dev_aware_key_for_history_and_siblings():
    # a cut 16-char key is judged by history only from a group that carries the size
    base = _base([dict(_g("bw", "mbps", 240, 5, model="HGST HUS726060AL"), size_gb=6001.2)])
    disks = {"sda": {"model": "HGST HUS726060ALE611", "size_gb": 6001.2},
             "sdb": {"model": "HGST HUS726060AL", "size_gb": 6001.2}}
    r = fj.judge({"sda": {"bw": _pj(mbps=100)}, "sdb": {"bw": _pj(mbps=240)}}, disks, base)
    assert r["sda"]["verdict"] == "outside" and r["sdb"]["verdict"] == "ok"


def test_judge_std_zero_group_does_not_flag_a_hair_thin_miss():
    base = _base([_g("bw", "mbps", 500, 0)])
    d = {"a": {"model": "MODELX"}}
    assert fj.judge({"a": {"bw": _pj(mbps=499.9)}}, d, base)["a"]["verdict"] == "ok"
    r = fj.judge({"a": {"bw": _pj(mbps=480)}}, d, base)["a"]       # 2σ floor = 2% of 500
    assert r["verdict"] == "outside" and r["expected"]["bw"] == "≥ 490 MB/s"
    assert "1%" in fj.note()


def test_judge_ignores_a_baseline_from_another_suite():
    base = dict(_base([_g("bw", "mbps", 240, 5)]), suite="v2")
    r = fj.judge({"a": {"bw": _pj(mbps=100)}}, {"a": {"model": "MODELX"}}, base)
    assert r["a"]["verdict"] == "no baseline"


def test_judge_sets_aside_a_too_wide_history_metric_and_judges_the_rest():
    base = _base([_g("lat", "lat_us", 100, 5), _g("lat", "p99_us", 1000, 300)])  # p99 30% spread
    r = fj.judge({"sda": {"lat": _pj(lat_us=101, p99_us=9000)}}, {"sda": {"model": "MODELX"}}, base)
    assert r["sda"]["verdict"] == "ok" and r["sda"]["reasons"] == []
    assert r["sda"]["spread"] == {"lat": ["p99_us"]}
    assert fj.no_baseline_fragment(r) == ""


def test_judge_all_metrics_too_wide_is_no_baseline_with_the_spread_label():
    base = _base([_g("bw", "mbps", 200, 60)])
    r = fj.judge({"sda": {"bw": _pj(mbps=10)}}, {"sda": {"model": "MODELX"}}, base)
    assert r["sda"]["verdict"] == "no baseline"
    assert r["sda"]["expected"]["bw"] == "no baseline (spread too wide)"
    assert fj.no_baseline_fragment(r) == " · no baseline"


def test_judge_sibling_check_still_applies_under_the_spread_cap():
    base = _base([_g("bw", "mbps", 200, 60)])
    parsed = {d: {"bw": _pj(mbps=v)} for d, v in (("sda", 200), ("sdb", 200), ("sdc", 100))}
    disks = {d: {"model": "MODELX"} for d in parsed}
    r = fj.judge(parsed, disks, base)
    assert r["sdc"]["verdict"] == "outside" and "sibling" in r["sdc"]["reasons"][0]


def test_judge_spread_at_the_cap_is_still_used():
    base = _base([_g("bw", "mbps", 200, 200 * fj.SPREAD_CAP)])
    r = fj.judge({"sda": {"bw": _pj(mbps=100)}}, {"sda": {"model": "MODELX"}}, base)
    assert r["sda"]["verdict"] == "outside"


def test_note_names_the_spread_cap():
    assert f"{fj.SPREAD_CAP * 100:g}%" in fj.note() and "spread too wide" in fj.note()


def test_constants_are_two_sigma_and_twenty_percent():
    # operator 2026-11-13: 1σ flagged ~12% of the history; back to 2σ and 20% siblings
    assert (fj.FLAG_SIGMA, fj.SIBLING_PCT, fj.MIN_SIBLINGS) == (2.0, 20, 3)


def test_size_key_rounds_to_three_significant_figures():
    assert fj.size_key(3840.7) == fj.size_key(3840.8) == 3840.0
    assert fj.size_key(256.0) == 256.0 and fj.size_key(None) is None


def test_siblings_need_the_same_size():
    disks = {"a": {"model": "M", "size_gb": 480}, "b": {"model": "M", "size_gb": 960},
             "c": {"model": "M", "size_gb": 960}}
    r = fj.judge({d: {"bw": _pj(mbps=v)} for d, v in zip("abc", (100, 200, 200))}, disks, {})
    assert {x["verdict"] for x in r.values()} == {"no baseline"}


def test_site_peers_judge_a_lone_drive():
    disks = {"nvme0n1": {"model": "SAMSUNG MZVL4256HBJD-00BH1", "size_gb": 256.0}}
    key = ("SAMSUNG MZVL4256HBJD-00BH1", 256.0)
    peers = {key: {("bw", "mbps"): [2450, 2640, 2770]}}
    r = fj.judge({"nvme0n1": {"bw": _pj(mbps=569)}}, disks, {}, peers=peers)
    assert r["nvme0n1"]["verdict"] == "outside" and "3 sibling median" in r["nvme0n1"]["reasons"][0]
    ok = fj.judge({"nvme0n1": {"bw": _pj(mbps=2500)}}, disks, {}, peers=peers)
    assert ok["nvme0n1"]["verdict"] == "ok"


def test_one_peer_is_not_enough():
    disks = {"a": {"model": "M", "size_gb": 1.0}}
    r = fj.judge({"a": {"bw": _pj(mbps=1)}}, disks, {}, peers={("M", 1.0): {("bw", "mbps"): [100]}})
    assert r["a"]["verdict"] == "no baseline"


def test_history_group_with_size_matches_only_that_size():
    g = dict(_g("bw", "mbps", 240, 5), size_gb=960.0)
    r = fj.judge({"a": {"bw": _pj(mbps=100)}}, {"a": {"model": "MODELX", "size_gb": 480}}, _base([g]))
    assert r["a"]["verdict"] == "no baseline"
    r2 = fj.judge({"a": {"bw": _pj(mbps=100)}}, {"a": {"model": "MODELX", "size_gb": 960}}, _base([g]))
    assert r2["a"]["verdict"] == "outside"


def test_v1_group_with_a_cut_sata_model_is_not_used():  # RF5
    g = _g("bw", "mbps", 240, 5, model="SANDISK SD9SN8W-")
    r = fj.judge({"sda": {"bw": _pj(mbps=100)}},
                 {"sda": {"model": "SanDisk SD9SN8W-512G", "size_gb": 512}}, _base([g]))
    assert r["sda"]["verdict"] == "no baseline"


def test_note_scope():
    from flax_post import fio_judge as fj
    assert "in the node and across the site" in fj.note()
    assert "across the site" not in fj.note(site=False) and "in the node" in fj.note(site=False)
