# tests/test_post_fleet_viewer.py
"""scripts/post_fleet_viewer.py: node drilldown and artifact pages render from
fake rows (spec 2026-09-11-post-completion-latch §3.1). The script is stdlib-only
and not a package, so it is loaded from its path."""
import gzip
import json
import importlib.util
import pathlib


_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PATH = _ROOT / "scripts" / "post_fleet_viewer.py"
_spec = importlib.util.spec_from_file_location("post_fleet_viewer", _PATH)
pfv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pfv)

_NODE = {"bmc_mac": "aa:bb", "serial": "SN1", "host_mac": "cc:dd", "order_no": "RRORD-100",
         "last_switch": "rabbit-edam", "last_port": "et6b1", "customer": None,
         "vars": {"result": {"run_id": "r1", "verdict": "pass", "finished_at": 1789000000,
                             "order_no": "RRORD-100", "port": "et6b1",
                             "fw": {"bmc": {"current": "1.1.2", "target": "1.1.2", "phase": "up_to_date"},
                                    "bios": {"current": "TPC_P26F", "target": "TPC_P26F", "phase": "up_to_date"},
                                    "nic": {"devices": [{"pci": "5e:00.0", "current": "14.32.1912",
                                                         "target": "14.32.1912", "phase": "up_to_date"}]}},
                             "pop": {"profile": "ovh-bare-tp", "verdict": "red",
                                     "failed_rules": ["BIOS Version: TPC_P26C"]},
                             "done": {"verdict": "pass", "power_off": "done", "identify": "done"}},
                  "runs": [{"run_id": "r1", "verdict": "pass", "finished_at": 1789000000,
                            "order_no": "RRORD-100", "port": "et6b1"}]}}
_ARTS = [{"id": "7", "run_id": "r1", "stage": "iperf", "name": "iperf", "kind": "raw", "bytes": "928"}]


def test_node_page_shows_result_runs_and_artifact_links(monkeypatch):
    monkeypatch.setattr(pfv, "fetch_node", lambda mac: _NODE if mac == "aa:bb" else None)
    monkeypatch.setattr(pfv, "fetch_run_artifacts", lambda mac: _ARTS)
    body = pfv.render_node_page("aa:bb")
    assert "SN1" in body and "TPC_P26F" in body and "14.32.1912" in body
    assert "BIOS Version: TPC_P26C" in body
    assert "r1" in body and 'href="/artifact?id=7"' in body
    assert "power_off=done" in body


def test_node_page_unknown_mac(monkeypatch):
    monkeypatch.setattr(pfv, "fetch_node", lambda mac: None)
    assert "no post_node row" in pfv.render_node_page("zz:zz")


def test_node_page_without_result_still_renders_identity(monkeypatch):
    monkeypatch.setattr(pfv, "fetch_node", lambda mac: dict(_NODE, vars={"sel": []}))
    monkeypatch.setattr(pfv, "fetch_run_artifacts", lambda mac: [])
    body = pfv.render_node_page("aa:bb")
    assert "SN1" in body and "no finished run" in body


def test_artifact_page_escapes_content(monkeypatch):
    monkeypatch.setattr(pfv, "fetch_artifact", lambda aid: {
        "id": "7", "bmc_mac": "aa:bb", "serial": "SN1", "run_id": "r1", "stage": "iperf",
        "name": "iperf", "kind": "raw", "content": "<b>bw</b> 9.4 Gbits"})
    body = pfv.render_artifact_page("7")
    assert "&lt;b&gt;bw&lt;/b&gt;" in body and "iperf" in body


def test_artifact_page_rejects_non_numeric_id():
    # fetch_artifact refuses to build SQL from a non-numeric id before any I/O
    assert pfv.fetch_artifact("7; drop table") is None


def test_sql_lit_restricts_charset():
    assert pfv._sql_lit("aa:bb'; --") == "aa:bb--"


