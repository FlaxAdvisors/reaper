# tests/test_flax_post_inventory.py
"""flax_post.inventory — INV modal parser + POP modal verdict (pure functions).

The INV/POP modal renders from the blade's CURRENT RUN: the post agent runs
ghost `macinv -p` on the node and uploads the detail form (`macinv-v`, fed to
parse()) and the count form (`macinv`, fed to verdict()). Never /export/nodes:
that tree is keyed by the host NIC MAC (the mezz card), which moves between
blades, so its `latest` can be ANOTHER blade (et28b4 2026-09-25:
post-98039b302fde/latest was blade WTH48381JYM1A from 09-17).

parse()/verdict() are anchored against REAL macinv output captured live from
bang-gouda (tests/fixtures/macinv/*.txt: two SV7220G3 TiogaPass boards with no
detected disks -- node-a/node-b -- and one board with real SATA/SAS/NVMe/USB
storage -- node-c-disk), plus one real node_config profile (linprof-compute).
"""
import os
import re

import pytest

from flax_post import inventory, population


@pytest.fixture(autouse=True)
def _no_host_family_map(monkeypatch):
    monkeypatch.setattr(inventory._fm_live, "current", lambda path=None: {})

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "macinv")


def _read(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read()


# --- parse(): anchor test against node-c-disk (richest real fixture: 2 CPU,
# 8 DIMM, 1 NIC, 4 storage devices spanning SAS/NVMe/USB shapes) ------------

def test_parse_node_c_disk_cpu_and_memory_counts_and_values():
    sections = inventory.parse(_read("node-c-disk.verbose.txt"))
    assert len(sections["cpu"]) == 2
    assert sections["cpu"][0] == {
        "serial": "To be filled by O.E.M.",
        "model": "Intel(R) Xeon(R) Gold 6138 CPU @ 2.00GHz",
    }
    assert len(sections["memory"]) == 8
    assert sections["memory"][0] == {
        "slot": "DIMM A0", "serial": "154E7F74", "size": "32 GB",
        "part": "M393A4K40BB2-CTD", "mfg": "Samsung", "speed": "2666 MT/s",
    }


def test_parse_node_c_disk_nic():
    sections = inventory.parse(_read("node-c-disk.verbose.txt"))
    assert sections["nic"] == [{
        "mac": "98:03:9b:a8:f1:ea",
        "model": "Mellanox MCX4411A-ACQN, ConnectX-4 Lx EN OCP, 1x25Gb",
    }]


def test_parse_node_c_disk_storage_all_four_shapes():
    # real fixture has 4 distinct storage device shapes: 2x SAS (Revision+
    # Serial ID), 1x NVMe (SubVendor/SubDevice+Serial ID), 1x USB (no serial)
    sections = inventory.parse(_read("node-c-disk.verbose.txt"))
    assert len(sections["storage"]) == 4
    models = [s["model"] for s in sections["storage"]]
    assert models == [
        "HITACHI H5H72121 EMC12T0", "HITACHI H5H72121 EMC12T0",
        "Realtek CS1030 250GB SSD", "Virtual USB Slot_2",
    ]
    hitachi = sections["storage"][0]
    assert hitachi == {"serial": "D5GNP0KM", "size": "11176 GB",
                        "model": "HITACHI H5H72121 EMC12T0"}
    nvme = sections["storage"][2]
    assert nvme == {"serial": "PNH02263039780200696", "size": "232 GB",
                     "model": "Realtek CS1030 250GB SSD"}
    usb = sections["storage"][3]
    assert usb == {"serial": "", "size": "2 GB", "model": "Virtual USB Slot_2"}


def test_parse_node_c_disk_block_ctlr_and_bios():
    sections = inventory.parse(_read("node-c-disk.verbose.txt"))
    assert [c["model"] for c in sections["block_ctlr"]] == [
        "Intel C620 Series Chipset Family SATA Controller [AHCI mode]",
        "Broadcom / LSI MegaRAID SAS 9380-8e",
        "Intel C620 Series Chipset Family SSATA Controller [AHCI mode]",
    ]
    assert sections["bios"] == {"version": "TPC_P26C"}


def test_parse_node_c_disk_missing_sections_default_empty_not_keyerror():
    # this fixture has no BMC MAC line, no Board Mfg (FRU) line, no PortDescr,
    # no GPU -- confirms the parser is robust to absent sections
    sections = inventory.parse(_read("node-c-disk.verbose.txt"))
    assert sections["bmc"] == {"version": ""}
    assert sections["fru"] == {}
    assert sections["gpu"] == []
    assert sections["lldp"] == [{
        "iface": "pxedev", "neighbor": "mac 00:90:fb:6b:a9:ec", "port": "",
    }]


# --- parse(): node-a fixture covers what node-c-disk lacks: BMC MAC, FRU
# (Board Mfg + Product Serial), LLDP PortDescr, and the BMC's onboard ASPEED
# VGA controller (macinv's gpudump genuinely classifies it as "gpu") -------

def test_parse_node_a_bmc_fru_lldp_and_onboard_vga():
    sections = inventory.parse(_read("node-a.verbose.txt"))
    assert sections["bmc"] == {"mac": "50:6b:4b:cd:5e:9c", "version": "0.08  Facebook, Inc."}
    assert sections["fru"] == {
        "mfg": "Wistron", "board_product": "Tioga Pass Single Side",
        "product_serial": "WTH4908267M1A", "chassis_serial": "C.M.B60.00X1C.001150201803DV",
        "family": None, "serial": "WTH4908267M1A", "serial_field": "Product Serial",
    }
    assert sections["lldp"] == [{
        "iface": "pxedev", "neighbor": "mac 00:90:fb:6c:10:04",
        "port": "Rack 2 right+center DUT subport - default TiogaPass post (VLAN 25)",
    }]
    # ASPEED AST1000/2000 is the BMC's own video controller, real macinv
    # gpudump output on this board -- storage is empty on this node (no
    # disks detected in this particular recon boot capture).
    assert sections["gpu"] == [{"model": "ASPEED AST1000/2000"}]
    assert sections["storage"] == []


def test_parse_node_a_nic_count_and_first_model():
    sections = inventory.parse(_read("node-a.verbose.txt"))
    assert len(sections["nic"]) == 3
    assert sections["nic"][0] == {
        "mac": "50:6b:4b:cd:5e:9a",
        "model": "Mellanox MCX4411A-ACQN, ConnectX-4 Lx EN OCP, 1x25Gb",
    }


def test_parse_empty_text_returns_all_empty_sections():
    sections = inventory.parse("")
    assert sections == {
        "cpu": [], "memory": [], "nic": [], "storage": [],
        "bmc": {}, "bios": {}, "fru": {}, "lldp": [], "gpu": [], "block_ctlr": [],
    }


from flax_discover.family_map import compile_family_map_text

_LEOPARD_MAP = {"leopard": compile_family_map_text("Leopard ORv2-DDR4\n")}


def test_parse_fru_is_the_id0_line_never_an_addon():
    text = ("Addon FRU: HPE, Product: HPE Eth 10/25Gb 2p 640SFP28 Adptr, Serial: ILC2230156, "
            "Part Number: 840140-001, Product Serial: ILC2230156\n")
    assert inventory.parse(text)["fru"] == {}


def test_parse_fru_without_product_serial_is_no_serial():
    text = "Board Mfg: Wistron, Product: Tioga Pass Single Side, Serial: WTH194826LZA1, Part Number: B91.00X10.0025\n"
    fru = inventory.parse(text)["fru"]
    assert fru["mfg"] == "Wistron" and fru["serial"] == "" and fru["product_serial"] == ""


def test_parse_fru_leopard_serial_is_the_chassis_serial():
    text = ("Chassis Serial: QCFF0617260650\n"
            "Board Mfg: Quanta, Product: Leopard ORv2-DDR4, Serial: QTF5KQ71502560, "
            "Part Number: 31F06MB00S0, Chassis Serial: QCFF0617260650\n")
    fru = inventory.parse(text, family_map=_LEOPARD_MAP)["fru"]
    assert (fru["family"], fru["serial_field"], fru["serial"]) == ("leopard", "Chassis Serial", "QCFF0617260650")


def test_parse_fru_old_macinv_first_board_line_still_reads():
    """Before the ghost sync (D6) macinv still prints every FRU as Board Mfg;
    the first line is ID 0 whenever FRU 0 answered."""
    text = ("Board Mfg: Wistron, Product: Tioga Pass Single Side, Serial: W, Part Number: P, Product Serial: WTH1\n"
            "Board Mfg: HPE, Product: HPE Eth, Serial: N, Part Number: 8, Product Serial: NIC1\n")
    assert inventory.parse(text)["fru"]["serial"] == "WTH1"


# --- verdict(): grey (no profile), and real deterministic green/red cases --

def test_verdict_no_profile_is_grey():
    v = inventory.verdict("anything", None)
    assert v == {"state": "grey", "profile": None, "results": [], "missing": []}


def test_verdict_named_unloadable_profile_is_grey_not_vacuous_green(tmp_path, monkeypatch):
    # REGRESSION GUARD: a profile_name is set but load_profile() can't find/
    # read it (mistyped name, deleted file) -> population.load_profile()
    # returns [], and evaluate([], ...) reports ok=True (all() of an empty
    # list) -- that must NOT surface as a passing (green) population check.
    monkeypatch.setattr(population, "PROFILE_DIR", str(tmp_path))
    v = inventory.verdict(_read("node-a.count.txt"), "does-not-exist")
    assert v == {"state": "grey", "profile": "does-not-exist", "results": [], "missing": []}


def test_verdict_green_with_matching_inline_profile(tmp_path, monkeypatch):
    # full-line presence rules against the COUNT form. node-a's count fixture
    # has a presence BIOS line, a "2 Processor Version: ...Gold 6138...2.00GHz"
    # count line, and a "1 Model: ...MCX4411A-ACQN..." count line -- all three
    # rules are authored to match those count lines verbatim.
    monkeypatch.setattr(population, "PROFILE_DIR", str(tmp_path))
    (tmp_path / "tiny").write_text(
        "'BIOS Version: TPC_P26C'\n"
        "'2 Processor.*Intel.*Gold 6138.*2.00GHz'\n"
        "'1 Model:.*MCX4411A-ACQN'\n"
    )
    v = inventory.verdict(_read("node-a.count.txt"), "tiny")
    assert v["state"] == "green"
    assert v["profile"] == "tiny"
    assert v["missing"] == []
    assert all(r["ok"] for r in v["results"])
    cpu_rule = next(r for r in v["results"] if "Gold 6138" in r["rule"])
    assert cpu_rule["found"] == 1 and cpu_rule["ok"] is True  # one collapsed count line


def test_verdict_red_with_nonmatching_inline_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(population, "PROFILE_DIR", str(tmp_path))
    (tmp_path / "tiny").write_text("'BIOS Version: NOT_A_REAL_BIOS'\n")
    v = inventory.verdict(_read("node-a.count.txt"), "tiny")
    assert v["state"] == "red"
    assert v["missing"] == ["BIOS Version: NOT_A_REAL_BIOS"]


def test_verdict_leading_count_integer_distinguishes_dimm_config_regression_guard():
    # REGRESSION GUARD for the corrected grammar. node-a is a 12x64GB node;
    # linprof-compute is a REAL profile written for an 8x32GB node. Its
    # "8 Memory Size: 32 GB" rule is a FULL-LINE presence regex: it must NOT
    # match node-a's count line "12 Memory Size: 64 GB, ..." (different count
    # prefix AND size), so that rule is missing/red -- while the shared
    # "2 Processor.*Gold 6138.*2.00GHz" rule DOES match node-a's
    # "2 Processor Version: ...Gold 6138...2.00GHz" count line and is green.
    # If evaluate() ever reverts to splitting the leading integer into a
    # separate expected line-count, the memory rule would spuriously behave
    # differently and this guard fails.
    rules = population.parse_profile(_read("profile-linprof-compute.txt"))
    r = population.evaluate(rules, _read("node-a.count.txt"))
    by_rule = {x["rule"]: x for x in r["results"]}
    mem_rule = by_rule["8 Memory Size: 32 GB"]
    assert mem_rule["found"] == 0 and mem_rule["ok"] is False   # 12x64GB != 8x32GB
    cpu_rule = by_rule["2 Processor.*Intel.*Gold 6138.*2.00GHz"]
    assert cpu_rule["found"] == 1 and cpu_rule["ok"] is True    # count prefix matches
    # presence-only rules on genuinely-shared fields still pass
    assert by_rule["BIOS Version: TPC_P26C"]["ok"] is True


def test_verdict_matching_dimm_config_passes_the_count_rule():
    # the same "8 Memory Size: 32 GB" rule DOES match node-c-disk, which really
    # is an 8x32GB node (count line "8 Memory Size: 32 GB, ..."). Confirms the
    # rule is a genuine discriminator, not always-false.
    rules = population.parse_profile(_read("profile-linprof-compute.txt"))
    r = population.evaluate(rules, _read("node-c-disk.count.txt"))
    by_rule = {x["rule"]: x for x in r["results"]}
    assert by_rule["8 Memory Size: 32 GB"]["found"] == 1
    assert by_rule["8 Memory Size: 32 GB"]["ok"] is True


def test_verdict_wires_load_profile_and_evaluate(tmp_path, monkeypatch):
    # end-to-end through inventory.verdict() (load_profile + evaluate wired)
    # against the real profile + real count fixture. node-a (12x64GB) is a
    # different build than linprof-compute (8x32GB) targets, so overall state
    # is red and the 8x32GB memory rule is in `missing`.
    monkeypatch.setattr(population, "PROFILE_DIR", str(tmp_path))
    (tmp_path / "linprof-compute").write_text(_read("profile-linprof-compute.txt"))
    v = inventory.verdict(_read("node-a.count.txt"), "linprof-compute")
    assert v["state"] == "red"
    assert v["profile"] == "linprof-compute"
    assert len(v["results"]) == 9  # 9 rules in the real profile
    assert "8 Memory Size: 32 GB" in v["missing"]


# --- nothing under flax_post reads triage's /export/nodes tree or shells macinv

_EXPORT_READ = re.compile(r"/export/nodes|NODES_ROOT|[\"']macinv[\"']\s*,\s*[\"']-p[\"']")


def _flax_post_offenders():
    import flax_post
    root = os.path.dirname(flax_post.__file__)
    bad = []
    for dp, _dn, fns in os.walk(root):
        if "__pycache__" in dp:
            continue
        for fn in fns:
            if fn.endswith((".py", ".html", ".js")):
                path = os.path.join(dp, fn)
                with open(path, errors="replace") as f:
                    if _EXPORT_READ.search(f.read()):
                        bad.append(os.path.relpath(path, root))
    return bad


def test_nothing_under_flax_post_reads_export_nodes_or_shells_macinv():
    """Presence-proved 2026-09-25: failed against main's inventory.py
    (NODES_ROOT + `["macinv", "-p", d]`) and against a re-added
    /export/nodes path in app.py."""
    assert _flax_post_offenders() == []


def test_leopard_memory_lines_parse_without_the_dimm_prefix():
    """Quanta Leopard macinv (edam et7b1, 2026-11-06): slot is 'A0', not 'DIMM A0'."""
    from flax_post import inventory
    p = inventory.parse("Memory: 32 GB, A0, _Node0_Channel0_Dimm0, 2667 MT/s, Micron, 1BA7B55B, 36ASF4G72PZ-2G6D1\n"
                        "Memory: 64 GB, DIMM A1, _Node0_Channel1_Dimm0, 2666 MT/s, Micron, 1DDE31E1, 72ASS8G72LZ-2G6D2\n")
    assert [(m["slot"], m["serial"], m["size"]) for m in p["memory"]] == [
        ("A0", "1BA7B55B", "32 GB"), ("DIMM A1", "1DDE31E1", "64 GB")]
