"""blades.build_slots: grid order, complete records, Discover steps (violet phase)."""
from flax_post import blades, geometry

_GEO = geometry.parse_geometry({
    "racks": [{"switch": "rabbit-edam", "tag": ""}],
    "slots": [
        {"port": "et6b1", "switch": "rabbit-edam", "bottom_ou": 12, "height": 2, "width": 3, "pos": "1:3", "chassis": 2},
        {"port": "et8b4", "switch": "rabbit-edam", "bottom_ou": 37, "height": 2, "width": 3, "pos": "1:3", "chassis": 4},
    ]})


def test_empty_slots_are_minimal():
    slots = blades.build_slots(_GEO["slots"], {}, {}, {"order_no": None, "population": None})
    et6 = next(s for s in slots if s["port"] == "et6b1")
    assert et6["empty"] is True
    assert set(et6) == {"port", "ou", "height", "width", "col", "group", "switch", "empty"}


def test_rack_order_top_group_first():
    slots = blades.build_slots(_GEO["slots"], {}, {}, {"order_no": None, "population": None})
    # real ports (ghost placeholders now fill the missing L/C/R columns): group 4
    # (ou 37) before group 2 (ou 12)
    assert [s["port"] for s in slots if s["port"]] == ["et8b4", "et6b1"]


def test_populated_record_is_complete():
    consumed = {"et6b1": {"link": "link", "bmc_mac": "aa:bb", "host_mac": "cc:dd",
                          "bmc_mac_seen": True, "host_mac_seen": True,
                          "bmc_ip": "172.24.6.101", "host_ip": "172.24.7.101",
                          "bmc_leased": True, "host_leased": True,
                          "bmc_reserved": True, "host_reserved": True}}
    st = {"et6b1": {"serial": "SN9", "power_on": "on", "watts": "210 W", "bmc_pinged": True}}
    settings = {"order_no": "RRORD-003", "population": "ovh-bare-tp"}
    blade = next(s for s in blades.build_slots(_GEO["slots"], consumed, st, settings)
                 if s["port"] == "et6b1")
    for k in ("serial", "bmc_mac", "host_mac", "bmc_ip", "host_ip", "bmc_leased",
              "host_leased", "power_on", "watts", "phase", "step", "steps",
              "order_no", "population", "pop_override", "sdr", "sel", "alerts",
              "updated_at"):
        assert k in blade, k
    assert blade["phase"] == "Discover"                  # violet phase while discovering
    assert blade["order_no"] == "RRORD-003"
    assert blade["population"] == "ovh-bare-tp"
    assert blade["steps"]["Discover"]["serial"] == "done"
    assert blade["steps"]["Discover"]["bmc-pinged"] == "done"


def test_discover_marks_first_incomplete_cur():
    consumed = {"et6b1": {"link": "link", "bmc_mac": "aa:bb", "bmc_mac_seen": True,
                          "bmc_leased": True, "bmc_reserved": True, "host_reserved": False}}
    st = {}                                              # no serial, no ping yet
    blade = next(s for s in blades.build_slots(_GEO["slots"], consumed, st,
                                               {"order_no": None, "population": None})
                 if s["port"] == "et6b1")
    steps = blade["steps"]["Discover"]
    assert steps["bmc-leased"] == "done"
    assert steps["bmc-pinged"] == "cur"
    assert steps["serial"] == "pending"


def test_statuses_strict_sequential_after_cur():
    # bmc-pinged is the first not-done step ("cur"); a LATER signal (serial) is
    # present but must still render "pending" so the step bar tracks the phase bar.
    consumed = {"et6b1": {"link": "link", "bmc_mac_seen": True,
                          "bmc_reserved": True, "bmc_leased": True}}
    st = {"et6b1": {"serial": "SNX"}}                     # serial set but bmc-pinged absent
    blade = next(s for s in blades.build_slots(_GEO["slots"], consumed, st,
                                               {"order_no": None, "population": None})
                 if s["port"] == "et6b1")
    steps = blade["steps"]["Discover"]
    assert steps["bmc-pinged"] == "cur"
    assert steps["serial"] == "pending"                  # NOT "done" despite serial set


def test_all_discover_done_marks_phase_firmware():
    # Phase now advances Discover -> Firmware (not Discover -> Done) once all Discover flags pass.
    consumed = {"et6b1": {"link": "link",
                          "bmc_mac_seen": True, "host_mac_seen": True,
                          "bmc_reserved": True, "host_reserved": True,
                          "bmc_leased": True, "host_leased": True}}
    st = {"et6b1": {"serial": "SN9", "bmc_pinged": True, "host_pinged": True, "power_on": "on"}}
    blade = next(s for s in blades.build_slots(_GEO["slots"], consumed, st,
                                               {"order_no": None, "population": None})
                 if s["port"] == "et6b1")
    assert blade["phase"] == "Firmware"
    assert blade["step"] is None
    # no ladder slice -> the boot markers fall back to "unknown": not a blocker
    # for the phase, but not green either
    assert all(v in blades._ADVANCES for v in blade["steps"]["Discover"].values())
    assert not blades.phase_done(blade["steps"]["Discover"])


# ---------------------------------------------------------------------------
# Firmware phase tests (Task 3)
# ---------------------------------------------------------------------------
_SLOT = {"port": "et6b1", "switch": "rabbit-edam", "ou": 12, "height": 2,
         "width": 3, "col": "L", "group": 2}
# a fully-discovered consumed dict so phase reaches Firmware
_DISCOVERED = {"link": "link", "bmc_mac_seen": True, "bmc_reserved": True,
               "bmc_leased": True, "host_mac_seen": True, "host_reserved": True,
               "host_leased": True, "bmc_mac": "aa", "host_mac": "bb"}


def _rec(fw_bmc, power_on="on", st_extra=None):
    st = {"bmc_pinged": True, "serial": "SN1", "host_pinged": True,
          "power_on": power_on, "fw_bmc": fw_bmc}
    st.update(st_extra or {})
    slots = blades.build_slots([_SLOT], {"et6b1": dict(_DISCOVERED)}, {"et6b1": st}, {})
    return slots[0]


def test_firmware_step_order_bmc_before_bios():
    assert blades.PHASE_STEPS["Firmware"] == (
        "bmc-checked", "bmc-updated", "bios-checked", "bios-updated", "mlx-checked", "mlx-updated")


def test_discover_steps_interleave_ladder_rungs_in_boot_order():
    assert blades.DISCOVER_STEPS == (
        "switchportlink", "bmc-mac-seen", "bmc-reserved", "bmc-leased", "bmc-pinged", "serial",
        "power-on", "tftp-seen", "ipxe-seen", "host-mac-seen", "host-reserved", "host-leased",
        "live-iso-seen", "host-pinged", "host-ssh", "bmc-ready")
    assert list(blades.PHASE_STEPS["Qualify"])[0] == "agent-reachable"
    assert list(blades.PHASE_STEPS["Qualify"])[-1] == "console"


def test_firmware_steps_dict_key_order_matches_phase_steps():
    # The UI renders steps in dict-key order (Object.keys), so _firmware_steps
    # must emit bmc-checked/bmc-updated BEFORE bios-checked/bios-updated.
    for st in ({"power_on": "on", "fw_bmc": {"phase": "up_to_date"}},
               {"power_on": "off"},
               {}):
        keys = list(blades._firmware_steps(st).keys())
        assert keys == list(blades.PHASE_STEPS["Firmware"]), keys
        assert keys.index("bmc-checked") < keys.index("bios-checked")


