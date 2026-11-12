# tests/test_flax_post_stepinfo.py
"""stepinfo.detail: the step modal's human-readable evidence (ruling 2026-09-12)."""
from flax_post import stepinfo

_LAD = {"rung": "qualify", "since": 1789178546.0, "power_on_at": 1789178324.0, "power_on_pending": False,
        "marks": {"tftp": 1789178342.0, "ipxe": 1789178409.0, "iso": 1789178482.0, "ping": 1789178486.0,
                  "ssh": 1789178535.0}, "fault": None, "probe_calls": {"fwd": 1789178330.0, "biosd": 1789178536.0},
        "last_power_attempt": 1789178324.0}
_REC = {"port": "et6b1", "switch": "rabbit-edam", "link": "link", "macs_seen": ["aa:bb", "cc:dd"],
        "bmc_mac": "aa:bb", "host_mac": "cc:dd", "bmc_ip": "172.25.6.101", "host_ip": "172.25.6.1",
        "bmc_leased": True, "host_leased": True, "bmc_pinged": True, "power_on": "on", "watts": "210 W",
        "serial": "SN1", "verdict": None, "launch_at": 1789178547.0,
        "ladder": _LAD,
        "ladder_view": {"rung": "qualify", "since": 1789178546.0, "budget_s": None,
                        "power_on_at": 1789178324.0, "marks": {k: v for k, v in _LAD["marks"].items()},
                        "skipped": None, "fault": None},
        "fw": {"bmc": {"phase": "up_to_date", "current": "flax-onetree-1.1.2", "target": "flax-onetree-1.1.2", "mode": "detect"},
               "bios": {"phase": "needs_update", "current": "TPC_P26C", "target": "TPC_P26F", "mode": "detect", "fault_reason": ""},
               "nic": {"phase": "up_to_date", "devices": [{"pci": "5e:00.0", "psid": "MT_0000000012", "current": "14.32.1912",
                                                          "target": "14.32.1912", "phase": "up_to_date"}]}},
        "steps": {"Discover": {"tftp-seen": "done", "power-on": "done", "host-ssh": "done"},
                  "Firmware": {"bmc-updated": "done", "bios-updated": "cur", "mlx-checked": "done"},
                  "Qualify": {"iperf": "done", "fio": "skip", "population-check": "fault", "mem-post": "done",
                              "agent-reachable": "done"},
                  "Done": {"power-off": "fault"}},
        "step_notes": {"fio": "no storage"}, "fault_notes": {}}
_ROW = {"qual": {"run_id": "r1", "agent": {"reachable": True, "ver": "1"},
                 "steps": {"iperf": {"status": "pass", "started": 1789178600.0, "ended": 1789178671.0,
                                     "summary": {"server": "bang-edam", "attempts": 3},
                                     "artifacts": [{"name": "iperf", "kind": "raw", "bytes": 900}]},
                           "mem-post": {"status": "pass", "summary": {"ce": 0, "ue": 0, "dimms": 12}},
                           "fio": {"status": "skip", "summary": {"reason": "no physical storage media"}}}},
        "pop": {"profile": "ovh-bare-tp", "verdict": "red", "failed_rules": ["12 Memory Size: 64 GB"]},
        "done": {"verdict": "pass", "identify": "done", "power_off": "fault", "power_off_reason": "still_on"}}


def _rows(d):
    return dict(d["rows"])


def test_boot_marker_shows_time_offset_and_evidence_source():
    d = stepinfo.detail(_REC, _ROW, "Discover", "tftp-seen")
    assert d["status"] == "done" and d["headline"] == "tftp-seen: passed"
    r = _rows(d)
    assert r["seen at"].endswith("Z") and r["after power-on"] == "+18s"
    assert "dnsmasq" in r["evidence"] and r["budget"] == "600 s"


