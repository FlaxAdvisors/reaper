"""fru.read_baseboard: blade identity from the Builtin FRU Device (ID 0) only
(spec 2026-09-14-fru-id0-only §4). Fixtures are real `ipmitool fru` text."""
import pathlib

import pytest

from flax_discover.family_map import compile_family_map_text
import flax_observe.fru as observe_fru
import flax_post.observe.fru as post_fru

FIX = pathlib.Path(__file__).parent / "fixtures" / "fru"
# The eindhoven family map as it will be after Task 8 (tiogapass gains the D4 line).
FAMILY_MAP = {
    "leopard": compile_family_map_text("Leopard-DDR3\nLeopard-DDR BW\nLeopard-Orv2\nLeopard ORv2-DDR4\n"),
    "tiogapass": compile_family_map_text(
        "SV7220G3\nTiogaPass\nTioga-Pass Channel\nTioga Pass Single Side .*\n"
        ".* Tioga Pass Single Side\n^Tioga Pass( Single Side)?$\n"),
    "wedge100s": compile_family_map_text("WEDGE100S12V\n"),
}


def _fx(name):
    return (FIX / name).read_text()


@pytest.fixture(params=[observe_fru, post_fru], ids=["observe", "post"])
def fru(request):
    return request.param


def test_wistron_three_block_serial_is_the_blade_not_the_nic(fru):
    bb = fru.read_baseboard(_fx("wistron_three_block.txt"), FAMILY_MAP)
    assert bb["state"] == "ok" and bb["serial"] == "WTH49152ZGM1A"
    assert bb["serial_field"] == "Product Serial" and bb["family"] == "tiogapass"
    assert bb["board_mfg"] == "Wistron" and bb["board_product"] == "Tioga Pass Single Side"
    assert bb["product_name"] == "Type6 Tioga Pass Single Side"
    assert bb["addons"] == ["HPE Eth 10/25Gb", "Ava-M.2-SSD-Adap"]


def test_id0_absent_with_nic_and_ava_is_absent_never_the_nic_serial(fru):
    bb = fru.read_baseboard(_fx("id0_absent_limit_nic_ava.txt"), FAMILY_MAP)
    assert bb["state"] == "absent" and bb["serial"] is None
    assert bb["reason"] == "Device not present (Request data field length limit exceeded)"
    assert bb["board_mfg"] is None and bb["product_name"] is None
    assert bb["addons"] == ["HPE Eth 10/25Gb", "Ava-M.2-SSD-Adap"]


def test_id0_absent_alone(fru):
    bb = fru.read_baseboard(_fx("id0_absent_not_found_alone.txt"), FAMILY_MAP)
    assert bb["state"] == "absent" and bb["reason"].startswith("Device not present (Requested sensor")
    assert bb["addons"] == []


def test_quanta_tioga_pass_single_side(fru):
    bb = fru.read_baseboard(_fx("quanta_tioga_pass.txt"), FAMILY_MAP)
    assert (bb["state"], bb["serial"], bb["family"]) == ("ok", "QCFF0818360193", "tiogapass")


def test_leopard_uses_the_id0_chassis_serial(fru):
    bb = fru.read_baseboard(_fx("leopard_orv2.txt"), FAMILY_MAP)
    assert bb["family"] == "leopard" and bb["serial_field"] == "Chassis Serial"
    assert (bb["state"], bb["serial"]) == ("ok", "QCFF0617260650")
    assert bb["addons"] == []          # QAVABFRU/QAVATFRU are "Device not present"


def test_leopard_without_a_family_map_is_product_serial_and_has_none(fru):
    bb = fru.read_baseboard(_fx("leopard_orv2.txt"), {})
    assert bb["family"] is None and bb["serial_field"] == "Product Serial"
    assert (bb["state"], bb["serial"]) == ("no_serial", None)       # no fallback to Chassis Serial


def test_mono_lake_has_no_ship_serial(fru):
    bb = fru.read_baseboard(_fx("mono_lake.txt"), FAMILY_MAP)
    assert (bb["state"], bb["serial"], bb["family"]) == ("no_serial", None, None)
    assert bb["board_mfg"] == "Quanta"


def test_et23b2_wistron_tioga_pass_empty_serials_is_no_serial(fru):
    bb = fru.read_baseboard(_fx("wistron_tioga_pass_no_serial_et23b2.txt"), FAMILY_MAP)
    assert (bb["state"], bb["serial"], bb["family"]) == ("no_serial", None, "tiogapass")
    assert bb["product_name"] == "Tioga Pass" and bb["board_product"] == "Tioga Pass Single Side"


