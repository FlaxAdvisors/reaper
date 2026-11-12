"""flax_rungs.bootorder: read efibootmgr, pick the installed system's ubuntu entry
(spec 2026-11-11-deploy-firstboot-boot-order §2)."""
from flax_rungs import bootorder

ESP = "c04dd539-9412-4637-8d67-7a19457bdd0a"

# LVQ et7b4 `efibootmgr -v` after the failed firstboot (2026-11-11 15:04), plus the stale
# entry a previous install left for a since-wiped disk (seen in its 11-10 bootorder.txt)
ET7B4 = f"""BootCurrent: 0003
Timeout: 7 seconds
BootOrder: 0003,0006,0007,0008,0009,000C,000B,000A
Boot0000* Ubuntu\tHD(1,GPT,{ESP},0x800,0x219800)/File(\\EFI\\ubuntu\\shimx64.efi)
      dp: 04 01 2a 00 01 00 00 00
Boot0001  UEFI: Built-in EFI Shell\tVenMedia(5023b95c-db26-429b-a648-bd47664c8012)0000424f
Boot0003* UEFI: PXE IPv4 Mellanox Network Adapter - 98:03:9B:87:5D:56\tPciRoot(0x3)/Pci(0x0,0x0)/Pci(0x0,0x0)/MAC(98039b875d56,1)/IPv4(0.0.0.00.0.0.0,0,0)0000424f
Boot0005* ubuntu Boot Manager\tHD(1,GPT,fbfe9088-fe45-49eb-a347-28df36942bdd,0x800,0x219800)/File(\\EFI\\UBUNTU\\SHIMX64.EFI)
Boot0006* UEFI: HTTP IPv4 Mellanox Network Adapter - 94:40:C9:7F:61:D0\tPciRoot(0x1)/Pci(0x2,0x0)/Pci(0x0,0x0)/MAC(9440c97f61d0,1)/IPv4(0.0.0.00.0.0.0,0,0)/Uri()0000424f
Boot0007* UEFI: PXE IPv4 Mellanox Network Adapter - 94:40:C9:7F:61:D0\tPciRoot(0x1)/Pci(0x2,0x0)/Pci(0x0,0x0)/MAC(9440c97f61d0,1)/IPv4(0.0.0.00.0.0.0,0,0)0000424f
Boot0008* UEFI: HTTP IPv4 Mellanox Network Adapter - 94:40:C9:7F:61:D1\tPciRoot(0x1)/Pci(0x2,0x0)/Pci(0x0,0x1)/MAC(9440c97f61d1,1)/IPv4(0.0.0.00.0.0.0,0,0)/Uri()0000424f
Boot0009* UEFI: PXE IPv4 Mellanox Network Adapter - 94:40:C9:7F:61:D1\tPciRoot(0x1)/Pci(0x2,0x0)/Pci(0x0,0x1)/MAC(9440c97f61d1,1)/IPv4(0.0.0.00.0.0.0,0,0)0000424f
Boot000A  UEFI: Built-in EFI Shell\tVenMedia(5023b95c-db26-429b-a648-bd47664c8012)0000424f
Boot000B* ubuntu Boot Manager\tHD(1,GPT,{ESP},0x800,0x219800)/File(\\EFI\\Ubuntu\\shimx64.efi) File(.)
Boot000C* UEFI: HTTP IPv4 Mellanox Network Adapter - 98:03:9B:87:5D:56\tPciRoot(0x3)/Pci(0x0,0x0)/Pci(0x0,0x0)/MAC(98039b875d56,1)/IPv4(0.0.0.00.0.0.0,0,0)/Uri()0000424f
"""


def test_parse_reads_current_order_entries():
    p = bootorder.parse(ET7B4)
    assert p["current"] == "0003"
    assert p["order"] == ["0003", "0006", "0007", "0008", "0009", "000C", "000B", "000A"]
    assert "000B" in p["entries"] and "ubuntu Boot Manager" in p["entries"]["000B"]
    assert "dp:" not in "".join(p["entries"].values())


def test_ubuntu_entry_lowest_on_this_host():
    assert bootorder.ubuntu_entry(bootorder.parse(ET7B4)["entries"], {ESP}) == "0000"
    assert bootorder.ubuntu_entry(bootorder.parse(ET7B4)["entries"], {ESP.upper()}) == "0000"


def test_ubuntu_entry_never_a_wiped_disk():
    entries = bootorder.parse(ET7B4)["entries"]
    assert bootorder.ubuntu_entry({k: v for k, v in entries.items() if k != "0000" and k != "000B"},
                                  {ESP}) is None
    assert bootorder.ubuntu_entry(entries, {"aaaaaaaa-0000-0000-0000-000000000000"}) is None
    assert bootorder.ubuntu_entry(entries, {"fbfe9088-fe45-49eb-a347-28df36942bdd"}) == "0005"


def test_ubuntu_first():
    assert bootorder.ubuntu_first(["0003", "0000", "000A"], "0000") == ["0000", "0003", "000A"]
    assert bootorder.ubuntu_first(["0003", "000A"], "0000") == ["0000", "0003", "000A"]


def test_order_line():
    assert bootorder.order_line(ET7B4) == \
        "BootCurrent: 0003 · BootOrder: 0003,0006,0007,0008,0009,000C,000B,000A"
    assert bootorder.order_line("BootOrder: 0000,0003\n") == "BootOrder: 0000,0003"
    assert bootorder.order_line("") == "not read"
