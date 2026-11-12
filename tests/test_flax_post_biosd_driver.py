import pytest

from flax_post.biosd import driver

_ENTRY = {"target": "TPC_P26C", "afulnx_url": "http://bang/x/afulnx_TP",
          "bin_url": "http://bang/x/TPC_P26C.zip", "bin_path": "TPC_P26C/TPC_P26C.bin",
          "flags": ["/X","/B","/P","/K","/N","/ME","/REBOOT"]}

def test_parse_bios_version():
    # `dmidecode -t bios` "Version" field is the OEM BIOS version (TPC_P26C).
    out = "BIOS Information\n\tVendor: American Megatrends\n\tVersion: TPC_P26C\n\tRelease Date: 10/15/2020\n"
    assert driver.parse_bios_version(out) == "TPC_P26C"
    assert driver.parse_bios_version("no version here") is None

def test_check_script_reads_bios_version_via_dmidecode_not_afulnx():
    # afulnx has no version display (its ROMID is a board family, e.g. TPCH);
    # the version check is dmidecode-only -- no afulnx/insmod/curl needed.
    s = driver.check_script(_ENTRY)
    assert "dmidecode -t bios" in s
    assert "afulnx" not in s and "insmod" not in s and "TPC_P26C.zip" not in s

def test_flash_script_pulls_zip_unzips_on_target_and_flashes():
    s = driver.flash_script(_ENTRY)
    assert "insmod /export/share/amifldrv/amifldrv_mod.ko" in s  # flash needs the module
    assert "http://bang/x/afulnx_TP" in s                        # + afulnx
    assert "http://bang/x/TPC_P26C.zip" in s and "unzip" in s   # fetch + unzip on the DUT
    assert "TPC_P26C/TPC_P26C.bin" in s                          # afulnx the in-zip bin
    assert "/X /B /P /K /N /ME /REBOOT" in s

def test_flash_script_raises_on_missing_required_field():
    # A malformed manifest entry missing any required field must raise
    # KeyError -- NOT silently build a garbage/no-op afulnx command with an
    # empty URL/path (the pre-fix `.get(..., '')` behavior).
    for missing in ("afulnx_url", "bin_url", "bin_path", "flags"):
        entry = {k: v for k, v in _ENTRY.items() if k != missing}
        with pytest.raises(KeyError):
            driver.flash_script(entry)


def test_run_over_ssh_runs_under_sudo(monkeypatch):
    # afulnx/insmod/dmidecode need root; the ISO user has passwordless sudo, so
    # the piped script must run under `sudo -n bash -s`.
    captured = {}
    class _P:
        returncode = 0; stdout = "ok"; stderr = ""
    def _fake_run(cmd, **kw):
        captured["cmd"] = cmd; captured["input"] = kw.get("input"); return _P()
    monkeypatch.setattr(driver.subprocess, "run", _fake_run)
    rc, out = driver.run_over_ssh("flax", "pw", "172.25.10.1", "dmidecode -t system")
    assert rc == 0
    assert captured["cmd"][-1] == "sudo -n bash -s"
    assert "flax@172.25.10.1" in captured["cmd"]


def test_run_over_ssh_has_keepalive(monkeypatch):
    # ServerAlive* bounds a mid-command link drop to ~10s (firmware resets that
    # sever the SSH's own NIC) instead of hanging on dead TCP.
    captured = {}
    class _P:
        returncode = 0; stdout = ""; stderr = ""
    monkeypatch.setattr(driver.subprocess, "run",
                        lambda cmd, **kw: (captured.update(cmd=cmd) or _P()))
    driver.run_over_ssh("flax", "pw", "1.2.3.4", "echo hi")
    assert "ServerAliveInterval=5" in captured["cmd"] and "ServerAliveCountMax=2" in captured["cmd"]