def test_post_node_view_has_link_column():
    cols = dict(pfv.VIEWS["post_node"]["columns"])
    assert "link" in cols and "link" in pfv.VIEWS["post_node"]["default"]
    assert pfv._cell("link", "/node?mac=aa:bb") == '<a href="/node?mac=aa:bb">open</a>'


def test_fleet_and_node_views_carry_verdict_columns():
    fleet = dict(pfv.VIEWS["fleet"]["columns"])
    for c in ("verdict", "finished", "slot_verdict", "pop", "power_on", "link"):
        assert c in fleet, c
    assert fleet["verdict"] == "Verdict"                # the recorded one, no qualifier
    assert "verdict" in pfv.VIEWS["fleet"]["default"]
    assert "slot_verdict" not in pfv.VIEWS["fleet"]["default"]
    node = dict(pfv.VIEWS["post_node"]["columns"])
    assert node["verdict"] == "Verdict" and "finished" in node
    assert "verdict" in pfv.VIEWS["post_node"]["default"]


def test_verdict_cells_render_as_pills():
    assert pfv._cell("verdict", "pass") == '<span class="pill good">pass</span>'
    assert pfv._cell("verdict", "fail") == '<span class="pill bad">fail</span>'
    assert pfv._cell("slot_verdict", "fail") == '<span class="pill bad">fail</span>'
    assert pfv._cell("pop", "red") == '<span class="pill bad">red</span>'
    assert pfv._cell("verdict", "") == '<span class="pill dim">—</span>'


def test_fetch_fleet_verdict_is_the_recorded_one(monkeypatch):
    """The slot row says fail (an old, untrusted verdict); post_node has no record
    for that blade -> Verdict is blank; the slot's value is only in slot_verdict."""
    monkeypatch.setattr(pfv, "fetch_post_state", lambda: [
        {"port": "et8b4", "switch": "rabbit-edam", "serial": "SN1", "bmc_mac": "aa:bb",
         "order_no": "RRORD-100", "updated_at": "t", "verdict": "fail", "pop": "red",
         "power_on": "off"},
        {"port": "et8b3", "switch": "rabbit-edam", "serial": "SN2", "bmc_mac": "cc:dd",
         "order_no": "RRORD-100", "updated_at": "t", "verdict": "pass", "pop": "green",
         "power_on": "off"}])
    monkeypatch.setattr(pfv, "fetch_post_node", lambda: [
        {"bmc_mac": "cc:dd", "host_mac": "ee:ff", "verdict": "pass", "finished": "2026-09-11 12:00:00+00"}])
    monkeypatch.setattr(pfv, "_cached", lambda key, fn: {} if key in ("bios_json", "bmc_json") else fn())
    rows = {r["port"]: r for r in pfv.fetch_fleet()}
    assert rows["et8b4"]["verdict"] == "" and rows["et8b4"]["slot_verdict"] == "fail"
    assert rows["et8b3"]["verdict"] == "pass" and rows["et8b3"]["finished"].startswith("2026-09-11")
    assert rows["et8b3"]["link"] == "/node?mac=cc%3Add"


# --- exact ID-list search (whitespace-separated, indexed, ledger-banded) -----
_IDROWS = [
    {"port": "et6b1", "serial": "WTH49152Z3M1A", "bmc_mac": "98:03:9b:a6:fd:fc",
     "host_mac": "98:03:9b:a6:fd:fa", "verdict": "pass"},
    {"port": "et24b2", "serial": "WTH4928342M1A", "bmc_mac": "98:03:9b:75:27:b6",
     "host_mac": "98:03:9b:75:27:b4", "verdict": "fail"},
    # same serial on a second BMC: the mezz-move case, one ID -> two rows
    {"port": "et9b9", "serial": "WTH4928342M1A", "bmc_mac": "98:03:9b:c6:49:c6",
     "host_mac": "98:03:9b:c6:49:c4", "verdict": ""},
]


def test_id_search_gives_every_token_its_own_index_in_input_order():
    out = pfv._id_search(_IDROWS, "WTH49152Z3M1A NOPE WTH4928342M1A")
    assert [i for i, _tok, _hits in out] == [1, 2, 3]
    assert [tok for _i, tok, _hits in out] == ["WTH49152Z3M1A", "NOPE", "WTH4928342M1A"]