def test_unknown_marker_explains_why_there_is_no_evidence():
    rec = dict(_REC, steps={"Discover": {"tftp-seen": "unknown"}},
               ladder_view=dict(_REC["ladder_view"], marks={}, skipped="power already on"),
               fault_notes={"tftp-seen": "skipped: power already on"})
    d = stepinfo.detail(rec, _ROW, "Discover", "tftp-seen")
    assert d["status"] == "unknown" and "no evidence collected" in d["headline"]
    assert _rows(d)["seen at"] == "not seen"
    assert any("already on when the worker first looked" in n for n in d["notes"])
    assert "skipped: power already on" in d["notes"]


def test_proven_marker_has_no_offset_before_power_on_and_explains_the_proof():
    """A proven marker predates power_on_at (the time the ladder first saw power on),
    so an "after power-on" offset would be negative."""
    marks = {"tftp": 1789375441.0, "ipxe": 1789375508.0, "iso": 1789375580.0, "kernel": 1789375563.5}
    rec = dict(_REC, steps={"Discover": {"tftp-seen": "done"}},
               ladder_view=dict(_REC["ladder_view"], power_on_at=1789375620.0, marks=marks,
                                skipped="power already on"))
    d = stepinfo.detail(rec, _ROW, "Discover", "tftp-seen")
    r = _rows(d)
    assert r["seen at"] == "2026-09-14 08:44:01Z" and "after power-on" not in r
    assert any("kernel" in n for n in d["notes"])


def test_fault_note_leads_the_notes():
    rec = dict(_REC, steps={"Discover": {"host-ssh": "fault"}}, fault_notes={"host-ssh": "no ssh after 360s"})
    d = stepinfo.detail(rec, _ROW, "Discover", "host-ssh")
    assert d["status"] == "fault" and d["headline"] == "host-ssh: FAILED"
    assert d["notes"][0] == "no ssh after 360s"
    assert _rows(d)["host IP"] == "172.25.6.1"


def test_power_on_rows():
    d = stepinfo.detail(_REC, _ROW, "Discover", "power-on")
    r = _rows(d)
    assert r["chassis power"] == "on" and r["engine power-on at"].endswith("Z") and r["awaiting power reading"] == "no"


def test_firmware_needs_update_explains_detect_mode():
    d = stepinfo.detail(_REC, _ROW, "Firmware", "bios-updated")
    r = _rows(d)
    assert d["status"] == "cur"
    assert r["current"] == "TPC_P26C" and r["target"] == "TPC_P26F" and r["daemon phase"] == "needs_update"
    assert any("detect mode" in n for n in d["notes"])
    assert r["last on-demand probe"].endswith("Z")


def test_firmware_nic_lists_each_card():
    d = stepinfo.detail(_REC, _ROW, "Firmware", "mlx-checked")
    r = _rows(d)
    assert r["cards"] == "1" and "14.32.1912 -> 14.32.1912" in r["card 5e:00.0"]


def test_firmware_latched_notes_the_freeze():
    rec = dict(_REC, verdict="pass")
    row = dict(_ROW, done=dict(_ROW["done"], steps={"Firmware": {"bmc-updated": "done"}}))
    d = stepinfo.detail(rec, row, "Firmware", "bmc-updated")
    assert any("frozen with the verdict" in n for n in d["notes"])


def test_qualify_step_shows_summary_timings_and_artifacts():
    d = stepinfo.detail(_REC, _ROW, "Qualify", "iperf")
    r = _rows(d)
    assert d["status"] == "done"
    assert r["agent status"] == "pass" and r["duration"] == "1m11s"
    assert r["server"] == "bang-edam" and r["attempts"] == "3" and r["artifacts"] == "iperf"
    assert any("throughput" in n for n in d["notes"])


def test_qualify_mem_post_shows_edac_counters():
    r = _rows(stepinfo.detail(_REC, _ROW, "Qualify", "mem-post"))
    assert r["ce"] == "0" and r["ue"] == "0" and r["dimms"] == "12"


