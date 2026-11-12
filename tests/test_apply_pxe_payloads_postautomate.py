import pathlib
import re

BOOT = pathlib.Path(__file__).resolve().parents[1] / "roles/apply_pxe_payloads/templates/v/boot.ipxe.j2"


def test_boot_ipxe_has_postautomate_label():
    t = BOOT.read_text()
    # the menu offers it, and its label sets the action then boots the live image
    assert re.search(r"^item .* postautomate ", t, re.M)
    label = t[t.index("\n:postautomate\n"):]
    label = label[:label.index("\n:", 1)]
    assert label.split("\n")[2:4] == ["set postaction postautomate", "goto live"]
