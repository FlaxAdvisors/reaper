"""tpm_state.py on a TPM 1.2 (operator 2026-10-27): ownership from the kernel's
TPM 1.2 sysfs attributes; TPM 2.0 keeps the raw GetCapability path."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "tpm_state.py")


def _sysfs(tmp, version="1", attrs=None, sub=""):
    d = os.path.join(str(tmp), "tpm0")
    os.makedirs(os.path.join(d, sub), exist_ok=True)
    with open(os.path.join(d, "tpm_version_major"), "w") as f:
        f.write(version + "\n")
    for name, val in (attrs or {}).items():
        with open(os.path.join(d, sub, name), "w") as f:
            f.write(val + "\n")
    return d


def _run(sysfs):
    env = dict(os.environ, TPM_SYSFS=sysfs, TPM_DEV=os.path.join(sysfs, "no-dev"))
    return subprocess.run([sys.executable, SCRIPT], env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, universal_newlines=True, timeout=30)


def test_tpm12_owned_from_sysfs():
    import tempfile
    p = _run(_sysfs(tempfile.mkdtemp(), attrs={"owned": "1", "enabled": "1", "active": "1"}))
    assert p.returncode == 0 and p.stdout.strip() == "owned=1 tpm=1.2 enabled=1 active=1"


def test_tpm12_unowned_older_kernel_layout_under_device():
    import tempfile
    p = _run(_sysfs(tempfile.mkdtemp(), sub="device",
                    attrs={"owned": "0", "enabled": "1", "active": "0"}))
    assert p.returncode == 0 and p.stdout.strip() == "owned=0 tpm=1.2 enabled=1 active=0"


def test_tpm12_without_an_owned_attribute_is_unreadable():
    import tempfile
    p = _run(_sysfs(tempfile.mkdtemp(), attrs={"enabled": "1"}))
    assert p.returncode == 2 and p.stdout.startswith("owned=? tpm=1.2 error=")


def test_tpm2_keeps_the_raw_path():
    import tempfile
    p = _run(_sysfs(tempfile.mkdtemp(), version="2"))
    assert p.returncode == 2 and p.stdout.startswith("owned=? error=") and "tpm=1.2" not in p.stdout