def test_bmc_up_to_date_marks_bmc_steps_done_phase_firmware():
    r = _rec({"phase": "up_to_date", "current": "1.0.8", "target": "1.0.8", "ver_class": "ver-ok"})
    fw = r["steps"]["Firmware"]
    assert fw["bmc-checked"] == "done" and fw["bmc-updated"] == "done"
    assert fw["bios-checked"] == "pending" and fw["bios-updated"] == "pending"
    assert r["phase"] == "Firmware"           # parks at Firmware (no bios producer yet)
    assert r["fw"]["bmc"]["ver_class"] == "ver-ok"


def test_bmc_oem_marks_bmc_steps_done():
    """An OEM board (reachable Redfish, nothing to flash) is terminal: bmc-checked
    AND bmc-updated read 'done' so the Firmware phase isn't stuck showing the BMC
    sub-step as perpetual attention (the old false-'unreachable' -> 'cur' behavior)."""
    r = _rec({"phase": "oem", "current": "1.17.0", "target": "", "fault_reason": ""})
    fw = r["steps"]["Firmware"]
    assert fw["bmc-checked"] == "done" and fw["bmc-updated"] == "done"


def test_bmc_flashing_marks_update_cur():
    r = _rec({"phase": "flashing", "current": "1.0.6", "target": "1.0.8", "percent": 40})
    fw = r["steps"]["Firmware"]
    assert fw["bmc-checked"] == "done" and fw["bmc-updated"] == "cur"


def test_bmc_fault_marks_update_fault():
    r = _rec({"phase": "fault", "current": "1.0.6", "target": "1.0.8", "fault_reason": "x"})
    assert r["steps"]["Firmware"]["bmc-updated"] == "fault"


def test_no_fw_slice_leaves_firmware_pending():
    r = _rec(None)
    fw = r["steps"]["Firmware"]
    assert fw["bmc-checked"] == "pending" and fw["bmc-updated"] == "pending"


def test_no_ladder_falls_back_to_live_power_for_power_on_step():
    d = _rec(None, power_on="off")["steps"]["Discover"]
    assert d["power-on"] == "cur"
    d = _rec(None, power_on="on")["steps"]["Discover"]
    assert d["power-on"] == "done"
    # without a ladder the boot markers are unknowable -> `unknown`: does not
    # hold the phase, but is NOT a completed step (the bar stays off green)
    assert d["tftp-seen"] == "unknown" and d["ipxe-seen"] == "unknown" and d["live-iso-seen"] == "unknown"


def test_ladder_drives_discover_steps():
    lad = {"rung": "live-iso-seen", "since": 1.0, "power_on_at": 1.0, "power_on_pending": False,
           "marks": {"tftp": 2.0, "ipxe": 3.0}, "fault": None, "probe_calls": {}}
    d = _rec(None, power_on="on", st_extra={"ladder": lad})["steps"]["Discover"]
    assert d["power-on"] == "done" and d["tftp-seen"] == "done" and d["ipxe-seen"] == "done"
    assert d["live-iso-seen"] == "cur"
    assert d["host-pinged"] == "pending" and d["host-ssh"] == "pending"


def test_ladder_fault_renders_fault_and_note():
    lad = {"rung": "tftp-seen", "since": 1.0, "power_on_at": 1.0, "power_on_pending": False,
           "marks": {}, "fault": {"rung": "tftp-seen", "reason": "no tftp after 600s", "at": 700.0},
           "probe_calls": {}}
    r = _rec(None, power_on="on", st_extra={"ladder": lad})
    assert r["steps"]["Discover"]["tftp-seen"] == "fault"
    assert r["fault_notes"]["tftp-seen"] == "no tftp after 600s"
    assert r["ladder"]["rung"] == "tftp-seen"


def test_ladder_skipped_markers_render_unknown_with_note():
    """Ruling 2026-09-12: a boot marker the ladder never observed ("power
    already on": observe restart, hold-gesture clear, operator power-on
    before the worker looked) is `unknown`, not `skip`. `skip` is a test that
    ran and found itself N/A (fio on a diskless blade) and completes the
    phase for the bar; `unknown` is evidence never collected and must not
    paint Discover green (et5b4, et6b3 2026-09-12)."""
    lad = {"rung": "host-pinged", "since": 1.0, "power_on_at": 1.0, "power_on_pending": False,
           "marks": {"skipped": "power already on"}, "fault": None, "probe_calls": {}}
    r = _rec(None, power_on="on", st_extra={"ladder": lad})
    d = r["steps"]["Discover"]
    assert d["tftp-seen"] == "unknown" and d["ipxe-seen"] == "unknown" and d["live-iso-seen"] == "unknown"
    assert r["fault_notes"]["tftp-seen"] == "skipped: power already on"


def test_proven_boot_markers_render_done_with_a_proof_note():
    """Already on at first sight, but the boot was proven at ssh (kernel boot
    between the iPXE and ISO fetches): the markers are completed steps."""
    lad = {"rung": "bmc-ready", "since": 12.0, "power_on_at": 10.0, "born_at": 1.0, "power_on_pending": False,
           "marks": {"skipped": "power already on", "tftp": 3.0, "ipxe": 4.0, "iso": 6.0, "kernel": 5.0,
                     "ping": 11.0, "ssh": 12.0}, "fault": None, "probe_calls": {}}
    r = _rec(None, power_on="on", st_extra={"ladder": lad})
    d = r["steps"]["Discover"]
    assert d["tftp-seen"] == "done" and d["ipxe-seen"] == "done" and d["live-iso-seen"] == "done"
    assert "kernel boot" in r["fault_notes"]["tftp-seen"]


def test_unknown_markers_do_not_hold_the_discover_phase():
    """The phase still advances past Discover with unknown markers (the ladder
    did walk on); only the bar's green is withheld."""
    lad = {"rung": "done", "since": 1.0, "power_on_at": 1.0, "power_on_pending": False,
           "marks": {"skipped": "power already on", "ping": 5.0, "ssh": 6.0}, "fault": None,
           "probe_calls": {}}
    consumed = {"et6b1": dict(_LATCH_CONSUMED["et6b1"], host_leased=True)}
    st = dict(_FW_DONE, serial="SN1", power_on="on", bmc_pinged=True, host_pinged=True,
              ladder=lad, qual={"steps": {}})
    b = next(s for s in blades.build_slots(_GEO["slots"], consumed, {"et6b1": st},
                                           {"order_no": None, "population": None})
             if s["port"] == "et6b1")
    assert b["steps"]["Discover"]["tftp-seen"] == "unknown"
    assert b["phase"] == "Qualify"
    assert not blades.phase_done(b["steps"]["Discover"])        # the bar rule: not green


def test_firmware_phase_uses_gate_needs_update_passes_in_detect():
    st_extra = {"host_pinged": True,
                "fw_bios": {"phase": "needs_update", "mode": "detect"},
                "fw_nic": {"phase": "up_to_date", "devices": [], "mode": "detect"},
                "ladder": {"rung": "qualify", "since": 1.0, "power_on_at": 1.0,
                           "power_on_pending": False, "marks": {"skipped": "power already on"},
                           "fault": None, "probe_calls": {}}}
    r = _rec({"phase": "up_to_date", "mode": "detect"}, st_extra=st_extra)
    assert r["fw_gates"] is True
    assert r["steps"]["Firmware"]["bios-updated"] == "cur"     # still visible
    assert r["phase"] == "Qualify"                              # but no longer parked