def test_qualify_skip_leads_with_the_note():
    d = stepinfo.detail(_REC, _ROW, "Qualify", "fio")
    assert d["status"] == "skip" and d["notes"][0] == "no storage"
    assert _rows(d)["reason"] == "no physical storage media"


def test_population_check_lists_failed_rules():
    d = stepinfo.detail(_REC, _ROW, "Qualify", "population-check")
    assert d["status"] == "fault"
    assert ("missing", "12 Memory Size: 64 GB") in [tuple(x) for x in d["rows"]]
    assert _rows(d)["profile"] == "ovh-bare-tp"
    assert any("back to triage" in n for n in d["notes"])


def test_agent_reachable_rows():
    r = _rows(stepinfo.detail(_REC, _ROW, "Qualify", "agent-reachable"))
    assert r["agent reachable"] == "yes" and r["run"] == "r1" and r["launched at"].endswith("Z")


def test_done_power_off_fault_shows_reason():
    d = stepinfo.detail(_REC, _ROW, "Done", "power-off")
    r = _rows(d)
    assert d["status"] == "fault" and r["result"] == "fault" and r["reason"] == "still_on"
    assert r["chassis power now"] == "on"


def test_unknown_phase_or_step_is_still_well_formed():
    d = stepinfo.detail({"steps": {}}, {}, "Nope", "x")
    assert d == {"phase": "Nope", "step": "x", "status": "pending", "status_word": "not reached",
                 "headline": "x: not reached", "rows": [], "notes": []}


def test_done_step_shows_the_clean_mark_and_the_open_steps():
    rec = dict(_REC, verdict="pass", clean=False, holes={"Discover": ["tftp-seen", "ipxe-seen"]},
               steps=dict(_REC["steps"], Done={"done": "fault"}),
               fault_notes={"done": "run not clean: Discover: tftp-seen, ipxe-seen"})
    d = stepinfo.detail(rec, _ROW, "Done", "done")
    r = _rows(d)
    assert d["status"] == "fault" and d["notes"][0].startswith("run not clean")
    assert r["run clean"] == "no" and r["open in Discover"] == "tftp-seen, ipxe-seen"


def test_bmc_ready_rows_show_the_fru_identity_and_timing():
    rec = dict(_REC, steps={"Discover": {"bmc-ready": "done"}},
               ladder=dict(_LAD, bmc_fru={"board_mfg": "Wistron", "product": "Tioga Pass Single Side", "serial": "WTH1"},
                           marks=dict(_LAD["marks"], bmcready=1789178600.0)),
               ladder_view=dict(_REC["ladder_view"], marks=dict(_LAD["marks"], bmcready=1789178600.0)))
    d = stepinfo.detail(rec, _ROW, "Discover", "bmc-ready")
    r = _rows(d)
    assert d["status"] == "done" and r["board"] == "Wistron Tioga Pass Single Side" and r["serial (Product Serial)"] == "WTH1"
    assert r["after power-on"] == "+4m36s" and r["budget"] == "600 s"
    assert any("ipmitool fru" in n for n in d["notes"])


def _bmc_ready_rows(bmc_fru):
    rec = dict(_REC, steps={"Discover": {"bmc-ready": "cur"}},
               ladder=dict(_LAD, rung="bmc-ready", bmc_fru=bmc_fru))
    d = stepinfo.detail(rec, _ROW, "Discover", "bmc-ready")
    return d, _rows(d)


def test_bmc_ready_modal_absent_names_the_reason():
    d, r = _bmc_ready_rows({"state": "absent", "reason": "Device not present (Unknown (0x81))",
                            "serial": None, "board_mfg": None, "board_product": None})
    assert r["FRU 0"] == "not present: Device not present (Unknown (0x81))"
    assert r["serial (Product Serial)"] == "not read"
    assert any(n.startswith("the BMC answers but has no FRU 0") for n in d["notes"])


