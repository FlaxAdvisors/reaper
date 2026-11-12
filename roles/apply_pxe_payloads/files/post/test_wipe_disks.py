"""wipe_disks.sh against stub lsblk/wipefs/blkdiscard/efibootmgr driven by a
JSON fixture. Each stub is one python dispatcher that reads and updates the
fixture, so a wipe is visible to the verify pass the way it is on a node."""
import json
import os
import stat
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "wipe_disks.sh")

STUB = r'''#!/usr/bin/env python3
import json, os, sys
cmd = os.path.basename(sys.argv[0]); a = sys.argv[1:]
F = os.environ["FAKE_STATE"]
st = json.load(open(F))
def save(): json.dump(st, open(F, "w"))
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(cmd + " " + " ".join(a) + "\n")
disks = st["disks"]
def disk_of(dev):
    for d in disks:
        if dev == d["name"] or dev in [p["name"] for p in d.get("parts", [])]:
            return d
if cmd == "lsblk":
    dev = a[-1] if a and a[-1].startswith("/dev/") else None
    if a[:2] == ["-dnpo", "NAME,TYPE"]:
        for d in disks: print(d["name"], "disk")
    elif a[0] == "-dno":
        col = a[1]; d = disk_of(dev)
        if d is None: sys.exit(1)
        if dev == d["name"]:
            print(d.get(col, ""))
        else:
            print([p for p in d["parts"] if p["name"] == dev][0].get(col, ""))
    elif a[:2] == ["-nro", "MOUNTPOINT"]:
        for m in disk_of(dev).get("mounts", []): print(m)
    elif a[:2] == ["-nrpo", "NAME,TYPE"]:
        d = disk_of(dev); print(d["name"], "disk")
        for p in d.get("parts", []): print(p["name"], "part")
    elif a[:2] == ["-nrpo", "TYPE"]:
        d = disk_of(dev); print("disk")
        for p in d.get("parts", []): print("part")
    else:
        print("NAME TYPE (listing)")
elif cmd == "wipefs":
    if a[0] == "-n":
        d = disk_of(a[1])
        if a[1] == d["name"] and (d.get("sigs") or d.get("parts")):
            print("DEVICE OFFSET TYPE UUID LABEL")
            for s in d.get("sigs", ["gpt"]): print(a[1], "0x200", s)
    elif a[0] == "-a":
        d = disk_of(a[-1])
        if a[-1] == d["name"] and not st.get("wipefs_fails"):
            d["parts"] = []; d["sigs"] = []; save()
elif cmd == "blkdiscard":
    sys.exit(st.get("discard_rc", 0))
elif cmd == "blockdev":
    if a[0] == "--getsize64": print(256 * 1024 * 1024 * 1024)
elif cmd == "efibootmgr":
    if a[:1] == ["-v"] or not a:
        for e in st.get("boot", []): print(e)
'''


def _setup(tmp, disks, boot=None, **extra):
    bindir = os.path.join(tmp, "bin")
    os.makedirs(bindir)
    disp = os.path.join(bindir, "_stub")
    with open(disp, "w") as f:
        f.write(STUB)
    os.chmod(disp, stat.S_IRWXU)
    for c in ("lsblk", "wipefs", "blkdiscard", "blockdev", "efibootmgr", "udevadm", "dd"):
        os.symlink(disp, os.path.join(bindir, c))
    state = os.path.join(tmp, "state.json")
    json.dump(dict(disks=disks, boot=boot or [], **extra), open(state, "w"))
    efi = os.path.join(tmp, "efi")
    os.makedirs(efi)
    return bindir, state, efi


def _run(tmp, bindir, state, efi, dry=False):
    log = os.path.join(tmp, "calls.log")
    open(log, "w").close()
    env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"],
               FAKE_STATE=state, FAKE_LOG=log, WIPE_EFI_DIR=efi,
               WIPE_SYS_BLOCK=os.path.join(tmp, "sys_block"))
    if dry:
        env["WIPE_DRYRUN"] = "1"
    p = subprocess.run(["bash", SCRIPT], env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, universal_newlines=True, timeout=60)
    return p, open(log).read().splitlines()


def _nvme(**kw):
    d = {"name": "/dev/nvme0n1", "TRAN": "nvme", "RM": "0", "MODEL": "WD PC SN810",
         "SERIAL": "222815805096", "SIZE": "238.5G", "DISC-MAX": "2T",
         "parts": [{"name": "/dev/nvme0n1p1", "PARTUUID": "070615b9-0a02-4a01-81ff-c047b9078207"},
                   {"name": "/dev/nvme0n1p3", "PARTUUID": "ccee5f79-cc79-4781-8d23-ac86c8f1fb6a"}],
         "sigs": ["gpt", "PMBR"]}
    d.update(kw)
    return d


WINBOOT = ("Boot0000* Windows Boot Manager\tHD(1,GPT,070615b9-0a02-4a01-81ff-c047b9078207,"
           "0x800,0xaf000)/File(\\EFI\\MICROSOFT\\BOOT\\BOOTMGFW.EFI)")
PXE = "Boot0003* UEFI: PXE IPv4 Mellanox Network Adapter - B8:59:9F:AC:19:E4\tPciRoot(0x3)/MAC(b8599fac19e4,1)"


def test_dirty_nvme_is_wiped_discarded_and_verified(tmp_path):
    b, s, e = _setup(str(tmp_path), [_nvme()])
    p, calls = _run(str(tmp_path), b, s, e)
    assert p.returncode == 0
    assert "WIPED /dev/nvme0n1 model=WD_PC_SN810 serial=222815805096" in p.stdout
    assert "method=wipefs+discard verified=yes" in p.stdout
    assert "wipefs -a -q /dev/nvme0n1p1" in calls and "wipefs -a -q /dev/nvme0n1" in calls
    assert "blkdiscard -f /dev/nvme0n1" in calls