def test_mct_ts2290(fru):
    bb = fru.read_baseboard(_fx("mct_ts2290.txt"), FAMILY_MAP)
    assert (bb["state"], bb["serial"], bb["board_mfg"]) == ("ok", "2031TPS6D0002", "MCT")


def test_ava_before_id0_ordering(fru):
    bb = fru.read_baseboard(_fx("ava_before_id0.txt"), FAMILY_MAP)
    assert (bb["state"], bb["serial"]) == ("ok", "WTH49152ZGM1A")
    assert bb["addons"] == ["Ava-M.2-SSD-Adap", "HPE Eth 10/25Gb"]


def test_weutil_headerless_is_id0_and_aliases_product_serial_number(fru):
    bb = fru.read_baseboard(_fx("weutil.txt"), FAMILY_MAP)
    assert (bb["state"], bb["serial"], bb["family"]) == ("ok", "AI09019591", "wedge100s")
    assert bb["product_name"] == "WEDGE100S12V"


def test_header_id00_is_id0(fru):
    bb = fru.read_baseboard(_fx("header_id00.txt"), FAMILY_MAP)
    assert (bb["state"], bb["serial"]) == ("ok", "QCFF0818360193")


def test_only_integer_zero_is_id0(fru):
    text = "FRU Device Description : Thing (ID 10)\n Product Serial : X10\n"
    bb = fru.read_baseboard(text, {})
    assert bb["state"] == "absent" and bb["reason"] == "no FRU 0 block" and bb["addons"] == ["Thing"]


def test_header_only_id0_is_absent_not_no_serial(fru):
    bb = fru.read_baseboard("FRU Device Description : Builtin FRU Device (ID 0)\n", {})
    assert bb["state"] == "absent" and bb["reason"] == "no FRU fields"


def test_headerless_device_not_present_is_absent(fru):
    bb = fru.read_baseboard("Device not present (Destination unavailable)\n", {})
    assert bb["state"] == "absent" and bb["reason"] == "Device not present (Destination unavailable)"


def test_empty_or_none_text_is_absent(fru):
    assert fru.read_baseboard("", {})["state"] == "absent"
    assert fru.read_baseboard(None, {})["state"] == "absent"


def test_first_non_empty_value_wins_within_the_block(fru):
    bb = fru.read_baseboard("Product Serial :   \nProduct Serial : WTH2\nProduct Serial : LATER\n", {})
    assert bb["serial"] == "WTH2"


def test_fru_blocks_shape(fru):
    blocks = fru.fru_blocks(_fx("id0_absent_limit_nic_ava.txt"))
    assert [(b["id"], b["present"]) for b in blocks] == [(0, False), (86, True), (186, True)]
    assert blocks[1]["fields"]["Product Serial"] == "ILC2230156"
    assert blocks[1]["description"] == "HPE Eth 10/25Gb"


def test_family_map_live_loads_reloads_and_tolerates_a_missing_dir(tmp_path):
    import os, time
    import flax_observe.family_map_live as live
    d = tmp_path / "family-map"
    assert live.current(str(d)) == {}                       # missing dir -> {}
    d.mkdir()
    (d / "leopard.txt").write_text("Leopard ORv2-DDR4\n")
    assert list(live.current(str(d))) == ["leopard"]
    (d / "tiogapass.txt").write_text("^Tioga Pass( Single Side)?$\n")
    later = time.time() + 5
    os.utime(d / "tiogapass.txt", (later, later))
    assert sorted(live.current(str(d))) == ["leopard", "tiogapass"]
    (d / "tiogapass.txt").write_text("([unclosed\n")          # a bad regex keeps the last good map
    later += 5
    os.utime(d / "tiogapass.txt", (later, later))
    assert sorted(live.current(str(d))) == ["leopard", "tiogapass"]


def test_family_map_live_tolerates_a_non_utf8_file(tmp_path):
    import os, time
    import flax_observe.family_map_live as live
    d = tmp_path / "family-map"
    d.mkdir()
    (d / "leopard.txt").write_text("Leopard ORv2-DDR4\n")
    assert list(live.current(str(d))) == ["leopard"]
    (d / "tiogapass.txt").write_bytes(b"\xff\xfe not utf-8\n")  # invalid UTF-8 keeps the last good map
    later = time.time() + 5
    os.utime(d / "tiogapass.txt", (later, later))
    assert list(live.current(str(d))) == ["leopard"]