def test_bmc_ready_modal_no_serial_says_do_not_ship():
    d, r = _bmc_ready_rows({"state": "no_serial", "serial": None, "serial_field": "Product Serial",
                            "board_mfg": "Wistron", "board_product": "Tioga Pass Single Side",
                            "family": "tiogapass"})
    assert r["FRU 0"] == "no ship serial" and r["serial (Product Serial)"] == "empty"
    assert r["board"] == "Wistron Tioga Pass Single Side" and r["family"] == "tiogapass"
    assert any("population check fails it (do not ship)" in n for n in d["notes"])


def test_bmc_ready_modal_leopard_names_the_chassis_serial():
    d, r = _bmc_ready_rows({"state": "ok", "serial": "QCFF0617260650", "serial_field": "Chassis Serial",
                            "board_mfg": "Quanta", "board_product": "Leopard ORv2-DDR4", "family": "leopard"})
    assert r["serial (Chassis Serial)"] == "QCFF0617260650" and r["FRU 0"] == "ok"


def test_needs_update_in_detect_mode_reads_needed_not_in_progress():
    d = stepinfo.detail(_REC, _ROW, "Firmware", "bios-updated")          # bios needs_update, mode detect
    assert d["status"] == "cur" and d["status_word"] == "needed" and d["headline"] == "bios-updated: needed"
    rec = dict(_REC, fw=dict(_REC["fw"], bios=dict(_REC["fw"]["bios"], mode="enforce", phase="flashing")))
    d = stepinfo.detail(rec, _ROW, "Firmware", "bios-updated")
    assert d["status_word"] == "in progress"
    d = stepinfo.detail(_REC, _ROW, "Discover", "power-on")
    assert d["status_word"] == "passed"


def test_population_check_lists_blocked_dimms_as_blocked_rows():
    row = dict(_ROW, pop={"profile": "ovh-bare-tp", "verdict": "red",
                          "failed_rules": ["blocked DIMM in DIMM A0: Samsung M386A4G40DM0-CPB SN 39E16042 — cloned SPD"],
                          "blocked_dimms": [{"slot": "DIMM A0", "serial": "39E16042", "part": "M386A4G40DM0-CPB", "mfg": "Samsung", "reason": "cloned SPD"}]})
    d = stepinfo.detail(_REC, row, "Qualify", "population-check")
    assert ["BLOCKED", row["pop"]["failed_rules"][0]] in d["rows"]
    assert d["notes"][0].startswith("a blocked part is installed: 1 DIMM(s)")


def test_population_modal_no_ship_serial_note_comes_first():
    row = dict(_ROW, pop={"profile": None, "verdict": "red", "no_ship_serial": True,
                          "failed_rules": ["no ship serial: FRU 0 Product Serial empty — do not ship"]})
    d = stepinfo.detail(_REC, row, "Qualify", "population-check")
    r = _rows(d)
    assert r["NO SHIP"] == "no ship serial: FRU 0 Product Serial empty — do not ship"
    assert d["notes"][0] == "the blade has no ship serial in FRU 0: the node must not ship"


# --- ship checks: smbios-serial + tpm-state (spec 2026-10-29) ---------------
def _check_row(name, c):
    return dict(_ROW, checks={name: c})


def test_smbios_serial_modal_rows():
    c = {"status": "fail", "reason": "SN WTH1 in FRU 0, not in SMBIOS", "fru0_serials": ["WTH1"],
         "dmi_serials": {"System": "To be filled by O.E.M.", "Chassis": "CH1", "Base Board": None}}
    d = stepinfo.detail(_REC, _check_row("smbios-serial", c), "Qualify", "smbios-serial")
    r = _rows(d)
    assert r["FRU 0 serial"] == "WTH1" and r["SMBIOS system"] == "To be filled by O.E.M."
    assert r["SMBIOS chassis"] == "CH1" and r["SMBIOS base board"] == "none"
    assert r["reason"] == "SN WTH1 in FRU 0, not in SMBIOS"
    assert any("a fail goes back to triage" in n for n in d["notes"])


