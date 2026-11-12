"""flax_post.finish: Finish order gatherer + rackpost-shaped archive payload."""
import os
import urllib.error

import pytest

from flax_post import finish

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "macinv")
MACINV_A = open(os.path.join(FIX, "node-a.verbose.txt")).read()      # FRU WTH4908267M1A, BMC 50:6b:4b:cd:5e:9c
SN, BMC = "WTH4908267M1A", "50:6b:4b:cd:5e:9c"


def _run(sn=SN, t=200, verdict="pass", rid="r2", compact=False):
    return {"bmc_mac": BMC, "serial": sn, "run_id": rid, "verdict": verdict, "finished_at": t,
            "port": "et8b1", "host_mac": "50:6b:4b:cd:5e:9a", "compact": compact,
            "result": {"qual": {"steps": {"fio": {"status": "done"}}}, "done": {"verdict": verdict},
                       "clean": True}}


def _arts(macinv=MACINV_A, extra=()):
    return lambda bmc, rid: [{"stage": "inventory", "name": "macinv-v", "content": macinv},
                             {"stage": "fio", "name": "fio.json", "content": "{}"}] + list(extra)


def _payload(mems, arts=None):
    return finish.build_payload("RRORD-100", mems, customer="Acme", sales="S-1", site="eindhoven",
                                get_artifacts=arts or _arts(), closed_at="2026-11-06T06:00:00+00:00")


def test_members_are_every_node_done_under_the_order():
    """Operator 2026-11-06: every node Done under the RRORD, however long ago."""
    old = dict(_run(sn="SN-OLD", t=10, rid="o1"), port=None)          # long gone, Done in September
    mems, left, _ = finish.members([old, _run(t=200, rid="r2")])
    assert {m["serial"] for m in mems} == {"SN-OLD", SN} and left == []


def test_latest_run_decides_a_later_failure_leaves_the_node_out():
    """Operator 2026-11-06: only nodes whose LATEST run under the order is Done."""
    mems, left, _ = finish.members([_run(t=100, rid="r1"), _run(t=300, verdict="fail", rid="r3")])
    assert mems == [] and [(l["run_id"], l["why"]) for l in left] == [("r3", "fail")]


def test_latest_run_done_after_an_earlier_failure_is_a_member():
    mems, left, _ = finish.members([_run(t=100, verdict="fail", rid="r1"), _run(t=300, rid="r3")])
    assert [m["run_id"] for m in mems] == ["r3"] and left == []


def test_node_never_done_is_left_out_never_a_member():
    mems, left, _ = finish.members([_run(sn="SN-FAIL", verdict="fail", rid="f1"), _run()])
    assert [m["serial"] for m in mems] == [SN]
    assert [(l["serial"], l["why"]) for l in left] == [("SN-FAIL", "fail")]


def test_member_without_serial_is_listed_not_archived():
    mems, left, no = finish.members([_run(sn=None, rid="x"), _run(sn="To be filled by O.E.M.", rid="y"), _run()])
    assert [m["serial"] for m in mems] == [SN] and {r["run_id"] for r in no} == {"x", "y"}


def test_running_lists_present_not_done_nodes_only():
    slots = [{"port": "et10b3", "serial": "SN-RUN", "order_no": "RRORD-100", "phase": "Qualify"},
             {"port": "et7b1", "serial": SN, "order_no": "RRORD-100", "phase": "Qualify"},
             {"port": "et9b1", "serial": "SN-OTHER", "order_no": "RRORD-7", "phase": "Qualify"}]
    assert finish.running("RRORD-100", slots, {SN}) == [{"port": "et10b3", "serial": "SN-RUN", "why": "running"}]


def test_payload_shape_matches_rackpost():
    from flax_control import order_archive
    p = _payload([_run()])
    assert set(p) == {"order_id", "rack", "customer", "site", "sales", "closed_at", "order_dir",
                      "closed_by", "devices", "macs", "components", "rungs", "artifacts"}
    assert p["closed_by"] == "post" and p["devices"][0]["pos"] == SN
    assert p["devices"][0]["hostname"] == "rr100-et8b1"
    order_archive._rows(p)


def test_components_blank_when_inventory_identity_mismatches():
    p = _payload([_run(sn="SN9-OTHER")])
    assert p["components"] == []
    p2 = _payload([_run()])
    assert p2["components"] and all(c["pos"] == SN for c in p2["components"])


def test_unsafe_artifact_name_not_stored_but_rung_keeps_path():
    from flax_control import order_archive
    p = _payload([_run()], _arts(extra=[{"stage": "fio", "name": "a b.txt", "content": "x"}]))
    bad = f"{SN}/r2/fio/a b.txt"
    assert bad not in [a["path"] for a in p["artifacts"]]
    assert any(bad in r["artifacts"] for r in p["rungs"])
    order_archive._rows(p)


def test_oversize_artifact_too_large():
    big = "x" * (finish.MAX_FILE + 1)
    p = _payload([_run()], _arts(extra=[{"stage": "sel", "name": "big.txt", "content": big}]))
    a = next(a for a in p["artifacts"] if a["path"].endswith("big.txt"))
    assert a["status"] == "too_large" and a["size"] == len(big)


