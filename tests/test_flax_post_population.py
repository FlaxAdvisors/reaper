# tests/test_flax_post_population.py
"""Ghost node_config string-match profiles: list, parse, evaluate vs inventory dump.

Corrected grammar: each profile rule is a FULL-LINE PRESENCE REGEX matched
against macinv's COUNT-form output. The leading integer is part of the regex
(it matches the `uniq -c` count prefix), NOT a separate expected line-count --
so `8 Memory Size: 32 GB` matches a count line `8 Memory Size: 32 GB, ...` and
does NOT match `12 Memory Size: 64 GB, ...`.
"""
from flax_post import population

_PROFILE = (
    "'BIOS Version: TPC_P26C'\n"
    "'2 Processor.*Intel.*Gold 6138.*2.00GHz'\n"
    "'8 Memory Size: 32 GB'\n"
)
# a real count-form dump: uniq -c collapses identical components to one line
_DUMP_OK = (
    "BIOS Version: TPC_P26C, Release Date: 10/15/2020\n"
    "2 Processor Version: Intel(R) Xeon(R) Gold 6138 CPU @ 2.00GHz\n"
    "8 Memory Size: 32 GB, Speed: 2666 MT/s, Mfg: Samsung, Part: M393A4K40BB2-CTD\n"
)
# a 12x64GB node: same fields, different counts+size -> memory rule must fail
_DUMP_WRONG_MEM = (
    "BIOS Version: TPC_P26C, Release Date: 10/15/2020\n"
    "2 Processor Version: Intel(R) Xeon(R) Gold 6138 CPU @ 2.00GHz\n"
    "12 Memory Size: 64 GB, Speed: 2666 MT/s, Mfg: Micron, Part: 72ASS8G72LZ-2G6D2\n"
)


def test_parse_profile_keeps_whole_line_including_leading_integer():
    rules = population.parse_profile(_PROFILE)
    assert rules == [
        "BIOS Version: TPC_P26C",
        "2 Processor.*Intel.*Gold 6138.*2.00GHz",
        "8 Memory Size: 32 GB",
    ]


def test_parse_profile_skips_blank_and_comment_lines():
    rules = population.parse_profile("# a comment\n\n'BIOS Version: X'\n   \n")
    assert rules == ["BIOS Version: X"]


def test_evaluate_all_pass_presence():
    verdict = population.evaluate(population.parse_profile(_PROFILE), _DUMP_OK)
    assert verdict["ok"] is True
    assert all(r["ok"] for r in verdict["results"])
    mem = next(r for r in verdict["results"] if "Memory Size" in r["rule"])
    assert mem["rule"] == "8 Memory Size: 32 GB" and mem["found"] == 1


def test_evaluate_leading_integer_is_part_of_the_regex():
    # the '8 ...32 GB' rule must NOT match a '12 ...64 GB' count line
    verdict = population.evaluate(population.parse_profile(_PROFILE), _DUMP_WRONG_MEM)
    assert verdict["ok"] is False
    mem = next(r for r in verdict["results"] if "Memory Size" in r["rule"])
    assert mem["found"] == 0 and mem["ok"] is False
    # the CPU + BIOS rules still match this node
    cpu = next(r for r in verdict["results"] if "Gold 6138" in r["rule"])
    assert cpu["found"] == 1 and cpu["ok"] is True


def test_evaluate_bad_regex_is_reported_not_raised():
    verdict = population.evaluate(["(unterminated"], "any dump")
    assert verdict["ok"] is False
    assert verdict["results"] == [{"rule": "(unterminated", "found": 0, "ok": False}]


def test_evaluate_result_shape_has_no_expected_key():
    verdict = population.evaluate(["BIOS Version: X"], "BIOS Version: X\n")
    assert set(verdict["results"][0]) == {"rule", "found", "ok"}


def test_list_profiles_skips_backups(tmp_path, monkeypatch):
    (tmp_path / "ovh-bare-tp").write_text(_PROFILE)
    (tmp_path / "linprof-compute~").write_text(_PROFILE)   # editor backup → skip
    (tmp_path / ".hidden").write_text(_PROFILE)            # hidden → skip
    (tmp_path / "subdir").mkdir()                          # directory → skip
    monkeypatch.setattr(population, "PROFILE_DIR", str(tmp_path))
    assert population.list_profiles() == ["ovh-bare-tp"]
    assert "subdir" not in population.list_profiles()


def test_list_profiles_skips_json(tmp_path, monkeypatch):
    """blocklist-parts.json shares the dir; it is not a population profile."""
    (tmp_path / "ovh-bare-tp").write_text("x\n")
    (tmp_path / "blocklist-parts.json").write_text('{"dimms": []}')
    monkeypatch.setattr(population, "PROFILE_DIR", str(tmp_path))
    assert population.list_profiles() == ["ovh-bare-tp"]


def test_load_profile(tmp_path, monkeypatch):
    (tmp_path / "ovh-bare-tp").write_text(_PROFILE)
    monkeypatch.setattr(population, "PROFILE_DIR", str(tmp_path))
    assert population.load_profile("ovh-bare-tp")[0] == "BIOS Version: TPC_P26C"