def test_id_search_uniquifies_repeated_tokens():
    """A repeated ID is looked up once; indices renumber over the unique list."""
    out = pfv._id_search(_IDROWS, "WTH49152Z3M1A WTH49152Z3M1A")
    assert [(i, tok) for i, tok, _h in out] == [(1, "WTH49152Z3M1A")]


def test_id_search_uniquifies_on_the_normalised_form_not_the_raw_text():
    """Same ID spelled three ways -- case, and a MAC with/without separators."""
    out = pfv._id_search(_IDROWS, "WTH49152Z3M1A wth49152z3m1a")
    assert len(out) == 1 and out[0][1] == "WTH49152Z3M1A"   # first spelling kept
    out = pfv._id_search(_IDROWS, "98:03:9b:a6:fd:fc 98039BA6FDFC")
    assert len(out) == 1 and out[0][1] == "98:03:9b:a6:fd:fc"


def test_id_search_renumbers_contiguously_after_dropping_duplicates():
    out = pfv._id_search(_IDROWS, "WTH49152Z3M1A NOPE WTH49152Z3M1A WTH4928342M1A")
    assert [i for i, _t, _h in out] == [1, 2, 3]            # no gap where the dup was
    assert [t for _i, t, _h in out] == ["WTH49152Z3M1A", "NOPE", "WTH4928342M1A"]


def test_id_search_reports_how_many_duplicates_it_dropped():
    assert pfv._id_dupes("WTH49152Z3M1A wth49152z3m1a NOPE NOPE NOPE") == 3
    assert pfv._id_dupes("WTH49152Z3M1A NOPE") == 0


def test_id_search_is_exact_not_substring():
    assert pfv._id_search(_IDROWS, "WTH49152")[0][2] == []
    assert pfv._id_search(_IDROWS, "49152Z3M1A")[0][2] == []


def test_id_search_normalises_case_and_mac_separators():
    for tok in ("98:03:9b:a6:fd:fc", "98039BA6FDFC", "98-03-9b-a6-fd-fc",
                "wth49152z3m1a"):
        hits = pfv._id_search(_IDROWS, tok)[0][2]
        assert len(hits) == 1 and hits[0]["port"] == "et6b1", tok


def test_id_search_matches_host_mac_too_but_not_other_fields():
    assert len(pfv._id_search(_IDROWS, "98:03:9b:a6:fd:fa")[0][2]) == 1
    assert pfv._id_search(_IDROWS, "et6b1")[0][2] == []      # port is not an ID
    assert pfv._id_search(_IDROWS, "pass")[0][2] == []       # nor is a verdict


def test_id_search_not_found_keeps_its_index_with_no_rows():
    out = pfv._id_search(_IDROWS, "NOPE WTH49152Z3M1A")
    assert out[0] == (1, "NOPE", [])
    assert out[1][0] == 2 and len(out[1][2]) == 1


def test_id_search_one_token_can_match_several_rows():
    i, tok, hits = pfv._id_search(_IDROWS, "WTH4928342M1A")[0]
    assert i == 1 and tok == "WTH4928342M1A"
    assert sorted(r["port"] for r in hits) == ["et24b2", "et9b9"]


def _tbody_rows(body):
    """(tr class, [cell inner html]) for each rendered row."""
    import re as _re
    tb = body.split("<tbody>")[1].split("</tbody>")[0]
    out = []
    for tr in _re.findall(r"<tr[^>]*>.*?</tr>", tb, _re.S):
        cls = (_re.match(r'<tr class="([^"]*)"', tr) or [None, ""])[1]
        out.append((cls, _re.findall(r"<td[^>]*>(.*?)</td>", tr, _re.S)))
    return out


def _id_table(monkeypatch, q, **kw):
    monkeypatch.setitem(pfv.VIEWS["fleet"], "fetch", lambda: _IDROWS)
    return pfv.render_table("fleet", ["port", "verdict"], q=q, ids=True, **kw)


