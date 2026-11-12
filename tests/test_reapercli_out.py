import json

import pytest

pytest.importorskip("reapercli")
from reapercli.lib.out import Row, exit_code, render, worst  # noqa: E402


def test_worst_orders_fail_first():
    assert worst(["ok", "warn", "FAIL"]) == "FAIL"
    assert worst(["ok", "warn"]) == "warn"


def test_worst_empty_is_ok():
    assert worst([]) == "ok"


def test_worst_info_beats_dormant():
    assert worst(["info", "dormant"]) == "info"


def test_exit_code_only_fail_counts():
    assert exit_code([Row("s", "i", "warn")]) == 0
    assert exit_code([Row("s", "i", "ok"), Row("s", "j", "FAIL")]) == 1


def test_unreachable_ranks_under_fail_and_still_exits_1():
    assert worst(["warn", "unreachable"]) == "unreachable" and worst(["unreachable", "FAIL"]) == "FAIL"
    assert exit_code([Row("s", "i", "unreachable")]) == 1


def test_render_json_round_trips():
    rows = [Row("ha", "vip", "warn", "on standby", host="bang-edam", site="eindhoven")]
    data = json.loads(render(rows, as_json=True))
    assert data[0]["status"] == "warn"
    assert data[0]["host"] == "bang-edam"


def test_render_human_one_line_per_row():
    rows = [Row("ha", "vip", "ok", "bang-gouda", site="eindhoven"),
            Row("host", "disk /", "FAIL", "96%", host="bang-gouda", site="eindhoven")]
    out = render(rows).splitlines()
    assert len(out) == 2
    assert "FAIL" in out[1] and "96%" in out[1] and "bang-gouda" in out[1]
