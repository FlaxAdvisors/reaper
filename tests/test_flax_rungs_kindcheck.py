"""Lab-rack kind checks on an installed OS (check only; plan 2026-11-02 Task 8)."""
import pytest
pytest.importorskip("flax_rungs")
from flax_rungs.kindcheck import check, BB_PCI_ARGS

# .53's /proc/cmdline today (the Big Basin head, no pci= args)
CMD_53 = "BOOT_IMAGE=/vmlinuz-6.8.0-147-generic root=/dev/mapper/ubuntu--vg-ubuntu--lv ro console=tty0 console=ttyS1,57600n8"
CMD_69 = ("BOOT_IMAGE=/vmlinuz-6.8.0-147-generic root=/dev/mapper/ubuntu--vg-ubuntu--lv ro "
          "pci=assign-busses,realloc,hpbussize=0x40,hpmmioprefsize=256G console=tty0 console=ttyS1,57600n8")
GOOD = {"cmdline": CMD_69,
        "pkgs": "hi  cuda-toolkit-12-9 12.9.1-1\nhi  nvidia-driver-580-server 580.95.05-0ubuntu1\n",
        "gpus": "GPU 0: Tesla V100-SXM2-32GB\n" * 8,
        "mmio": "MMIO High Size\nOptions =[02]256G\n         *[04]1024G\n"}


def runner(answers):
    def run(script):
        for k, v in answers.items():
            if f"# {k}" in script:
                return 0, v
        return 1, ""
    return run


def test_bb_head_passes_with_everything_in_place():
    r = check("bb-head", runner(GOOD), {"gpu": 8})
    assert r.verdict == "pass", r.detail


def test_kindcheck_bb_head_pci_args_missing():
    r = check("bb-head", runner({**GOOD, "cmdline": CMD_53}), {"gpu": 8})
    assert r.verdict == "fail" and "pci=" in r.detail["headline"]


def test_kindcheck_bb_head_gpu_count_and_mmio():
    r = check("bb-head", runner({**GOOD, "gpus": "GPU 0: Tesla V100-SXM2-32GB\n" * 4}), {"gpu": 8})
    assert r.verdict == "fail" and "4/8" in r.detail["headline"]
    r = check("bb-head", runner({**GOOD, "mmio": "MMIO High Size\nOptions =*[02]256G\n"}), {"gpu": 8})
    assert r.verdict == "fail" and "MMIO" in r.detail["headline"]


def test_kindcheck_lightning_rejects_pci_args():
    assert check("lightning-head", runner({"cmdline": CMD_69}), {}).verdict == "fail"
    assert check("lightning-head", runner({"cmdline": CMD_53}), {}).verdict == "pass"


def test_other_kinds_have_no_kind_setup():
    r = check("compute-tp", runner({}), {})
    assert r.verdict == "pass" and r.detail["headline"] == "no kind setup"
    assert BB_PCI_ARGS == "pci=assign-busses,realloc,hpbussize=0x40,hpmmioprefsize=256G"


def test_mmio_script_fetches_scelnx_from_the_share_bundle(tmp_path):
    """The real mmio script, run by bash: SCELNX_64 comes out of the tpm_enabler
    bundle curl fetches (an installed OS has no /opt/flax/bin/SCELNX_64)."""
    import io, os, subprocess, tarfile
    from flax_rungs.kindcheck import _CMD, SCE_TGZ_URL, mmio_size
    assert "tpm_enabler.v4.tgz" in SCE_TGZ_URL and "/opt/flax/bin" not in _CMD["mmio"]
    fake = b'#!/bin/bash\n[ "$1 $2" = "/o /s" ] && printf "MMIO High Size\\nOptions =[02]256G\\n *[04]1024G\\n" > "$3"\n'
    tgz = tmp_path / "bundle.tgz"
    with tarfile.open(tgz, "w:gz") as t:
        ti = tarfile.TarInfo("SCELNX_64"); ti.size = len(fake); ti.mode = 0o755
        t.addfile(ti, io.BytesIO(fake))
    bin_ = tmp_path / "bin"; bin_.mkdir()
    (bin_ / "curl").write_text(f'#!/bin/bash\n[ "${{@: -1}}" = "{SCE_TGZ_URL}" ] && cat {tgz}\n')
    (bin_ / "curl").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"}
    p = subprocess.run(["bash", "-c", _CMD["mmio"]], capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    assert mmio_size(p.stdout) == "1024G"
    (bin_ / "curl").write_text("#!/bin/bash\nexit 22\n")  # share unreachable -> not read
    p = subprocess.run(["bash", "-c", _CMD["mmio"]], capture_output=True, text=True, env=env)
    assert p.returncode != 0 and mmio_size(p.stdout) is None