def test_tpm_state_modal_rows_tpm2():
    c = {"status": "fail", "reason": "TPM 2.0 storage hierarchy off", "device": True, "version": "2.0",
         "fields": {"owned": "0", "shEnable": "0", "ehEnable": "1"}, "raw": "owned=0 ... shEnable=0"}
    d = stepinfo.detail(_REC, _check_row("tpm-state", c), "Qualify", "tpm-state")
    r = _rows(d)
    assert (r["TPM device"], r["version"], r["owned"]) == ("present", "2.0", "no")
    assert (r["storage hierarchy"], r["endorsement hierarchy"]) == ("off", "on")
    assert r["state line"] == "owned=0 ... shEnable=0" and r["reason"] == "TPM 2.0 storage hierarchy off"
    assert "enabled" not in r
    assert any("whose wipe pass turns the TPM on" in n for n in d["notes"])


def test_tpm_state_modal_rows_tpm12():
    c = {"status": "pass", "reason": "", "device": True, "version": "1.2",
         "fields": {"owned": "0", "tpm": "1.2", "enabled": "1", "active": "1"}, "raw": "owned=0 tpm=1.2 enabled=1 active=1"}
    r = _rows(stepinfo.detail(_REC, _check_row("tpm-state", c), "Qualify", "tpm-state"))
    assert (r["enabled"], r["active"], r["owned"]) == ("yes", "yes", "no")
    assert "reason" not in r and "storage hierarchy" not in r


def test_tpm_state_modal_no_device():
    c = {"status": "fail", "reason": "no TPM device", "device": False, "version": None, "fields": {}, "raw": ""}
    r = _rows(stepinfo.detail(_REC, _check_row("tpm-state", c), "Qualify", "tpm-state"))
    assert r["TPM device"] == "absent" and r["reason"] == "no TPM device"


def test_ship_check_modal_without_result():
    d = stepinfo.detail(_REC, _ROW, "Qualify", "tpm-state")
    assert d["rows"] == [["result", "not judged yet"]]


def test_ship_check_modal_shows_checker():
    c = {"status": "pass", "reason": "", "device": True, "version": "1.2", "checker": "flax-post 0.9.189",
         "fields": {"owned": "0", "enabled": "1", "active": "1"}, "raw": "owned=0 tpm=1.2 enabled=1 active=1"}
    assert _rows(stepinfo.detail(_REC, _check_row("tpm-state", c), "Qualify", "tpm-state"))["checked by"] == "flax-post 0.9.189"


# ---- fio judge: the step modal's sorted table with flags ----
from flax_post import fio_judge
from flax_rack.fakes import _fio_job

_HDD = "ST6000NM019B-2TG103"
_HDD_ALLDISKS = "".join(
    f"/dev/{d} [0:0:{i}:0] Front_ArrayDevice0{i} mpt3sas rotational 6001.2 "
    f"SEAGATE SN04 {_HDD} WX01DSV{i}\n" for i, d in enumerate(("sda", "sdb", "sdc"), 2))
_HDD_BASE = {"suite": "v1", "groups": [
    {"model": _HDD[:16], "controller": "mpt3sas", "job": "lat", "metric": "lat_us",
     "n": 40, "mean": 5000.0, "std": 100.0}]}


def _fio_text():
    lat = {"sdc": "9000", "sda": "5010", "sdb": "4990"}     # artifact order is not table order
    return "\n".join(_fio_job(d, *j) for d, v in lat.items() for j in (
        ("bw", "read", "64.0KiB", 64, "3700", 231, 242, "69000", "usec", 70000, "usec"),
        ("lat", "randread", "4096B", 1, "190", 0.7, 0.8, v, "usec", 9000, "usec"),
        ("iops", "read", "4096B", 256, "60.1k", 230, 241, "4200", "usec", 5000, "usec")))