def test_safe_path_matches_order_archive():
    from flax_control import order_archive
    assert finish.SAFE_PATH.pattern == order_archive._PATH_RE.pattern


def test_send_maps_409_and_unreachable():
    import io

    def r409(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 409, "conflict", {}, io.BytesIO(b'{"error":"closed by rackpost"}'))

    def down(req, timeout=None):
        raise urllib.error.URLError("refused")
    with pytest.raises(finish.FinishError) as e:
        finish.send({}, opener=r409)
    assert e.value.status == 409 and "rackpost" in e.value.message
    with pytest.raises(finish.FinishError) as e:
        finish.send({}, opener=down)
    assert e.value.status == 502


# --- the three customer CSVs as order documents (operator 2026-11-06) ------------
def _docs(p):
    import base64, gzip
    return {a["path"]: gzip.decompress(base64.b64decode(a["gzip_b64"])).decode()
            for a in p["artifacts"] if "/" not in a["path"]}


def test_finish_attaches_the_three_rackpost_named_csvs():
    from flax_control import order_archive
    p = _payload([_run()])
    docs = _docs(p)
    assert set(docs) == {"Acme-RRORD-100-S-1-devices.csv", "Acme-RRORD-100-S-1-firmware.csv",
                         "Acme-RRORD-100-S-1-inventory.csv"}
    assert docs["Acme-RRORD-100-S-1-devices.csv"].splitlines()[0] == ",".join(finish.DEVICE_COLUMNS)
    assert f"{SN},server,Tioga Pass Single Side,{BMC}" in docs["Acme-RRORD-100-S-1-devices.csv"]
    assert all(a["content_type"] == "text/csv" for a in p["artifacts"] if a["path"].endswith(".csv"))
    order_archive._rows(p)


def test_inventory_csv_rows_only_for_identity_matched_inventory():
    ok = _docs(_payload([_run()]))["Acme-RRORD-100-S-1-inventory.csv"].splitlines()
    assert len(ok) > 1 and ok[1].startswith(f"{SN},")
    bad = _docs(_payload([_run(sn="SN9-OTHER")]))["Acme-RRORD-100-S-1-inventory.csv"].splitlines()
    assert bad == [",".join(finish.COMPONENT_COLUMNS)]


def test_csv_names_without_sales_and_filename_safe():
    assert finish.csv_names("O V/H", "RRORD-101", None)["devices"] == "O_V_H-RRORD-101-devices.csv"


def test_firmware_csv_bmc_version_is_the_version_token():
    leo = MACINV_A.replace("BMC Version:", "BMC Version-old:") + "\nBMC Version:  14.00  Quanta Computer Inc.\n"
    r = _run(); r["result"]["fw"] = {"bmc": {"current": "report-only"}}
    fw = _docs(_payload([r], _arts(macinv=leo)))["Acme-RRORD-100-S-1-firmware.csv"]
    assert ",14.00," in fw and "Quanta" not in fw


# --- operator 2026-11-11: loose-node CSVs are keyed by serial (pos/ou are useless for a loose
# order: a slot is wherever the blade sat), device `kind` = the node_config profile, and the
# inventory names the device serial first and the part's own serial as part_serial ---
def test_csv_columns_drop_pos_and_ou_and_lead_with_serial():
    assert finish.DEVICE_COLUMNS[0] == "serial" and finish.FIRMWARE_COLUMNS[0] == "serial"
    for cols in (finish.DEVICE_COLUMNS, finish.FIRMWARE_COLUMNS, finish.COMPONENT_COLUMNS):
        assert "pos" not in cols and "ou" not in cols
    assert finish.COMPONENT_COLUMNS == ("serial", "kind", "model", "part_serial", "slot")


def test_csv_kind_is_the_node_config_profile():
    r = _run()
    r["result"]["pop"] = {"profile": "tec6-compute-tp", "verdict": "green", "failed_rules": []}
    docs = _docs(_payload([r]))
    assert f"{SN},tec6-compute-tp,Tioga Pass Single Side,{BMC}" in \
        docs["Acme-RRORD-100-S-1-devices.csv"]
    assert docs["Acme-RRORD-100-S-1-firmware.csv"].splitlines()[1].startswith(f"{SN},tec6-compute-tp,")


def test_csv_kind_without_a_profile_stays_server():
    docs = _docs(_payload([_run()]))
    assert f"{SN},server,Tioga Pass Single Side," in docs["Acme-RRORD-100-S-1-devices.csv"]


def test_inventory_csv_rows_name_the_device_serial_then_the_part():
    lines = _docs(_payload([_run()]))["Acme-RRORD-100-S-1-inventory.csv"].splitlines()
    assert lines[0] == "serial,kind,model,part_serial,slot"
    assert len(lines) > 1
    assert all(l.split(",")[0] == SN and l.split(",")[1] in ("cpu", "memory", "nic", "storage", "gpu")
               for l in lines[1:])
