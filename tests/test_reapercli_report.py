"""reaper report: everything reaper manages, to a file (spec §7.1, §9)."""
import json
from datetime import datetime, timezone

import pytest

pytest.importorskip("reapercli")
from reapercli.__main__ import Context  # noqa: E402
from reapercli.commands.report import report_dir, write_report  # noqa: E402
from reapercli.lib.extras import parse_extras  # noqa: E402
from reapercli.lib.out import Row  # noqa: E402

NOW = datetime(2026, 11, 7, 23, 5, 9, tzinfo=timezone.utc)
ROWS = [Row("ha", "vip", "ok", "bang-gouda", site="eindhoven"),
        Row("host", "disk /", "FAIL", "96%", "bang-gouda", "eindhoven"),
        Row("post-loose", "flax_post/flax-post", "warn", "image 1 != pin 2", "bang-gouda", "eindhoven")]


def ctx(home):
    return Context(root=home, home=home, hostname="bang-gouda")


def test_report_dir_on_dev_vm(tmp_path):
    (tmp_path / "git" / "reaper-devel").mkdir(parents=True)
    assert report_dir(ctx(tmp_path), "eindhoven", NOW) == \
        tmp_path / "git" / "reaper-devel" / "reports" / "eindhoven" / "20261107T230509Z"


def test_report_dir_on_a_bang(tmp_path):
    assert report_dir(ctx(tmp_path), "eindhoven", NOW) == tmp_path / "reaper-reports" / "eindhoven" / "20261107T230509Z"


def test_txt_summary_first_and_fails_before_ok(tmp_path):
    paths = write_report(ROWS, tmp_path, html=False)
    text = (tmp_path / "report.txt").read_text()
    first = [l for l in text.splitlines() if l.strip()][0]
    assert first.startswith("SUMMARY") and "1 FAIL" in first and "1 warn" in first
    assert text.index("FAIL") < text.index(" ok ")
    assert tmp_path / "report.json" in paths


def test_json_is_rows_only(tmp_path):
    write_report(ROWS, tmp_path, html=False)
    data = json.loads((tmp_path / "report.json").read_text())
    assert {"section", "item", "status", "value", "host", "site"} >= set().union(*data)
    assert any(r["item"] == "disk /" and r["status"] == "FAIL" for r in data)   # presence proof


def test_html_is_self_contained(tmp_path):
    write_report(ROWS, tmp_path, html=True)
    html = (tmp_path / "report.html").read_text()
    assert "<script src=" not in html and "http" not in html
    assert "disk /" in html and "96%" in html


def test_parse_extras():
    text = ('##ALERTS\n{"status":"success","data":{"alerts":['
            '{"labels":{"alertname":"BangPeerUnreachable","severity":"critical"},"state":"firing"},'
            '{"labels":{"alertname":"DiskFilling","severity":"warning"},"state":"pending"}]}}\n'
            "##SQL\nlease4|42\nhosts|310\nswitch|rabbit-gouda|t|3\nswitch|turtle-gouda|f|5000\n")
    rows = {r.item: r for r in parse_extras(text, "eindhoven")}
    assert rows["alert BangPeerUnreachable"].status == "FAIL"
    assert "alert DiskFilling" not in rows                 # pending, not firing
    assert rows["kea leases v4"].value == "42 active"
    assert rows["switch rabbit-gouda"].status == "ok"
    assert rows["switch turtle-gouda"].status == "FAIL"


def test_parse_extras_no_alerts():
    rows = parse_extras('##ALERTS\n{"status":"success","data":{"alerts":[]}}\n##SQL\n', "lavergne")
    assert [(r.item, r.status) for r in rows] == [("alerts", "ok")]


def test_alerts_are_this_sites_and_grouped():
    alerts = [{"labels": {"alertname": "DiskCapacityLow", "site": "braintree", "bang": "bang-fiesta",
                          "severity": "warning", "mountpoint": m}, "state": "firing"} for m in ("/", "/var")]
    alerts += [{"labels": {"alertname": "TextfileCollectorStale", "site": "eindhoven", "bang": "bang-edam",
                           "severity": "warning", "file": "vrrp_state.prom"}, "state": "firing"},
               {"labels": {"alertname": "TextfileCollectorStale", "site": "eindhoven", "bang": "bang-gouda",
                           "severity": "warning", "file": "vrrp_state.prom"}, "state": "firing"},
               {"labels": {"alertname": "NoSiteLabel", "severity": "warning"}, "state": "firing"}]
    text = "##ALERTS\n" + json.dumps({"status": "success", "data": {"alerts": alerts}}) + "\n##SQL\n"
    rows = [r for r in parse_extras(text, "eindhoven") if r.section == "alerts"]
    items = sorted(r.item for r in rows)
    assert items == ["alert NoSiteLabel", "alert TextfileCollectorStale"]
    stale = [r for r in rows if r.item == "alert TextfileCollectorStale"][0]
    assert "x2" in stale.value and "bang-edam" in stale.value and "bang-gouda" in stale.value