def test_firmware_phase_holds_when_gate_blocks():
    st_extra = {"fw_bios": {"phase": "unreachable"}, "fw_nic": {"phase": "up_to_date", "devices": []}}
    r = _rec({"phase": "up_to_date"}, st_extra=st_extra)
    assert r["fw_gates"] is False


def test_firmware_bios_steps_from_fw_bios_phase():
    st = {"power_on": "on", "fw_bmc": {"phase": "up_to_date"},
          "fw_bios": {"phase": "needs_update", "current": "TPC_P25C", "target": "TPC_P26C"}}
    steps = blades._firmware_steps(st)
    assert steps["bios-checked"] == "done" and steps["bios-updated"] == "cur"


def test_firmware_bios_up_to_date():
    st = {"power_on": "on", "fw_bmc": {"phase": "up_to_date"}, "fw_bios": {"phase": "up_to_date"}}
    steps = blades._firmware_steps(st)
    assert steps["bios-checked"] == "done" and steps["bios-updated"] == "done"


def test_firmware_steps_include_mlx_after_bios():
    keys = list(blades._firmware_steps({}).keys())
    assert keys == list(blades.PHASE_STEPS["Firmware"])
    assert keys[-2:] == ["mlx-checked", "mlx-updated"]


def test_mlx_steps_all_up_to_date_green():
    st = {"fw_nic": {"phase": "up_to_date", "devices": [
        {"current": "14.32.1010", "psid": "MT_x", "target": "14.32.1010", "target_psid": "MT_x",
         "uefi": True, "secure": False, "phase": "up_to_date"}]}}
    steps = blades._firmware_steps(st)
    assert steps["mlx-checked"] == "done" and steps["mlx-updated"] == "done"


def test_mlx_updated_red_on_unsupported():
    st = {"fw_nic": {"phase": "unsupported", "devices": [
        {"current": "1", "psid": "X", "phase": "unsupported"}]}}
    steps = blades._firmware_steps(st)
    assert steps["mlx-checked"] == "done" and steps["mlx-updated"] == "fault"


def test_mlx_no_slice_is_pending():
    steps = blades._firmware_steps({})
    assert steps["mlx-checked"] == "pending" and steps["mlx-updated"] == "pending"


def test_mlx_unreachable_is_attention():
    st = {"fw_nic": {"phase": "unreachable", "devices": []}}
    steps = blades._firmware_steps(st)
    assert steps["mlx-checked"] == "cur" and steps["mlx-updated"] == "pending"


def test_mlx_no_cards_stays_green():
    st = {"fw_nic": {"phase": "up_to_date", "devices": []}}
    steps = blades._firmware_steps(st)
    assert steps["mlx-checked"] == "done" and steps["mlx-updated"] == "done"


def test_mlx_flash_fault_phase_is_red():
    # Flash-lifecycle fault (nicd/service.py _flash_node sets phase="fault" on
    # e.g. "card reset did not take") leaves devices as the stale pre-flash
    # needs_update list. Without the phase=="fault" special-case, aggregate()
    # would read the stale device and return ("done", "cur") -- amber
    # "in progress" for a flash that actually failed.
    st = {"fw_nic": {"phase": "fault", "fault_reason": "card reset did not take",
                      "devices": [{"current": "14.20.1202", "psid": "FB_x",
                                   "target": "14.32.1010", "target_psid": "MT_x",
                                   "uefi": False, "phase": "needs_update"}]}}
    steps = blades._firmware_steps(st)
    assert steps["mlx-checked"] == "done" and steps["mlx-updated"] == "fault"


# ---------------------------------------------------------------------------
# Live switch overlay tests (Task 1)
# ---------------------------------------------------------------------------
def test_unreserved_linkup_port_renders_present_but_unreserved():
    # No reservation, no state, but switch_facts says the port is link-up with 2 macs.
    facts = {"Ethernet6/1": {"link": "link", "macs": ["AA:BB:CC:00:00:01", "aa:bb:cc:00:00:02"]}}
    blade = next(s for s in blades.build_slots(
        _GEO["slots"], {}, {}, {"order_no": None, "population": None}, facts)
        if s["port"] == "et6b1")
    assert not blade.get("empty")                       # populated because link-up
    assert blade["link"] == "link"
    assert blade["steps"]["Discover"]["switchportlink"] == "done"
    assert blade["macs_seen"] == ["aa:bb:cc:00:00:01", "aa:bb:cc:00:00:02"]
    assert blade["bmc_mac"] is None and blade["host_mac"] is None
    # first unmet step: switchportlink is done, bmc-mac-seen is the next
    # DISCOVER_STEPS entry and is unmet (no reservation), so it goes "cur".
    assert blade["steps"]["Discover"]["bmc-mac-seen"] == "cur"


def test_unreserved_linkdown_port_stays_empty():
    facts = {"Ethernet6/1": {"link": "nolink", "macs": []}}
    blade = next(s for s in blades.build_slots(
        _GEO["slots"], {}, {}, {"order_no": None, "population": None}, facts)
        if s["port"] == "et6b1")
    assert blade["empty"] is True


def test_switchportlink_is_live_regardless_of_reservation():
    # Reserved port but switch_facts says nolink -> switchportlink must be 'cur', not 'done'.
    consumed = {"et6b1": {"link": "link", "bmc_mac": "aa:bb", "bmc_reserved": True,
                          "bmc_mac_seen": True, "host_reserved": False}}
    facts = {"Ethernet6/1": {"link": "nolink", "macs": []}}
    blade = next(s for s in blades.build_slots(
        _GEO["slots"], consumed, {}, {"order_no": None, "population": None}, facts)
        if s["port"] == "et6b1")
    assert blade["link"] == "nolink"
    assert blade["steps"]["Discover"]["switchportlink"] == "cur"


def test_reserved_record_backward_compatible_without_switch_facts():
    # No switch_facts passed -> live_link falls back to consumed['link']; adds macs_seen=[]
    consumed = {"et6b1": {"link": "link", "bmc_mac": "aa:bb", "bmc_reserved": True,
                          "bmc_mac_seen": True, "host_reserved": False}}
    blade = next(s for s in blades.build_slots(
        _GEO["slots"], consumed, {}, {"order_no": None, "population": None})
        if s["port"] == "et6b1")
    assert blade["steps"]["Discover"]["switchportlink"] == "done"
    assert blade["macs_seen"] == []
    assert blade["link"] == "link"


def _nslot(port, pos, chassis, ou):
    return geometry._normalize_slot({"port": port, "switch": "rabbit-lorax",
                                     "bottom_ou": ou, "width": 3, "pos": pos,
                                     "chassis": chassis})


def test_build_slots_fills_ghost_placeholders_for_missing_columns():
    """A width-3 group missing a column (R here, e.g. a trimmed non-post port)
    gets a ghost placeholder cell (no port, empty, placeholder) so the L/C/R grid
    renders aligned instead of shifting cells left."""
    slots = [_nslot("et5b1", "1:3", 1, 1), _nslot("et9b1", "2:3", 1, 1)]  # L, C — no R
    out = blades.build_slots(slots, {}, {}, {"order_no": None, "population": None})
    cells = {s["col"] for s in out if s["group"] == 1 and s["ou"] == 1}
    assert cells == {"L", "C", "R"}, f"expected full L/C/R via ghost, got {cells}"
    ghost = next(s for s in out if s["group"] == 1 and s["ou"] == 1 and s["col"] == "R")
    assert ghost.get("placeholder") is True
    assert ghost.get("port") is None
    assert ghost["empty"] is True