def _fio_case(judge=True):
    from flax_rungs import fio
    text = _fio_text()
    parsed = {}
    for dev, job, out in fio.sections(text):
        parsed.setdefault(dev, {})[job] = fio.parse_job(out)
    summ = {"devices": ["sdc", "sda", "sdb"], "smart_excluded": {}}
    if judge:
        j = fio_judge.judge(parsed, fio.disk_info(_HDD_ALLDISKS), _HDD_BASE)
        j["sdc"]["serial"] = "WX01DSV4"
        summ.update(judge=j, flagged=["sdc"])
    rec = dict(_REC, steps=dict(_REC["steps"], Qualify=dict(_REC["steps"]["Qualify"], fio="done")),
               step_notes={}, step_flags={"fio": "1 outside range"} if judge else {})
    row = {"qual": {"run_id": "r1", "steps": {"fio": {
        "status": "pass", "started": 1789178600.0, "ended": 1789178971.0, "summary": summ,
        "artifacts": [{"name": "fio", "kind": "raw", "bytes": len(text)}]}}}}
    arts = {("fio", "fio"): text, ("inventory", "alldisks"): _HDD_ALLDISKS}
    return rec, row, lambda stage, name: arts.get((stage, name))


def test_stepinfo_fio_table_and_flag_rows():
    rec, row, get = _fio_case()
    d = stepinfo.detail(rec, row, "Qualify", "fio", get_artifact=get)
    assert d["status"] == "done"                    # the verdict is untouched
    assert d["flag"] is True
    assert d["headline"] == "fio: passed · 1 drive outside range: WX01DSV4 (sdc) lat"
    t = d["table"]
    assert t["cols"][-2:] == ["expected", "check"]
    assert [(r[0], r[2].split(" ")[0]) for r in t["rows"]] == [
        (dv, jb) for jb in ("bw", "lat", "iops") for dv in ("sda", "sdb", "sdc")]
    assert t["row_cls"] == ["flag" if r[0] == "sdc" else "" for r in t["rows"]]
    lat_sdc = next(r for r in t["rows"] if r[0] == "sdc" and r[2].startswith("lat"))
    assert lat_sdc[-2:] == ["≤ 5200 µs", "outside"]
    r = _rows(d)
    assert r["outside range"].startswith("WX01DSV4 (sdc): lat lat_us 9000 vs expected ≤ 5200 µs")
    assert "judge" not in r and "flagged" not in r
    assert any("never a failure" in n for n in d["notes"])


def test_stepinfo_fio_no_baseline_is_a_headline_label_not_a_flag():
    rec, row, get = _fio_case()
    summ = row["qual"]["steps"]["fio"]["summary"]
    summ["judge"]["sdb"] = {"verdict": "no baseline", "reasons": [], "expected": {}}
    d = stepinfo.detail(rec, row, "Qualify", "fio", get_artifact=get)
    assert d["status"] == "done"
    assert d["headline"] == ("fio: passed · 1 drive outside range: WX01DSV4 (sdc) lat"
                             " · 1 drive no baseline")
    rec2 = dict(rec, step_flags={})
    summ["judge"] = {k: {"verdict": "no baseline", "reasons": [], "expected": {}}
                     for k in ("sda", "sdb", "sdc")}
    summ["flagged"] = []
    d = stepinfo.detail(rec2, row, "Qualify", "fio", get_artifact=get)
    assert d["headline"] == "fio: passed · no baseline" and not d.get("flag")


def test_stepinfo_fio_unjudged_run_still_shows_the_table():
    rec, row, get = _fio_case(judge=False)
    d = stepinfo.detail(rec, row, "Qualify", "fio", get_artifact=get)
    assert not d.get("flag") and d["headline"] == "fio: passed"
    assert d["table"]["cols"][-1] == "p99 µs" and "row_cls" not in d["table"]


def test_stepinfo_fio_without_artifacts_has_no_table():
    rec, row, _ = _fio_case()
    d = stepinfo.detail(rec, row, "Qualify", "fio")
    assert "table" not in d and d["flag"] is True