def test_id_mode_leads_with_index_and_searched_id_columns(monkeypatch):
    body = _id_table(monkeypatch, "WTH49152Z3M1A")
    head = body.split("<thead>")[1].split("</thead>")[0]
    assert head.index("#") < head.index("Searched ID") < head.index("Switch port")
    cls, cells = _tbody_rows(body)[0]
    assert cells[0] == "1" and cells[1] == "WTH49152Z3M1A"


def test_id_mode_repeats_the_index_on_every_row_of_a_multi_match(monkeypatch):
    rows = _tbody_rows(_id_table(monkeypatch, "WTH49152Z3M1A WTH4928342M1A"))
    assert [c[0] for _cls, c in rows] == ["1", "2", "2"]
    assert [c[1] for _cls, c in rows] == ["WTH49152Z3M1A"] + ["WTH4928342M1A"] * 2


def test_id_mode_not_found_keeps_its_index_with_a_blank_line_and_red_id(monkeypatch):
    rows = _tbody_rows(_id_table(monkeypatch, "NOPE WTH49152Z3M1A"))
    (_c0, miss), (_c1, hit) = rows[0], rows[1]
    assert miss[0] == "1" and "miss" in _id_table(monkeypatch, "NOPE")
    assert miss[1] == "NOPE"
    assert all(c == "" for c in miss[2:]), miss      # blank matching-line
    assert hit[0] == "2" and hit[2] != ""            # the found row still has data


def test_id_mode_indices_never_skip_across_a_not_found(monkeypatch):
    rows = _tbody_rows(_id_table(monkeypatch, "NOPE WTH49152Z3M1A ALSONOPE"))
    assert [c[0] for _cls, c in rows] == ["1", "2", "3"]


def test_id_mode_bands_alternate_by_index_not_by_row(monkeypatch):
    rows = _tbody_rows(_id_table(monkeypatch, "WTH49152Z3M1A WTH4928342M1A NOPE"))
    bands = [cls for cls, _c in rows]
    assert bands[0] != bands[1]           # index 1 -> index 2 flips
    assert bands[1] == bands[2]           # both rows of index 2 share a band
    assert bands[3] == bands[0]           # index 3 flips back


def test_id_mode_reports_found_and_not_found_counts(monkeypatch):
    body = _id_table(monkeypatch, "WTH49152Z3M1A WTH4928342M1A NOPE")
    assert "2 of 3 IDs found" in body and "1 not found" in body
    assert "duplicate" not in body                      # nothing dropped, stay quiet


def test_id_mode_says_when_it_dropped_duplicates(monkeypatch):
    body = _id_table(monkeypatch, "WTH49152Z3M1A wth49152z3m1a NOPE")
    assert "2 of 2 IDs found" not in body               # NOPE is still a miss
    assert "1 of 2 IDs found" in body and "1 duplicate dropped" in body


def test_id_mode_unsorted_keeps_input_order(monkeypatch):
    rows = _tbody_rows(_id_table(monkeypatch, "WTH4928342M1A WTH49152Z3M1A"))
    assert [c[0] for _cls, c in rows] == ["1", "1", "2"]


# Sorting in ID mode reorders whole index GROUPS -- a multi-match ID's rows stay
# together -- and not-found rows always sink to the bottom, either direction.
# Ports sort naturally: et6b1 < et9b9 < et24b2.
def test_id_mode_sort_moves_groups_and_keeps_their_rows_together(monkeypatch):
    rows = _tbody_rows(_id_table(monkeypatch, "WTH4928342M1A WTH49152Z3M1A NOPE",
                                 sort_col="port", sort_dir="asc"))
    assert [c[2] for _cls, c in rows] == ["et6b1", "et9b9", "et24b2", ""]
    assert [c[0] for _cls, c in rows] == ["2", "1", "1", "3"]


