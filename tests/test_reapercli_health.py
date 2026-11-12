"""Per-bang health rows (spec §7, §7.1)."""
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli.lib.components import load_components  # noqa: E402
from reapercli.lib.health import component_rows, host_rows, rollup, unmanaged_rows  # noqa: E402
from reapercli.lib.out import Row  # noqa: E402
from reapercli.lib.sites import load_sites  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
COMPS = load_components(ROOT, load_sites(ROOT))
PINS = {"apply_flax_post_image_tag": "0.9.207", "apply_flax_stack_flax_control_image_tag": "0.9.207"}
PAIR = ("bang-gouda", "bang-edam")


def facts(units=None, containers=None, **extra):
    f = {"host": {"disks": [{"mount": "/", "used_pct": 40}], "mem_avail_pct": 50, "failed_units": [],
                  "chrony_offset_s": 0.001, "tailscale": {}, "load1": 0.5},
         "units": units or {"_present": []}, "containers": containers or {}, "timers": {},
         "crons": [], "listen": [], "files": {}, "pg": {"freshness": {}}}
    f.update(extra)
    return f


def rows_for(comp, bang, f, vip="bang-gouda"):
    return component_rows(bang, f, {comp: COMPS[comp]}, vip, PINS, {comp: PAIR})


def find(rows, needle):
    return [r for r in rows if needle in r.item]


def test_flax_post_container_missing_on_vip_holder_fails():
    rows = rows_for("flax_post", "bang-gouda", facts())
    assert find(rows, "flax_post/flax-post")[0].status == "FAIL"


def test_exited_on_standby_is_ok_standby():
    f = facts(containers={"flax-post": {"state": "exited", "image": "flax-control:0.9.207", "health": ""}})
    r = [x for x in rows_for("flax_post", "bang-edam", f) if x.item == "flax_post/flax-post"
         and x.value.startswith("standby")]
    assert r and r[0].status == "ok"


def test_unclaimed_container_is_info_unmanaged():
    f = facts(containers={"fio-results-browser": {"state": "running", "image": "x", "health": ""}})
    rows = unmanaged_rows("bang-fiesta", f, COMPS)
    assert [(r.item, r.status) for r in rows] == [("fio-results-browser", "info")]
    assert "unmanaged" in rows[0].value


def test_unhealthy_kea_fails():
    f = facts(containers={"kea-dhcp4": {"state": "running", "image": "kea:2.6.3", "health": "unhealthy"}})
    assert find(rows_for("kea", "bang-gouda", f), "kea/kea-dhcp4")[0].status == "FAIL"


def test_image_behind_pin_warns():
    f = facts(containers={"flax-stack-flax-control-1": {"state": "running", "health": "",
                                                       "image": "flax-control:0.9.203"}})
    r = find(rows_for("flax_console", "bang-gouda", f), "flax-control")[0]
    assert r.status == "warn" and "0.9.203" in r.value and "0.9.207" in r.value


def test_root_disk_96_fails():
    f = facts()
    f["host"]["disks"] = [{"mount": "/", "used_pct": 96}]
    assert [r for r in host_rows("bang-gouda", f, {}) if r.item == "disk /"][0].status == "FAIL"


def test_freshness_on_vip_holder_only():
    f = facts(pg={"freshness": {"observe_state": 4000}})
    r = find(rows_for("flax_observe", "bang-gouda", f), "freshness")
    assert r and r[0].status == "FAIL"
    assert find(rows_for("flax_observe", "bang-edam", f), "freshness") == []


def test_unreachable_bang_is_one_unreachable_row():
    rows = host_rows("bang-edam", {"error": "ssh timeout", "unreachable": True}, {})
    assert [(r.item, r.status) for r in rows] == [("reach", "unreachable")]
    assert host_rows("bang-edam", {"error": "bad json"}, {})[0].status == "FAIL"
    assert component_rows("bang-edam", {"error": "timeout"}, COMPS, "bang-gouda", PINS,
                          {c: PAIR for c in COMPS}) == []


def test_optional_component_absent_is_info():
    rows = component_rows("bang-gouda", facts(), {"wan": COMPS["wan"]}, "bang-gouda", PINS, {"wan": PAIR})
    assert rows and {r.status for r in rows} == {"info"}


def test_rollup_one_row_per_component_worst_wins():
    rows = [Row("post-loose", "flax_post/flax-post", "ok", host="g", site="e"),
            Row("post-loose", "flax_post/flax-post-sol", "warn", "image 1 != pin 2", host="g", site="e"),
            Row("post-loose", "flax_post/port 8446", "ok", host="g", site="e")]
    out = rollup(rows)
    assert len(out) == 1 and out[0].status == "warn"
    assert out[0].item == "flax_post" and "flax-post-sol" in out[0].value


def test_rollup_keeps_host_items_whole():
    rows = [Row("host", "disk /export", "warn", "87%", host="g", site="e")]
    assert rollup(rows)[0].item == "disk /export"


def test_rollup_value_names_info_items():
    rows = [Row("infra", "wan/wan-failover", "info", "absent", host="g", site="e"),
            Row("infra", "wan/wlan-watchdog", "ok", "timer: last 5s ago", host="g", site="e")]
    assert "wan-failover" in rollup(rows)[0].value


def test_other_image_repo_is_not_compared_to_the_flax_pin():
    f = facts(containers={"flax-post-nginx": {"state": "running", "health": "", "image": "nginx:1.27-alpine"}})
    row = [r for r in find(rows_for("flax_post", "bang-gouda", f), "flax-post-nginx")
           if r.value.startswith("container:")][0]
    assert row.status == "ok" and "!= pin" not in row.value


def test_8444_belongs_to_flax_post():
    assert 8444 in COMPS["flax_post"].ports and 8444 not in COMPS["flax_console"].ports


def test_collector_errors_carry_the_reason_not_absent():
    f = facts(timers={"error": "RuntimeError: list-timers broke"},
              listen={"error": "RuntimeError: ss broke"}, crons={"error": "PermissionError: cron.d"})
    rows = component_rows("bang-gouda", f, {"health": COMPS["health"], "share_sync": COMPS["share_sync"]},
                          "bang-gouda", PINS, {"health": PAIR, "share_sync": PAIR})
    timer = [r for r in rows if "timer" in r.value or "list-timers" in r.value]
    assert timer and all("list-timers broke" in r.value for r in timer)
    port = [r for r in rows if r.item.startswith("health/port ")]
    assert port and all("ss broke" in r.value and r.status == "FAIL" for r in port)
    cron = [r for r in rows if "flax-share-sync" in r.item]
    assert cron and "cron.d" in cron[0].value


def test_freshness_reports_pg_error():
    f = facts(pg={"error": "RuntimeError: No such container: flax-stack-postgres-1"})
    r = find(rows_for("flax_observe", "bang-gouda", f), "freshness")[0]
    assert r.status == "FAIL" and "No such container" in r.value
