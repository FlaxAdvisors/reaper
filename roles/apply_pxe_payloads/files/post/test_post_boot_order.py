"""post.sh order: nothing may (re)boot a triage node before its disks are wiped,
and every reboot post itself makes first asks the BMC for a one-time UEFI PXE boot.

Why: recycled disks carry a previous owner's OS, and with disk ahead of PXE in
the BIOS boot order a reboot boots it and hangs (et10b4, 2026-10-26). The BIOS
gate power-off, a NIC flash and the Leopard BIOS flash all come before the
inventory, so the wipe has to come before them too.
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))


def _src(name):
    with open(os.path.join(HERE, name)) as f:
        return f.read()


def _line(text, pattern):
    for n, line in enumerate(text.splitlines(), 1):
        if re.search(pattern, line) and not line.lstrip().startswith("#"):
            return n
    raise AssertionError("no line matches %r" % pattern)


def test_wipe_runs_before_the_first_power_off_or_reboot():
    post = _src("post.sh")
    wipe = _line(post, r"\./wipe_disks\.sh")
    for first in (r"\./bios_gate_report\.sh", r"\./update_mellanox\.sh",
                  r"\./update_quanta_leopard\.sh"):
        assert wipe < _line(post, first), first


def test_tpm_clear_still_follows_the_inventory():
    post = _src("post.sh")
    assert _line(post, r"smartctl --all \$dev") < _line(post, r"\./tpm_clear\.sh")


def _pxe_before(text, reboot):
    r = _line(text, reboot)
    pxe = [n for n, l in enumerate(text.splitlines(), 1)
           if re.search(r"pxe_next_boot|bootdev pxe options=efiboot", l)
           and not l.lstrip().startswith("#") and n < r]
    assert pxe, "no PXE bootdev before %r" % reboot


def test_post_sets_pxe_before_its_tpm_reboot():
    _pxe_before(_src("post.sh"), r"systemctl reboot")


def test_leopard_flash_sets_pxe_before_its_reboot():
    _pxe_before(_src("update_quanta_leopard.sh"), r"shutdown -r now")