def test_bmc_pinged_field_exposed_true():
    """The SOL reconciler needs bmc_pinged (BMC reachability) on the blade record
    directly (not just buried in a Discover step name) — observe already writes
    post_state.vars.bmc_pinged."""
    consumed = {"et6b1": {"bmc_mac": "aa:bb", "bmc_ip": "172.24.6.101"}}
    st = {"et6b1": {"bmc_pinged": True}}
    blade = next(s for s in blades.build_slots(_GEO["slots"], consumed, st,
                                               {"order_no": None, "population": None})
                 if s["port"] == "et6b1")
    assert blade["bmc_pinged"] is True


def test_bmc_pinged_field_exposed_false():
    consumed = {"et6b1": {"bmc_mac": "aa:bb", "bmc_ip": "172.24.6.101"}}
    st = {"et6b1": {"bmc_pinged": False}}
    blade = next(s for s in blades.build_slots(_GEO["slots"], consumed, st,
                                               {"order_no": None, "population": None})
                 if s["port"] == "et6b1")
    assert blade["bmc_pinged"] is False


def test_bmc_pinged_field_absent_defaults_false():
    consumed = {"et6b1": {"bmc_mac": "aa:bb", "bmc_ip": "172.24.6.101"}}
    st = {"et6b1": {}}
    blade = next(s for s in blades.build_slots(_GEO["slots"], consumed, st,
                                               {"order_no": None, "population": None})
                 if s["port"] == "et6b1")
    assert blade["bmc_pinged"] is False


def test_post_switch_derived_from_geometry_racks():
    """The viewer's post switch comes from the geometry's first declared rack
    (braintree rabbit-lorax vs eindhoven rabbit-edam), not a hardcoded constant;
    empty racks fall back to the module default."""
    geo = {"racks": {"rabbit-lorax": {"tag": "", "label": "Braintree Post"}}, "slots": []}
    assert blades.post_switch(geo) == "rabbit-lorax"
    assert blades.post_switch({"racks": {}, "slots": []}) == blades.SWITCH


# ---------------------------------------------------------------------------
# Qualify/Done phase tests (Task 8)
# ---------------------------------------------------------------------------
def test_qualify_steps_map_agent_status():
    from flax_post import blades
    st = {"qual": {"steps": {
        "agent-reachable": {"status": "pass"}, "sdr-pre": {"status": "pass"},
        "iperf": {"status": "running"}, "sel-post": {"status": "pending"}}}}
    out = blades._qualify_steps(st)
    assert out["agent-reachable"] == "done"
    assert out["sdr-pre"] == "done"
    assert out["iperf"] == "cur"
    assert out["sel-post"] == "pending"
    # order-first boot-detection present
    assert list(blades.PHASE_STEPS["Qualify"])[0] == "agent-reachable"
    assert "hosted-fw" not in blades.PHASE_STEPS["Qualify"]


def test_qualify_step_fault_maps_to_fault():
    from flax_post import blades
    st = {"qual": {"steps": {"cpu-mem-stress": {"status": "fail"}}}}
    assert blades._qualify_steps(st)["cpu-mem-stress"] == "fault"


def test_done_steps_from_vars_done():
    from flax_post import blades
    st = {"done": {"identify": "done", "power_off": "done", "verdict": "pass"}}
    out = blades._done_steps(st)
    assert out["identify"] == "done" and out["power-off"] == "done" and out["done"] == "done"


def test_done_pending_when_no_verdict():
    from flax_post import blades
    out = blades._done_steps({})
    assert set(out.values()) == {"pending"}


def test_record_surfaces_run_id_from_vars_qual():
    from flax_post import blades
    slots = [{"port": "et6b1", "switch": "rabbit-edam", "ou": 12, "height": 2,
              "width": 3, "col": "L", "group": 2}]
    state = {"et6b1": {"qual": {"run_id": "runXYZ"}}}
    out = blades.build_slots(slots, {}, state, {}, {})
    rec = next(s for s in out if s.get("port") == "et6b1")
    assert rec["run_id"] == "runXYZ"


def test_qualify_includes_fio_step():
    from flax_post import blades
    q = list(blades.PHASE_STEPS["Qualify"])
    assert "fio" in q
    assert q.index("smart-health") == q.index("inventory") + 1   # SMART right after inventory
    assert q.index("fio") == q.index("smart-health") + 1           # fio right after SMART
    st = {"qual": {"steps": {"fio": {"status": "pass"}}}}
    assert blades._qualify_steps(st)["fio"] == "done"


# ---- completion latch + skip (spec 2026-09-11-post-completion-latch) ----
_LATCH_CONSUMED = {"et6b1": {"link": "link", "bmc_mac": "aa:bb", "host_mac": "cc:dd",
                             "bmc_mac_seen": True, "host_mac_seen": True,
                             "bmc_ip": "172.24.6.101", "host_ip": "172.24.7.101",
                             "bmc_leased": True, "host_leased": False,
                             "bmc_reserved": True, "host_reserved": True}}
_FW_DONE = {"fw_bmc": {"phase": "up_to_date"}, "fw_bios": {"phase": "up_to_date"},
            "fw_nic": {"phase": "up_to_date", "devices": []}}
_QUAL_PASS = {"steps": {n: {"status": "pass"} for n in blades.PHASE_STEPS["Qualify"]}}
_SNAP = {"Discover": {n: "done" for n in blades.PHASE_STEPS["Discover"]},
         "Firmware": {n: "done" for n in blades.PHASE_STEPS["Firmware"]}}


def _latch_blade(st):
    return next(s for s in blades.build_slots(_GEO["slots"], _LATCH_CONSUMED, {"et6b1": st},
                                              {"order_no": None, "population": None})
                if s["port"] == "et6b1")


def test_pass_verdict_holds_done_when_powered_off_and_unpinged():
    st = dict(_FW_DONE, serial="SN1", power_on="off", bmc_pinged=True, host_pinged=False,
              qual=_QUAL_PASS, done={"verdict": "pass", "identify": "done", "power_off": "done"})
    b = _latch_blade(st)
    assert b["phase"] == "Done"
    # a latched row WITHOUT a snapshot renders its live (regressed) maps, and
    # those holes make the run not clean: the last step never completes
    assert b["steps"]["Done"] == {"identify": "done", "power-off": "done", "done": "fault"}
    assert b["step"] == "not clean" and b["clean"] is False
    assert b["steps"]["Discover"]["power-on"] == "cur"
    assert b["steps"]["Discover"]["host-leased"] == "pending"


def test_pass_verdict_with_failed_power_off_shows_done_fault():
    st = dict(_FW_DONE, serial="SN1", power_on="on", bmc_pinged=True, host_pinged=True,
              qual=_QUAL_PASS, done={"verdict": "pass", "identify": "done",
                                     "power_off": "fault", "power_off_reason": "still_on",
                                     "steps": _SNAP})
    b = _latch_blade(st)
    assert b["phase"] == "Done"
    assert b["steps"]["Done"]["power-off"] == "fault"
    assert b["steps"]["Done"]["done"] == "pending"        # clean run, tail not verified


def test_fail_verdict_holds_qualify_with_failed_step_fault():
    q = {"steps": dict(_QUAL_PASS["steps"], **{"iperf": {"status": "fail"}})}
    st = dict(_FW_DONE, serial="SN1", power_on="off", bmc_pinged=True, host_pinged=False,
              qual=q, done={"verdict": "fail"})
    b = _latch_blade(st)
    assert b["phase"] == "Qualify"
    assert b["steps"]["Qualify"]["iperf"] == "fault"
    assert b["steps"]["Done"] == {"identify": "pending", "power-off": "pending", "done": "pending"}