def test_id_mode_sort_preserves_the_input_index_numbers(monkeypatch):
    rows = _tbody_rows(_id_table(monkeypatch, "WTH4928342M1A WTH49152Z3M1A",
                                 sort_col="port", sort_dir="asc"))
    assert [c[0] for _cls, c in rows] == ["2", "1", "1"]     # not renumbered 1,2,3


def test_id_mode_sort_pins_not_found_to_the_bottom_either_direction(monkeypatch):
    for direction in ("asc", "desc"):
        rows = _tbody_rows(_id_table(monkeypatch, "NOPE WTH49152Z3M1A ALSONOPE",
                                     sort_col="port", sort_dir=direction))
        misses = [c[1] for _cls, c in rows if c[2] == ""]
        assert misses == ["NOPE", "ALSONOPE"], direction     # input order among misses
        assert [c[1] for _cls, c in rows][0] == "WTH49152Z3M1A", direction


def test_id_mode_sort_desc_reverses_found_groups_only(monkeypatch):
    # input order here is et6b1, et24b2, et9b9 -- so desc output differs from it
    rows = _tbody_rows(_id_table(monkeypatch, "WTH49152Z3M1A WTH4928342M1A NOPE",
                                 sort_col="port", sort_dir="desc"))
    assert [c[2] for _cls, c in rows] == ["et24b2", "et9b9", "et6b1", ""]
    assert [c[0] for _cls, c in rows] == ["2", "2", "1", "3"]


def test_id_mode_bands_realternate_in_sorted_order(monkeypatch):
    rows = _tbody_rows(_id_table(monkeypatch, "WTH4928342M1A WTH49152Z3M1A NOPE",
                                 sort_col="port", sort_dir="asc"))
    bands = [cls for cls, _c in rows]
    assert bands[1] == bands[2]        # the two rows of index 1 still share a band
    assert bands[0] != bands[1] and bands[2] != bands[3]


def test_id_mode_ignores_artifact_search(monkeypatch):
    def boom(term):
        raise AssertionError("artifact search must not run in ID mode")
    monkeypatch.setattr(pfv, "fetch_artifact_hits", boom)
    body = _id_table(monkeypatch, "WTH49152Z3M1A", art=True)
    assert "Matched artifacts" not in body


def test_query_cap_admits_a_long_pasted_id_list():
    """48 pasted serials is ~670 chars; the old 200-char clip silently ate them."""
    ids = " ".join(f"WTH4915{n:03d}M1A" for n in range(48))
    assert len(ids) > 200
    assert pfv._clip_q(ids) == ids
    assert len(pfv._clip_q("x" * 99999)) < 99999      # still bounded


def test_id_checkbox_reflects_mode_and_disables_artifact_search(monkeypatch):
    monkeypatch.setitem(pfv.VIEWS["fleet"], "fetch", lambda: _IDROWS)
    on = pfv.render_page("fleet", ["port"], q="WTH49152Z3M1A", ids=True)
    import re as _re
    ids_box = _re.search(r'<input id="ids"[^>]*>', on).group(0)
    art_box = _re.search(r'<input id="art"[^>]*>', on).group(0)
    assert "checked" in ids_box and "disabled" in art_box

    off = pfv.render_page("fleet", ["port"], q="WTH49152Z3M1A", ids=False)
    ids_box = _re.search(r'<input id="ids"[^>]*>', off).group(0)
    art_box = _re.search(r'<input id="art"[^>]*>', off).group(0)
    assert "checked" not in ids_box and "disabled" not in art_box


def test_id_mode_band_colour_is_themed_in_light_and_dark(monkeypatch):
    monkeypatch.setitem(pfv.VIEWS["fleet"], "fetch", lambda: _IDROWS)
    page = pfv.render_page("fleet", ["port"], q="WTH49152Z3M1A", ids=True)
    assert page.count("--band:") == 2          # :root and the dark media block
    assert ".band-a td" in page and ".band-b td" in page
    assert ".sid.miss" in page


# ---- order archive ----

