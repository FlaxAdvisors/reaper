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


def _tpm2_main(monkeypatch, capsys, startup, prop=0x201):
    """main() on the TPM 2.0 path with get_capability stubbed: PERMANENT=0,
    no persistent handles, STARTUP_CLEAR=startup (None = the read fails)."""
    import importlib.util
    import struct
    import tempfile
    spec = importlib.util.spec_from_file_location("tpm_state_t", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    dev = os.path.join(tempfile.mkdtemp(), "tpm0dev")
    open(dev, "wb").close()
    monkeypatch.setattr(m, "DEV", dev)
    monkeypatch.setattr(m, "SYSFS", _sysfs(tempfile.mkdtemp(), version="2"))

    def cap(f, c, p, n):
        if c == 1:
            return struct.pack(">BII", 0, 1, 0)
        if p == 0x201:
            if startup is None:
                raise IOError("TPM rc=0x1c4")
            return struct.pack(">BIIII", 0, 6, 1, prop, startup)
        return struct.pack(">BIIII", 0, 6, 1, 0x200, 0)
    monkeypatch.setattr(m, "get_capability", cap)
    assert m.main() == 0
    return capsys.readouterr().out.strip()


def test_tpm2_reports_hierarchy_enables(monkeypatch, capsys):
    out = _tpm2_main(monkeypatch, capsys, 0x80000006)
    assert out.startswith("owned=0 perm=0x00000000 ")
    assert out.endswith(" persistent=0 startup=0x80000006 shEnable=1 ehEnable=1 phEnable=0")


def test_tpm2_disabled_reads_as_sh_eh_off(monkeypatch, capsys):
    out = _tpm2_main(monkeypatch, capsys, 0x80000000)
    assert "shEnable=0 ehEnable=0" in out


def test_tpm2_startup_read_failure_or_wrong_property_omits_the_fields(monkeypatch, capsys):
    assert "startup=" not in _tpm2_main(monkeypatch, capsys, None)
    assert "startup=" not in _tpm2_main(monkeypatch, capsys, 0x6, prop=0x202)