def test_no_verdict_still_regresses_live():
    st = dict(_FW_DONE, serial="SN1", power_on="off", bmc_pinged=True, host_pinged=False,
              qual=_QUAL_PASS, done={})
    assert _latch_blade(st)["phase"] == "Discover"


def test_skip_is_its_own_status_and_completes_the_phase():
    q = {"steps": dict(_QUAL_PASS["steps"], **{"fio": {"status": "skip",
                                                       "summary": {"reason": "no physical storage media"}}})}
    consumed = {"et6b1": dict(_LATCH_CONSUMED["et6b1"], host_leased=True)}
    st = dict(_FW_DONE, serial="SN1", power_on="on", bmc_pinged=True, host_pinged=True, qual=q)
    b = next(s for s in blades.build_slots(_GEO["slots"], consumed, {"et6b1": st},
                                           {"order_no": None, "population": None})
             if s["port"] == "et6b1")
    assert b["steps"]["Qualify"]["fio"] == "skip"
    assert b["step_notes"] == {"fio": "no storage"}
    assert b["phase"] == "Done"          # skip does not hold the phase at Qualify


def test_skip_reason_other_than_storage_is_passed_through():
    q = {"steps": dict(_QUAL_PASS["steps"], **{"fio": {"status": "skip",
                                                       "summary": {"reason": "tool missing"}}})}
    st = dict(_FW_DONE, serial="SN1", power_on="on", bmc_pinged=True, host_pinged=True, qual=q)
    assert _latch_blade(st)["step_notes"] == {"fio": "tool missing"}


def test_fail_verdict_population_check_comes_from_pop_slice_when_steps_were_lost():
    """After a fail the blade is powered off; the old unreachable-poll wrote a fresh
    qual with only pending boot steps. The pop slice survives, so the tile still
    shows WHICH step failed (population-check fault -> red dot)."""
    q = {"agent": {"reachable": False},
         "steps": {n: {"status": "pending"} for n in ("pxe-in-logs", "live-iso-in-logs", "agent-reachable")}}
    st = dict(_FW_DONE, serial="SN1", power_on="off", bmc_pinged=True, host_pinged=False,
              qual=q, pop={"profile": "ovh-bare-tp", "verdict": "red",
                           "failed_rules": ["BIOS Version: TPC_P26C"]},
              done={"verdict": "fail"})
    b = _latch_blade(st)
    assert b["phase"] == "Qualify"
    assert b["steps"]["Qualify"]["population-check"] == "fault"


def test_population_check_green_from_pop_slice():
    st = dict(_FW_DONE, serial="SN1", power_on="on", bmc_pinged=True, host_pinged=True,
              qual={"steps": {}}, pop={"verdict": "green", "failed_rules": []})
    assert _latch_blade(st)["steps"]["Qualify"]["population-check"] == "done"


# ---------------------------------------------------------------------------
# Ladder faults on steps that are not ladder-derived Discover steps
# ---------------------------------------------------------------------------
def _lad(rung, reason, **kw):
    d = {"rung": rung, "since": 1.0, "power_on_at": 1.0, "power_on_pending": False,
         "marks": {"skipped": "power already on"}, "probe_calls": {},
         "fault": {"rung": rung, "reason": reason, "at": 700.0}}
    d.update(kw)
    return d


def test_ladder_fault_on_host_leased_renders_fault_on_the_discover_step():
    """host-leased is a consumed-fact Discover step, not a ladder step; a
    ladder fault there must still show on the tile instead of vanishing."""
    lad = _lad("host-leased", "no host lease after 120s")
    r = _rec(None, power_on="on", st_extra={"ladder": lad})
    assert r["steps"]["Discover"]["host-leased"] == "fault"
    assert r["fault_notes"]["host-leased"] == "no host lease after 120s"


def test_ladder_fault_on_agent_reachable_renders_fault_on_the_qualify_step():
    lad = _lad("agent-reachable", "agent not reachable 180s after launch")
    r = _rec(None, power_on="on", st_extra={"ladder": lad})
    assert r["steps"]["Qualify"]["agent-reachable"] == "fault"
    assert r["fault_notes"]["agent-reachable"] == "agent not reachable 180s after launch"


def test_fw_gates_fault_renders_on_the_first_blocking_firmware_step():
    """fw-gates is not a tile step at all; the fault (and its note) attach to
    the first update step whose slice does not pass the gate."""
    lad = _lad("fw-gates", "firmware gate not passed after 600s")
    reason = "firmware gate not passed after 600s"

    # BMC blocks -> bmc-updated
    r = _rec({"phase": "unreachable"}, power_on="on", st_extra={
        "ladder": lad, "fw_bios": {"phase": "up_to_date"},
        "fw_nic": {"phase": "up_to_date", "devices": []}})
    assert r["steps"]["Firmware"]["bmc-updated"] == "fault"
    assert r["fault_notes"]["bmc-updated"] == reason

    # BMC passes, BIOS blocks -> bios-updated
    r = _rec({"phase": "up_to_date"}, power_on="on", st_extra={
        "ladder": lad, "fw_bios": {"phase": "unreachable"},
        "fw_nic": {"phase": "up_to_date", "devices": []}})
    assert r["steps"]["Firmware"]["bios-updated"] == "fault"
    assert r["fault_notes"]["bios-updated"] == reason
    assert r["steps"]["Firmware"]["bmc-updated"] == "done"

    # BMC and BIOS pass, NIC blocks -> mlx-updated
    r = _rec({"phase": "up_to_date"}, power_on="on", st_extra={
        "ladder": lad, "fw_bios": {"phase": "up_to_date"}, "fw_nic": None})
    assert r["steps"]["Firmware"]["mlx-updated"] == "fault"
    assert r["fault_notes"]["mlx-updated"] == reason


def test_bmc_unsupported_marks_bmc_steps_done():
    """_GATE_PASS accepts `unsupported`, so the tile must agree: no perpetual
    pending on a board with nothing to flash."""
    r = _rec({"phase": "unsupported"})
    fw = r["steps"]["Firmware"]
    assert fw["bmc-checked"] == "done" and fw["bmc-updated"] == "done"
    assert blades.fw_gate_passed({"phase": "unsupported"}) is True


def test_record_carries_the_ladder_reset_stamp():
    r = _rec(None, power_on="on", st_extra={"ladder_reset_at": 1234.5})
    assert r["ladder_reset_at"] == 1234.5


def test_ladder_view_carries_rung_budget_marks_skipped_and_fault(monkeypatch):
    monkeypatch.setenv("FLAX_POST_LADDER_HOST_SSH_S", "360")
    lad = {"rung": "host-ssh", "since": 100.0, "power_on_at": 10.0, "power_on_pending": False,
           "marks": {"tftp": 40.0, "ping": 90.0, "skipped": "power already on"},
           "fault": {"rung": "host-ssh", "reason": "no ssh after 360s", "at": 461.0}, "probe_calls": {}}
    r = _rec(None, power_on="on", st_extra={"ladder": lad})
    v = r["ladder_view"]
    assert v["rung"] == "host-ssh" and v["since"] == 100.0 and v["budget_s"] == 360
    assert v["marks"] == {"tftp": 40.0, "ping": 90.0} and v["skipped"] == "power already on"
    assert v["fault"]["reason"] == "no ssh after 360s"
    # timing faults are modal-only: not in the inline step_notes
    assert "host-ssh" not in r["step_notes"] and r["fault_notes"]["host-ssh"] == "no ssh after 360s"