def test_no_crypto_sanitize_or_ata_security_ever(tmp_path):
    b, s, e = _setup(str(tmp_path), [_nvme()])
    p, calls = _run(str(tmp_path), b, s, e)
    text = open(SCRIPT).read()
    for banned in ("nvme sanitize", "nvme format", "--ses", "hdparm", "security-erase", "sg_sanitize"):
        assert banned not in text, banned
    assert not any(c.startswith(("nvme", "hdparm", "sg_")) for c in calls)


def test_clean_disk_is_left_alone(tmp_path):
    clean = {"name": "/dev/sda", "TRAN": "sata", "RM": "0", "MODEL": "SSD PLUS",
             "SERIAL": "x", "SIZE": "232G", "DISC-MAX": "2G", "parts": [], "sigs": []}
    b, s, e = _setup(str(tmp_path), [clean])
    p, calls = _run(str(tmp_path), b, s, e)
    assert "CLEAN /dev/sda" in p.stdout and "WIPED" not in p.stdout
    assert not any(c.startswith(("wipefs -a", "blkdiscard", "dd ")) for c in calls)


def test_presence_proof_the_clean_check_is_not_vacuous(tmp_path):
    # Same disk as above with one signature: it must now be wiped.
    dirty = {"name": "/dev/sda", "TRAN": "sata", "RM": "0", "MODEL": "SSD PLUS",
             "SERIAL": "x", "SIZE": "232G", "DISC-MAX": "2G", "parts": [], "sigs": ["ntfs"]}
    b, s, e = _setup(str(tmp_path), [dirty])
    p, calls = _run(str(tmp_path), b, s, e)
    assert "WIPED /dev/sda" in p.stdout and "wipefs -a -q /dev/sda" in calls


def test_usb_removable_mounted_and_unknown_transport_are_skipped(tmp_path):
    disks = [_nvme(name="/dev/sdb", TRAN="usb", parts=[], sigs=["vfat"]),
             _nvme(name="/dev/sdc", TRAN="sata", RM="1", parts=[], sigs=["vfat"]),
             _nvme(name="/dev/sdd", TRAN="sata", mounts=["/run/initramfs/live"], parts=[], sigs=["iso9660"]),
             _nvme(name="/dev/loop0", TRAN="", parts=[], sigs=["squashfs"])]
    b, s, e = _setup(str(tmp_path), disks)
    p, calls = _run(str(tmp_path), b, s, e)
    assert "SKIP  /dev/sdb" in p.stdout and "reason=transport:usb" in p.stdout
    assert "reason=removable" in p.stdout and "reason=mounted" in p.stdout
    assert "reason=transport:none" in p.stdout
    assert "WIPED" not in p.stdout
    assert not any(c.startswith(("wipefs -a", "blkdiscard", "dd ")) for c in calls)


def test_disk_held_by_md_or_lvm_is_skipped(tmp_path):
    b, s, e = _setup(str(tmp_path), [_nvme()])
    holders = tmp_path / "sys_block" / "nvme0n1" / "nvme0n1p3" / "holders"
    holders.mkdir(parents=True)
    (holders / "dm-0").write_text("")
    p, calls = _run(str(tmp_path), b, s, e)
    assert "reason=in-use-by-md/lvm/dm" in p.stdout
    assert not any(c.startswith("wipefs -a") for c in calls)


def test_no_discard_falls_back_to_zeroing_head_and_tail(tmp_path):
    b, s, e = _setup(str(tmp_path), [_nvme(**{"DISC-MAX": "0B"})])
    p, calls = _run(str(tmp_path), b, s, e)
    assert "method=wipefs+zero" in p.stdout
    dd = [c for c in calls if c.startswith("dd ")]
    assert len(dd) == 2 and "seek=" in dd[1]
    assert not any(c.startswith("blkdiscard") for c in calls)


def test_failed_discard_still_zeroes(tmp_path):
    b, s, e = _setup(str(tmp_path), [_nvme()], discard_rc=1)
    p, calls = _run(str(tmp_path), b, s, e)
    assert "method=wipefs+zero" in p.stdout
    assert len([c for c in calls if c.startswith("dd ")]) == 2


def test_verify_reports_NO_when_signatures_survive(tmp_path):
    b, s, e = _setup(str(tmp_path), [_nvme()], wipefs_fails=True)
    p, _ = _run(str(tmp_path), b, s, e)
    assert "verified=NO" in p.stdout and p.returncode == 0


def test_boot_entry_into_a_wiped_disk_is_removed_pxe_kept(tmp_path):
    b, s, e = _setup(str(tmp_path), [_nvme()], boot=[WINBOOT, PXE])
    p, calls = _run(str(tmp_path), b, s, e)
    assert "BOOTENTRY-REMOVED Boot0000 Windows Boot Manager" in p.stdout
    assert "efibootmgr -q -b 0000 -B" in calls
    assert not any("-b 0003" in c for c in calls)


def test_dry_run_changes_nothing(tmp_path):
    b, s, e = _setup(str(tmp_path), [_nvme()], boot=[WINBOOT, PXE])
    p, calls = _run(str(tmp_path), b, s, e, dry=True)
    assert "verified=dry-run" in p.stdout and "dry-run: blkdiscard -f /dev/nvme0n1" in p.stdout
    assert not any(c.startswith(("wipefs -a", "blkdiscard", "dd ")) or " -B" in c for c in calls)
