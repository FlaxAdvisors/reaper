"""Per-host iPXE boot file: /srv/pxe/ipxe/hostname-<hostname>.ipxe.

menu.ipxe chains `${menu-dir}hostname-${hostname}.ipxe` when DHCP handed the node a
hostname (the Kea reservation's), before the mac-/product- files and the default
menu; every /srv/pxe/v<vid>/ipxe is a symlink to /srv/pxe/ipxe, so one file serves
every VLAN. The file is laid out exactly like ghost `sethostnameboot <act>` writes it
(its heredoc, reproduced, not called: sethostnameboot needs an order dir's nic0.txt
and 00_slack.txt):

    #!ipxe
    <blank>
    # <hostname> <platform> to do <act> created YYYYmmdd.HHMMSS
    <blank>
    <templates/<platform>.part>
    <blank>
    <templates/<act>.ipxe without its #!ipxe line>

each template with its trailing newlines stripped (shell `$(cat ...)`). The platform
part sets node-setting / com-setting; the act file sets menu-default + submenu-default
and chains boot.ipxe, whose menu then loads the installer. Removing the file returns
the node to the default menu, so a later power-on cannot reinstall."""
import os
import re
import tempfile
import time
from pathlib import Path

from .result import RungResult

PXE_DIR = "/srv/pxe/ipxe"
PLATFORM = "wiwynn-tp-hmc"      # Tioga Pass (SV7220G3), ttyS0 115200
ACT = "ubuntu-24.04"
# = order_reservations._HOSTNAME (what Kea hands out); it also keeps the name a
# plain file name in PXE_DIR
_HOSTNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_TEMPLATE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def path(hostname: str, pxe_dir: str = PXE_DIR) -> Path:
    if not isinstance(hostname, str) or not _HOSTNAME.fullmatch(hostname):
        raise ValueError(f"bad hostname: {hostname!r}")
    return Path(pxe_dir) / f"hostname-{hostname}.ipxe"


def _strip_nl(text: str) -> str:
    return text.rstrip("\n")             # shell $(...) drops every trailing newline


def render(hostname: str, part: str, act_text: str, *, platform: str = PLATFORM,
           act: str = ACT, created: float | None = None) -> str:
    """sethostnameboot's file for `hostname`, from the two template texts."""
    stamp = time.strftime("%Y%m%d.%H%M%S", time.localtime(created))
    body = "\n".join(ln for ln in act_text.split("\n") if not ln.startswith("#!ipxe"))
    return (f"#!ipxe\n\n# {hostname} {platform} to do {act} created {stamp}\n\n"
            f"{_strip_nl(part)}\n\n{_strip_nl(body)}\n")


def write(hostname: str, *, platform: str = PLATFORM, act: str = ACT,
          pxe_dir: str = PXE_DIR, created: float | None = None) -> RungResult:
    """Write (atomically replace) the hostname file; artifacts = its text. Fails, writing
    nothing, when a template is missing."""
    try:
        dst = path(hostname, pxe_dir)
    except ValueError as e:
        return RungResult("fail", {"error": str(e)})
    for name in (platform, act):
        if not _TEMPLATE.fullmatch(name):
            return RungResult("fail", {"error": f"bad template name: {name!r}"})
    tdir = Path(pxe_dir) / "templates"
    try:
        part = (tdir / f"{platform}.part").read_text()
        act_text = (tdir / f"{act}.ipxe").read_text()
    except OSError as e:
        return RungResult("fail", {"error": f"iPXE template: {e}"})
    text = render(hostname, part, act_text, platform=platform, act=act, created=created)
    fd, tmp = tempfile.mkstemp(dir=pxe_dir, prefix=".hostname-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o664)             # like sethostnameboot's: group users may edit
        os.replace(tmp, dst)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return RungResult("fail", {"error": f"write {dst}: {e}"})
    return RungResult("pass", {"path": str(dst), "platform": platform, "act": act,
                               "bytes": len(text.encode())},
                      {f"hostname-{hostname}.ipxe": text})


def remove(hostname: str, *, pxe_dir: str = PXE_DIR) -> tuple[bool, str | None]:
    """(gone, error): gone is True when no hostname file is left (removed, or none was
    there); error names an unlink that failed."""
    try:
        p = path(hostname, pxe_dir)
    except ValueError as e:
        return False, str(e)
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        return False, f"remove {p}: {e}"
    return True, None


def exists(hostname: str, *, pxe_dir: str = PXE_DIR) -> bool:
    try:
        return path(hostname, pxe_dir).exists()
    except ValueError:
        return False