def test_no_ladder_means_empty_ladder_view():
    assert _rec(None)["ladder_view"] == {}


# ---------------------------------------------------------------------------
# Latched Discover/Firmware (ruling 2026-09-12): a verdict carries a snapshot of
# the run's Discover + Firmware step maps in done.steps; the tile renders THOSE
# once latched, not the live signals that regress after power-off (leases
# expire, biosd/nicd write unreachable). Qualify and Done already latch through
# their own slices. A latched row with no snapshot (pre-ruling) stays live.
# ---------------------------------------------------------------------------
_OFF_UNREACHABLE = dict(serial="SN1", power_on="off", bmc_pinged=True, host_pinged=False,
                        fw_bmc={"phase": "up_to_date"}, fw_bios={"phase": "unreachable"},
                        fw_nic={"phase": "unreachable", "devices": []})


def test_pass_verdict_renders_discover_and_firmware_from_the_done_snapshot():
    st = dict(_OFF_UNREACHABLE, qual=_QUAL_PASS,
              done={"verdict": "pass", "identify": "done", "power_off": "done", "steps": _SNAP})
    b = _latch_blade(st)
    assert b["phase"] == "Done"
    assert set(b["steps"]["Discover"].values()) == {"done"}
    assert set(b["steps"]["Firmware"].values()) == {"done"}
    # key order is the canonical PHASE_STEPS order, whatever the snapshot's order
    assert list(b["steps"]["Discover"]) == list(blades.PHASE_STEPS["Discover"])
    assert list(b["steps"]["Firmware"]) == list(blades.PHASE_STEPS["Firmware"])


def test_fail_verdict_renders_from_the_snapshot_too():
    q = {"steps": dict(_QUAL_PASS["steps"], **{"iperf": {"status": "fail"}})}
    st = dict(_OFF_UNREACHABLE, qual=q, done={"verdict": "fail", "steps": _SNAP})
    b = _latch_blade(st)
    assert b["phase"] == "Qualify"
    assert set(b["steps"]["Discover"].values()) == {"done"}
    assert set(b["steps"]["Firmware"].values()) == {"done"}
    assert b["steps"]["Qualify"]["iperf"] == "fault"


def test_snapshot_holes_are_kept_and_missing_steps_read_not_in_this_run():
    snap = {"Discover": dict(_SNAP["Discover"], **{"tftp-seen": "skip", "host-ssh": "cur"}),
            "Firmware": {"bmc-checked": "done", "bmc-updated": "done"}}     # bios/mlx absent
    st = dict(_OFF_UNREACHABLE, qual=_QUAL_PASS,
              done={"verdict": "pass", "identify": "done", "power_off": "done", "steps": snap})
    b = _latch_blade(st)
    assert b["steps"]["Discover"]["tftp-seen"] == "unknown"      # a marker is never `skip`
    assert b["steps"]["Discover"]["host-ssh"] == "cur"
    # steps the snapshot never had were not part of that run: skip + note
    assert b["steps"]["Firmware"]["bios-updated"] == "skip"
    assert b["steps"]["Firmware"]["mlx-updated"] == "skip"
    assert b["step_notes"]["bios-updated"] == "added since this run"


def test_snapshot_never_overrides_qualify_or_done():
    snap = dict(_SNAP, Qualify={n: "done" for n in blades.PHASE_STEPS["Qualify"]},
                Done={n: "done" for n in blades.PHASE_STEPS["Done"]})
    q = {"steps": dict(_QUAL_PASS["steps"], **{"iperf": {"status": "fail"}})}
    st = dict(_OFF_UNREACHABLE, qual=q, done={"verdict": "fail", "steps": snap})
    b = _latch_blade(st)
    assert b["steps"]["Qualify"]["iperf"] == "fault"
    assert b["steps"]["Done"] == {"identify": "pending", "power-off": "pending", "done": "pending"}


def test_snapshot_is_ignored_without_a_verdict():
    st = dict(_OFF_UNREACHABLE, qual=_QUAL_PASS, done={"steps": _SNAP})
    b = _latch_blade(st)
    assert b["phase"] == "Discover"
    assert b["steps"]["Discover"]["host-leased"] == "pending"
    assert b["steps"]["Firmware"]["bios-checked"] == "cur"


def test_latched_row_without_a_snapshot_stays_live():
    """Rows latched before the ruling carry no done.steps: their holes are real
    (never re-run under the ladder) and must show, never be painted over."""
    st = dict(_OFF_UNREACHABLE, qual=_QUAL_PASS,
              done={"verdict": "pass", "identify": "done", "power_off": "done"})
    b = _latch_blade(st)
    assert b["phase"] == "Done"
    assert b["steps"]["Discover"]["host-leased"] == "pending"
    assert b["steps"]["Firmware"]["bios-checked"] == "cur"


def test_latch_snapshot_keeps_only_discover_and_firmware():
    steps = {"Discover": {"power-on": "done"}, "Firmware": {"bmc-checked": "cur"},
             "Qualify": {"fio": "done"}, "Done": {"done": "pending"}}
    assert blades.latch_snapshot(steps) == {"Discover": {"power-on": "done"},
                                            "Firmware": {"bmc-checked": "cur"}}
    assert blades.latch_snapshot(None) is None
    assert blades.latch_snapshot({}) is None
    assert blades.latch_snapshot({"Qualify": {"fio": "done"}}) is None


def test_legacy_snapshot_skip_on_a_boot_marker_renders_unknown():
    """et5b4/et6b3 (2026-09-12) latched under 0.9.139 with markers frozen as
    `skip`; a boot marker is never legitimately skipped, so the snapshot
    renders `unknown` and the bar withholds green. fio's skip is untouched."""
    snap = {"Discover": dict(_SNAP["Discover"], **{"tftp-seen": "skip", "ipxe-seen": "skip",
                                                  "live-iso-seen": "skip"}),
            "Firmware": _SNAP["Firmware"]}
    q = {"steps": dict(_QUAL_PASS["steps"], **{"fio": {"status": "skip", "summary": {"reason": "x"}}})}
    st = dict(_OFF_UNREACHABLE, qual=q,
              done={"verdict": "pass", "identify": "done", "power_off": "done", "steps": snap})
    b = _latch_blade(st)
    d = b["steps"]["Discover"]
    assert d["tftp-seen"] == "unknown" and d["ipxe-seen"] == "unknown" and d["live-iso-seen"] == "unknown"
    assert not blades.phase_done(d)
    assert b["steps"]["Qualify"]["fio"] == "skip" and blades.phase_done(b["steps"]["Qualify"])


# ---- the clean mark (ruling 2026-09-12): Done completes only on a clean run ----
def test_pass_with_holes_never_completes_done():
    snap = {"Discover": dict(_SNAP["Discover"], **{"tftp-seen": "unknown", "ipxe-seen": "unknown"}),
            "Firmware": dict(_SNAP["Firmware"], **{"bmc-updated": "cur"})}
    st = dict(_OFF_UNREACHABLE, qual=_QUAL_PASS,
              done={"verdict": "pass", "identify": "done", "power_off": "done", "steps": snap})
    b = _latch_blade(st)
    assert b["phase"] == "Done" and b["verdict"] == "pass"
    assert b["steps"]["Done"] == {"identify": "done", "power-off": "done", "done": "fault"}
    assert b["clean"] is False
    assert b["holes"] == {"Discover": ["tftp-seen", "ipxe-seen"], "Firmware": ["bmc-updated"]}
    assert b["fault_notes"]["done"] == "run not clean: Discover: tftp-seen, ipxe-seen; Firmware: bmc-updated"
    assert b["step"] == "not clean"