_ADEV = {"customer": "F4", "order_id": "RRORD-101", "rack": "", "pos": "14L", "kind": "tioga-pass",
         "hostname": "rr101-tp-swp5", "chassis_serial": "QTFCQ0AAA", "final_verdict": "deployed",
         "closed_at": "2026-10-30 12:00:00+00"}


def _fake_archive(monkeypatch, devs=(_ADEV,)):
    """Fake _psql_rows: records each query; answers by which table it reads. Search
    returns the device only when the query carries the colon-form MAC or the serial."""
    seen = []

    def fake(query, columns):
        seen.append(query)
        if "WHERE (" in query or "ILIKE" in query:
            hit = "88:e9:a4:77:de:38'::macaddr" in query or "QTFC" in query
            return [dict(d) for d in devs] if hit else []
        if "GROUP BY customer" in query:
            return [{"customer": "F4", "orders": "1", "devices": str(len(devs)), "last_closed": "2026-10-30"}]
        if "FROM rackpost.order_mac" in query:
            return [{"role": "bmc", "mac": "88:e9:a4:77:de:38"}]
        if "FROM rackpost.order_component" in query:
            return [{"kind": "dimm", "model": "M1", "serial": "DIMMSN1", "slot": "A1"}]
        if "FROM rackpost.order_rung" in query:
            return [{"stage": "qualify", "rung": "fio", "verdict": "pass", "finished_at": "t",
                     "artifacts": '["qualify/fio.log"]'}]
        if "FROM rackpost.order_artifact" in query:
            if "encode(content" in query:
                ok = "path='qualify/fio.log'" in query
                return [{"content_type": "text/plain",
                         "hex": gzip.compress(b"fio ok\n").hex()}] if ok else []
            rows = [{"path": "qualify/fio.log", "status": "stored", "size": "7"},
                    {"path": "F4-RRORD-101-devices.csv", "status": "stored", "size": "9"},
                    {"path": "process-report.html", "status": "missing", "size": ""}]
            return [r for r in rows if "position(" not in query or "/" not in r["path"]]
        if "FROM rackpost.order_device WHERE order_id" in query:
            return [dict(devs[0], site="", ou="14", part="", ha_mode="", order_dir="/x")]
        return [dict(d) for d in devs]

    monkeypatch.setattr(pfv, "_psql_rows", fake)
    return seen


def test_archive_search_by_component_serial(monkeypatch):
    seen = _fake_archive(monkeypatch)
    body = pfv.render_archive_page(q="QTFCQ0")
    assert "rr101-tp-swp5" in body and "/archive/device?order=RRORD-101&amp;rack=&amp;pos=14L" in body
    assert "c.serial ILIKE '%QTFCQ0%'" in seen[0]
    assert "no archived device matches" in pfv.render_archive_page(q="ZZZ")


def test_archive_search_mac_forms(monkeypatch):
    seen = _fake_archive(monkeypatch)
    for form in ("88e9a477de38", "88:E9:A4:77:DE:38", "88-e9-a4-77-de-38", "88e9.a477.de38"):
        assert "rr101-tp-swp5" in pfv.render_archive_page(q=form), form
        assert "m.mac='88:e9:a4:77:de:38'::macaddr" in seen[-1]


def test_archive_escapes_html(monkeypatch):
    _fake_archive(monkeypatch, [dict(_ADEV, hostname="<x>", customer="F4")])
    body = pfv.render_archive_page(customer="F4")
    assert "<x>" not in body and "&lt;x&gt;" in body
    assert "<img" not in pfv.render_archive_page(q="<img src=x>")


def test_archive_empty_ok(monkeypatch):
    monkeypatch.setattr(pfv, "_psql_rows", lambda q, c: [])
    assert "no archived orders" in pfv.render_archive_page()
    assert "no archived orders" in pfv.render_archive_page(customer="Nobody")


