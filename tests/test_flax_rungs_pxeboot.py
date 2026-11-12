"""pxeboot: hostname-<name>.ipxe assembled exactly like ghost sethostnameboot writes it.

The equality test runs sethostnameboot's own heredoc (cut from the read-only copy in
fixtures/rackpost/pxe/, with `date` pinned) against the real bang-gouda templates."""
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from flax_rungs import pxeboot

FIX = Path(__file__).parent / "fixtures" / "rackpost" / "pxe"
STAMP = "20261030.120000"


def _created():
    return time.mktime(time.strptime(STAMP, "%Y%m%d.%H%M%S"))


def _ghost_file(tmp_path, hostname, platform, act) -> str:
    """Run the `cat<<EOF ... EOF` block of the real sethostnameboot."""
    src = (FIX / "sethostnameboot").read_text()
    m = re.search(r"^    cat<<EOF>\$\{dstdir\}/hostname-\$\{hostname\}\.ipxe\n.*?^EOF\n", src,
                  re.S | re.M)
    assert m, "sethostnameboot heredoc not found (ghost changed its layout?)"
    dst = tmp_path / "ghost"
    dst.mkdir()
    script = (f"date() {{ echo {STAMP}; }}\n"
              f"srcdir={FIX}\ndstdir={dst}\nhostname={hostname}\nplatform={platform}\n"
              f"act={act}\n" + m.group(0))
    subprocess.run(["bash", "-c", script], check=True)
    return (dst / f"hostname-{hostname}.ipxe").read_text()


def _pxe_dir(tmp_path) -> Path:
    d = tmp_path / "ipxe"
    (d / "templates").mkdir(parents=True)
    for f in ("wiwynn-tp-hmc.part", "ubuntu-24.04.ipxe", "bios.ipxe"):
        shutil.copy(FIX / f, d / "templates" / f)
    return d


def test_assembly_equals_sethostnameboot(tmp_path):
    d = _pxe_dir(tmp_path)
    r = pxeboot.write("rr101-c37", pxe_dir=str(d), created=_created())
    assert r.verdict == "pass", r.detail
    ours = (d / "hostname-rr101-c37.ipxe").read_text()
    assert ours == _ghost_file(tmp_path, "rr101-c37", "wiwynn-tp-hmc", "ubuntu-24.04")
    assert r.artifacts == {"hostname-rr101-c37.ipxe": ours}


def test_file_content_and_mode(tmp_path):
    d = _pxe_dir(tmp_path)
    pxeboot.write("rr101-l05", pxe_dir=str(d), created=_created())
    p = d / "hostname-rr101-l05.ipxe"
    text = p.read_text()
    assert text.startswith("#!ipxe\n\n# rr101-l05 wiwynn-tp-hmc to do ubuntu-24.04 created "
                           f"{STAMP}\n\n# apply the wiwynn-tp settings\n")
    assert text.count("#!ipxe") == 1                 # the act file's own line is dropped
    assert "set node-setting wiwynn-tp\n" in text
    assert "set menu-default menu-ubuntu\nset submenu-default C_24.04\n" in text
    assert text.endswith("chain --replace --autofree ${boot-url}\n")
    assert oct(p.stat().st_mode & 0o777) == "0o664"
    assert not [f for f in os.listdir(d) if f.endswith(".tmp")]


def test_rewrite_replaces_and_remove_is_idempotent(tmp_path):
    d = _pxe_dir(tmp_path)
    pxeboot.write("n1", pxe_dir=str(d), created=1)
    pxeboot.write("n1", pxe_dir=str(d), created=_created())
    assert STAMP in (d / "hostname-n1.ipxe").read_text()
    assert pxeboot.exists("n1", pxe_dir=str(d))
    assert pxeboot.remove("n1", pxe_dir=str(d)) == (True, None)
    assert not pxeboot.exists("n1", pxe_dir=str(d))
    assert pxeboot.remove("n1", pxe_dir=str(d)) == (True, None)   # none there: still gone


def test_remove_failure_is_reported(tmp_path, monkeypatch):
    d = _pxe_dir(tmp_path)
    pxeboot.write("n1", pxe_dir=str(d))

    def boom(self):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(Path, "unlink", boom)
    gone, err = pxeboot.remove("n1", pxe_dir=str(d))
    assert gone is False and "Permission denied" in err


def test_missing_template_writes_nothing(tmp_path):
    d = _pxe_dir(tmp_path)
    (d / "templates" / "ubuntu-24.04.ipxe").unlink()
    r = pxeboot.write("n1", pxe_dir=str(d))
    assert r.verdict == "fail" and "iPXE template" in r.detail["error"]
    assert os.listdir(d) == ["templates"]


@pytest.mark.parametrize("bad", ["../etc/x", "a/b", "", "-x", "a b", None])
def test_bad_hostnames_refused(tmp_path, bad):
    d = _pxe_dir(tmp_path)
    r = pxeboot.write(bad, pxe_dir=str(d))
    assert r.verdict == "fail" and "bad hostname" in r.detail["error"]
    assert pxeboot.remove(bad, pxe_dir=str(d))[0] is False


def test_bad_template_names_refused(tmp_path):
    d = _pxe_dir(tmp_path)
    r = pxeboot.write("n1", platform="../x", pxe_dir=str(d))
    assert r.verdict == "fail" and "bad template" in r.detail["error"]