def test_clean_pass_completes_done_and_carries_no_holes():
    st = dict(_OFF_UNREACHABLE, qual=_QUAL_PASS,
              done={"verdict": "pass", "identify": "done", "power_off": "done", "steps": _SNAP})
    b = _latch_blade(st)
    assert b["steps"]["Done"] == {"identify": "done", "power-off": "done", "done": "done"}
    assert b["clean"] is True and b["holes"] == {} and b["step"] is None
    assert "done" not in b["fault_notes"]


def test_qualify_hole_on_a_pass_blocks_done_too():
    q = {"steps": dict(_QUAL_PASS["steps"], **{"console": {"status": "fail", "summary": {"reason": "no sol capture"}}})}
    st = dict(_OFF_UNREACHABLE, qual=q,
              done={"verdict": "pass", "identify": "done", "power_off": "done", "steps": _SNAP})
    b = _latch_blade(st)
    assert b["holes"] == {"Qualify": ["console"]} and b["steps"]["Done"]["done"] == "fault"


def test_fail_verdict_is_never_clean_and_done_stays_pending():
    q = {"steps": dict(_QUAL_PASS["steps"], **{"iperf": {"status": "fail"}})}
    st = dict(_OFF_UNREACHABLE, qual=q, done={"verdict": "fail", "steps": _SNAP})
    b = _latch_blade(st)
    assert b["clean"] is False and b["holes"] == {"Qualify": ["iperf"]}
    assert b["steps"]["Done"] == {"identify": "pending", "power-off": "pending", "done": "pending"}


def test_unverdicted_blade_has_no_holes_judged():
    st = dict(_OFF_UNREACHABLE, qual={"steps": {}})
    b = _latch_blade(st)
    assert b["clean"] is False and b["holes"] == {}


def test_bmc_ready_is_a_ladder_driven_discover_step():
    lad = {"rung": "bmc-ready", "since": 1.0, "power_on_at": 1.0, "power_on_pending": False,
           "marks": {"tftp": 2.0, "ipxe": 3.0, "iso": 4.0, "ping": 5.0, "ssh": 6.0}, "fault": None, "probe_calls": {}}
    d = _rec(None, power_on="on", st_extra={"ladder": lad})["steps"]["Discover"]
    assert d["host-ssh"] == "done" and d["bmc-ready"] == "cur"
    lad2 = dict(lad, rung="fw-gates", marks=dict(lad["marks"], bmcready=7.0))
    d = _rec(None, power_on="on", st_extra={"ladder": lad2})["steps"]["Discover"]
    assert d["bmc-ready"] == "done"
    # no ladder at all -> unknowable
    assert _rec(None, power_on="on")["steps"]["Discover"]["bmc-ready"] == "unknown"


def test_snapshot_from_before_a_step_existed_stays_clean_and_says_so():
    """The 27 clean runs of 2026-09-12 predate bmc-ready: their snapshots lack
    it. That is 'not in this run', a legitimate skip — never a hole."""
    snap = {"Discover": {n: "done" for n in blades.PHASE_STEPS["Discover"] if n != "bmc-ready"},
            "Firmware": _SNAP["Firmware"]}
    st = dict(_OFF_UNREACHABLE, qual=_QUAL_PASS,
              done={"verdict": "pass", "identify": "done", "power_off": "done", "steps": snap})
    b = _latch_blade(st)
    assert b["steps"]["Discover"]["bmc-ready"] == "skip"
    assert b["step_notes"]["bmc-ready"] == "added since this run"
    assert b["clean"] is True and b["steps"]["Done"]["done"] == "done"


def test_phase_follows_the_ladder_once_the_gate_was_proven():
    """et6b1 2026-09-12: mid-battery the fwd scan wrote `unreachable` again and
    the tile dropped from Qualify to Firmware. Past fw-gates the ladder is
    the authority; before it the live gate still holds the phase."""
    lad = {"rung": "qualify", "since": 1.0, "power_on_at": 1.0, "power_on_pending": False,
           "marks": {"tftp": 2.0, "ipxe": 3.0, "iso": 4.0, "ping": 5.0, "ssh": 6.0, "bmcready": 7.0},
           "fault": None, "probe_calls": {}}
    r = _rec({"phase": "unreachable"}, power_on="on", st_extra={
        "ladder": lad, "fw_bios": {"phase": "up_to_date"}, "fw_nic": {"phase": "up_to_date", "devices": []},
        "qual": {"steps": {"agent-reachable": {"status": "pass"}}}})
    assert r["fw_gates"] is False and r["phase"] == "Qualify"
    r = _rec({"phase": "unreachable"}, power_on="on", st_extra={
        "ladder": dict(lad, rung="fw-gates"), "fw_bios": {"phase": "up_to_date"},
        "fw_nic": {"phase": "up_to_date", "devices": []}})
    assert r["phase"] == "Firmware"


# ---- bmc-ready retry display + reset scalars (spec 2026-09-14-post-occupant-reset §4.1/§6) ----
def _bmc_ready_lad(attempts, fault=None):
    return {"rung": "bmc-ready", "since": 1000.0, "power_on_at": 10.0, "power_on_pending": False,
            "marks": {"ssh": 400.0}, "fault": fault, "probe_calls": {},
            "bmc_ready_attempts": [{"at": 1000.0, "waited_s": 601.0}] * attempts}


def test_bmc_ready_retry_shows_a_note_and_the_budget_in_effect():
    r = _rec(None, power_on="on", st_extra={"ladder": _bmc_ready_lad(1)})
    assert r["ladder_view"]["budget_s"] == 600 and len(r["ladder_view"]["attempts"]) == 1
    assert r["fault_notes"]["bmc-ready"] == "retry 1/3: BMC not answering data reads (budget 600s)"
    assert r["steps"]["Discover"]["bmc-ready"] != "fault"
    r = _rec(None, power_on="on", st_extra={"ladder": _bmc_ready_lad(2)})
    assert r["ladder_view"]["budget_s"] == 720
    assert r["fault_notes"]["bmc-ready"] == "retry 2/3: BMC not answering data reads (budget 720s)"


def test_bmc_ready_terminal_fault_note_is_the_fault_reason():
    fault = {"rung": "bmc-ready", "reason": "BMC not answering data reads after 4 attempts (2761s)", "at": 5.0}
    r = _rec(None, power_on="on", st_extra={"ladder": _bmc_ready_lad(3, fault=fault)})
    assert r["fault_notes"]["bmc-ready"] == fault["reason"]
    assert r["steps"]["Discover"]["bmc-ready"] == "fault"


def test_record_carries_the_reset_kind_and_birth_for_the_worker():
    r = _rec(None, power_on="on", st_extra={"ladder_reset_at": 5.0, "ladder_reset_kind": "occupant",
                                              "ladder_reset_born_at": 2.0})
    assert r["ladder_reset_kind"] == "occupant" and r["ladder_reset_born_at"] == 2.0
    r = _rec(None, power_on="on")
    assert r["ladder_reset_kind"] is None and r["ladder_reset_born_at"] is None