def test_archive_customer_and_device_pages(monkeypatch):
    _fake_archive(monkeypatch)
    assert "customer=F4" in pfv.render_archive_page()
    cust = pfv.render_archive_page(customer="F4")
    assert "order RRORD-101" in cust and "QTFCQ0AAA" in cust
    dev = pfv.render_archive_device_page("RRORD-101", "", "14L")
    for want in ("88:e9:a4:77:de:38", "DIMMSN1", "qualify/fio.log", "fio"):
        assert want in dev
    assert 'href="/archive"' in dev


def test_archive_device_identity_block(monkeypatch):
    """The archived identity read renders like the rackpost drawer: its title, rows and
    raw artifact paths, escaped; a device without one shows no identity section."""
    ident = json.dumps({"title": "Board Serial", "serial": "J49110C3ZB1",
                        "rows": [["Board Serial", "J49110C3ZB1"], ["IOM Location", "Side <A>"]],
                        "raw": {"fruid-util-iom": "artifacts/JBOD01.iom1/d9/serial/fruid.txt"}})
    _fake_archive(monkeypatch, [dict(_ADEV, identity=ident)])
    dev = pfv.render_archive_device_page("RRORD-101", "", "14L")
    assert "identity &middot; Board Serial" in dev
    assert "J49110C3ZB1" in dev and "Side &lt;A&gt;" in dev and "<A>" not in dev
    assert "artifacts/JBOD01.iom1/d9/serial/fruid.txt" in dev
    _fake_archive(monkeypatch)
    assert "identity &middot;" not in pfv.render_archive_device_page("RRORD-101", "", "14L")


def test_archive_sales_order_shown_and_searched(monkeypatch):
    seen = _fake_archive(monkeypatch, [dict(_ADEV, sales="S-ORD101031")])
    assert "sales order S-ORD101031" in pfv.render_archive_page(customer="F4")
    pfv.render_archive_page(q="S-ORD101031")
    assert "d.sales ILIKE '%S-ORD101031%'" in seen[-1]
    dev = pfv.render_archive_device_page("RRORD-101", "", "14L")
    assert "S-ORD101031" in dev
    _fake_archive(monkeypatch)
    assert "&middot; sales order" not in pfv.render_archive_page(customer="F4")


def test_archive_files_linked_and_served(monkeypatch):
    """040: stored files link to /archive/file, served from the DB; missing ones say so;
    the order's documents are listed on the customer page."""
    seen = _fake_archive(monkeypatch)
    dev = pfv.render_archive_device_page("RRORD-101", "", "14L")
    assert 'href="/archive/file?order=RRORD-101&amp;rack=&amp;path=qualify/fio.log"' in dev
    cust = pfv.render_archive_page(customer="F4")
    assert "path=F4-RRORD-101-devices.csv" in cust and "missing at close" in cust
    assert "qualify/fio.log" not in cust                 # documents only
    assert pfv.fetch_archive_file("RRORD-101", "", "qualify/fio.log") == ("text/plain", b"fio ok\n")
    n = len(seen)
    for bad in ("artifacts/../x", "a'b", "a b", "$(id)", 'a"b'):
        assert pfv.fetch_archive_file("RRORD-101", "", bad) is None
    assert len(seen) == n                       # never reached psql


def test_archive_marks_post_closed_orders(monkeypatch):
    _fake_archive(monkeypatch, [dict(_ADEV, closed_by="post")])
    cust = pfv.render_archive_page(customer="F4")
    assert "closed by post" in cust and "<td>post</td>" in cust
    _fake_archive(monkeypatch, [dict(_ADEV, closed_by="rackpost")])
    assert "closed by post" not in pfv.render_archive_page(customer="F4")


def test_archive_device_page_shows_closed_by(monkeypatch):
    _fake_archive(monkeypatch, [dict(_ADEV, closed_by="post")])
    dev = pfv.render_archive_device_page("RRORD-101", "", "14L")
    assert "closed_by" in dev and "serial (pos)" in dev
    _fake_archive(monkeypatch, [dict(_ADEV, closed_by="rackpost")])
    assert "serial (pos)" not in pfv.render_archive_device_page("RRORD-101", "", "14L")
