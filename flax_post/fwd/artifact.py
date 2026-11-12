"""Fetch the operator-placed firmware .mtd.tar over HTTP.

Artifacts live under <BMC_FW_SHARE_BASE>/<manifest flash.artifact> (e.g.
http://bang/export/share/SXC-Lab_OCP-Updates/OpenBMC/fx-tp-...mtd.tar). We stream
the bytes into memory and hand them to RedfishClient.post_flash.
"""
import urllib.error
import urllib.request


def fetch(share_base: str, rel_path: str, timeout: int = 60) -> bytes:
    url = share_base.rstrip("/") + "/" + rel_path.lstrip("/")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RuntimeError("artifact fetch failed (%s): %s" % (url, e)) from e