# ---- chip_phase: the phase a blade is COUNTED under by the summary buttons ----

def _steps(**over):
    s = {p: {n: "done" for n in blades.PHASE_STEPS[p]} for p in blades.PHASES}
    for key, state in over.items():
        p, n = key.split("__")
        s[p][n.replace("_", "-")] = state
    return s


def test_chip_phase_is_the_phase_reached_when_nothing_faulted():
    s = _steps(Qualify__iperf="cur", Done__identify="pending", Done__power_off="pending", Done__done="pending")
    assert blades.chip_phase("Qualify", s, {}) == "Qualify"
    assert blades.chip_phase("Done", _steps(), {}) == "Done"


def test_chip_phase_is_the_earliest_faulted_phase():
    # the ladder runs on past a fault: a later phase's state never hides it
    s = _steps(Discover__ipxe_seen="fault", Qualify__population_check="fault")
    assert blades.chip_phase("Qualify", s, {}) == "Discover"
    s = _steps(Firmware__bmc_updated="fault", Qualify__iperf="cur")
    assert blades.chip_phase("Qualify", s, {}) == "Firmware"


def test_chip_phase_fail_verdict_stays_on_its_fault_not_an_earlier_hole():
    # a hole that never faulted (bmc-updated still cur) does not outrank a real fault
    s = _steps(Firmware__bmc_updated="cur", Qualify__population_check="fault")
    assert blades.chip_phase("Qualify", s, {}) == "Qualify"


def test_chip_phase_not_clean_pass_is_charged_to_its_first_hole():
    s = _steps(Firmware__bmc_updated="cur", Done__done="fault")
    holes = blades.run_holes(s)
    assert holes == {"Firmware": ["bmc-updated"]}
    assert blades.chip_phase("Done", s, holes) == "Firmware"
    s = _steps(Discover__tftp_seen="unknown", Firmware__bmc_updated="cur", Done__done="fault")
    assert blades.chip_phase("Done", s, blades.run_holes(s)) == "Discover"


def test_chip_phase_done_tail_fault_without_holes_stays_done():
    s = _steps(Done__power_off="fault", Done__done="pending")
    assert blades.chip_phase("Done", s, {}) == "Done"


# --- ship checks: smbios-serial + tpm-state (spec 2026-10-29) ---------------
def test_qualify_order_has_ship_checks_after_population():
    q = list(blades.PHASE_STEPS["Qualify"])
    i = q.index("population-check")
    assert q[i + 1:i + 3] == ["smbios-serial", "tpm-state"]


def test_checks_slice_wins_over_qual_steps():
    st = {"qual": {"steps": {"tpm-state": {"status": "pass"}}},
          "checks": {"tpm-state": {"status": "fail", "reason": "no TPM device"},
                     "smbios-serial": {"status": "pass", "reason": ""}}}
    q = blades._qualify_steps(st)
    assert (q["tpm-state"], q["smbios-serial"]) == ("fault", "done")


def test_live_run_without_checks_is_pending():
    q = blades._qualify_steps({"qual": {"steps": {}}})
    assert (q["smbios-serial"], q["tpm-state"]) == ("pending", "pending")


_OLD_QUAL_PASS = {"steps": {n: {"status": "pass"} for n in blades.PHASE_STEPS["Qualify"]
                            if n not in ("smbios-serial", "tpm-state")}}


def test_preship_verdict_reads_new_checks_as_skip():
    st = dict(_OFF_UNREACHABLE, qual=_OLD_QUAL_PASS,
              done={"verdict": "pass", "identify": "done", "power_off": "done", "steps": _SNAP})
    b = _latch_blade(st)
    assert b["steps"]["Qualify"]["smbios-serial"] == "skip"
    assert b["steps"]["Qualify"]["tpm-state"] == "skip"
    assert b["step_notes"]["tpm-state"] == "added since this run"
    assert "Qualify" not in blades.run_holes(b["steps"])


def test_build_result_failed_check_is_a_hole():
    from flax_post.observe import host_qual
    checks = {"smbios-serial": {"status": "pass", "reason": ""}, "tpm-state": {"status": "fail", "reason": "no TPM device"}}
    r = host_qual.build_result({}, {}, {"run_id": "r1", "steps": {}}, {"verdict": "green"},
                               {"verdict": "fail", "steps": {"Discover": {}, "Firmware": {}}},
                               checks=checks, now=lambda: 1)
    assert "tpm-state" in r["holes"]["Qualify"] and "smbios-serial" not in r["holes"]["Qualify"]


def test_blade_record_step_flags_fio():
    q = {"steps": dict(_QUAL_PASS["steps"], **{"fio": {"status": "pass", "summary": {
        "devices": ["sda", "sdb", "sdc"], "flagged": ["sdc"],
        "judge": {"sdc": {"verdict": "outside", "reasons": ["lat lat_us 9000"], "expected": {}}}}}})}
    consumed = {"et6b1": dict(_LATCH_CONSUMED["et6b1"], host_leased=True)}
    st = dict(_FW_DONE, serial="SN1", power_on="on", bmc_pinged=True, host_pinged=True, qual=q)
    b = next(s for s in blades.build_slots(_GEO["slots"], consumed, {"et6b1": st},
                                           {"order_no": None, "population": None})
             if s["port"] == "et6b1")
    assert b["step_flags"] == {"fio": "1 outside range"}
    assert b["steps"]["Qualify"]["fio"] == "done"
    assert blades.phase_done(b["steps"]["Qualify"]) is True
    assert b["phase"] == "Done" and b["chip_phase"] == "Done"
    # unflagged, or a failed fio (red already): no flag
    for fio in ({"status": "pass", "summary": {"flagged": []}},
                {"status": "fail", "summary": {"flagged": ["sdc"]}}):
        st["qual"] = {"steps": dict(_QUAL_PASS["steps"], fio=fio)}
        b = next(s for s in blades.build_slots(_GEO["slots"], consumed, {"et6b1": st},
                                               {"order_no": None, "population": None})
                 if s["port"] == "et6b1")
        assert b["step_flags"] == {}


# --- effective (per-node) order, 2026-11-06 ---------------------------------
_C = {"et6b1": {"link": "link", "bmc_mac": "aa:bb", "host_mac": "cc:dd", "bmc_ip": "172.24.6.101"}}
_ST = {"et6b1": {"serial": "SN9"}}
_HDR = {"order_no": "RRORD-100", "population": "ovh-bare-tp", "customer": "X", "sales": None}


def _blade(nodes=None, orders=None):
    return next(s for s in blades.build_slots(_GEO["slots"], _C, _ST, _HDR, nodes=nodes,
                                              orders=orders)
                if s["port"] == "et6b1")


def test_record_uses_node_assignment():
    b = _blade({"aa:bb": {"assigned_order_no": "RRORD-1033", "assigned_customer": "stale",
                          "assigned_population": "acme-leo", "assigned_serial": "SN9"}},
               orders={"RRORD-1033": {"sales": "S-1033", "customer": "Acme", "population": None}})
    assert (b["order_no"], b["order_source"], b["header_order_no"]) == ("RRORD-1033", "node", "RRORD-100")
    assert b["customer"] == "Acme" and b["sales"] == "S-1033" and b["population"] == "acme-leo"
    assert b["own_population"] == "acme-leo"
    assert _blade()["own_population"] is None


def test_record_follows_header_without_node():
    b = _blade()
    assert (b["order_no"], b["order_source"], b["order_stale"]) == ("RRORD-100", "header", False)
