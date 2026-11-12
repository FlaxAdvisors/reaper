"""Post BIOS manifest from per-site inventory (reaper fw spec 2026-11-11 §6a).

The rendered /etc/flax/bios-firmware-versions.json must stay byte-identical to the
static file it replaces (af7f196), for every site, except for what `reaper fw <site>
bios <family> <tar>` moves: the `target` and `bin_url` of each entry linked to that
triage family, which then name the site's host_fw_platforms target and tar
(eindhoven moved to TPO_P27F, 1181dd9)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytest.importorskip("reapercli")
from reapercli.lib.fwentry import SHARE_URL  # noqa: E402
from reapercli.lib.inventory import _Loader  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SITES = ("eindhoven", "lavergne", "braintree")
TEMPLATE = ROOT / "roles" / "apply_flax_post" / "templates" / "bios-firmware-versions.json.j2"


def site_vars(site):
    text = (ROOT / "inventories" / site / "group_vars" / "all" / "vars.yml").read_text()
    return yaml.load(text, Loader=_Loader)


def site_var(site):
    return site_vars(site)["post_bios_fw_platforms"]


def todays_file() -> bytes:
    return subprocess.run(["git", "-C", str(ROOT), "show",
                           "af7f196:roles/apply_flax_post/files/bios-firmware-versions.json"],
                          capture_output=True, check=True).stdout


def render(var, tmp_path) -> bytes:
    dest = tmp_path / "out.json"
    subprocess.run(["ansible", "localhost", "-c", "local", "-m", "ansible.builtin.template",
                    "-a", f"src={TEMPLATE} dest={dest}",
                    "-e", json.dumps({"post_bios_fw_platforms": var})],
                   capture_output=True, check=True, cwd=str(ROOT),
                   env={"PATH": str(Path(shutil.which("ansible")).parent) + ":/usr/bin:/bin",
                        "ANSIBLE_CONFIG": str(ROOT / "ansible.cfg"), "HOME": str(Path.home())})
    return dest.read_bytes()


def expected_file(site) -> bytes:
    """Today's file with each linked entry's target/bin_url following the site's
    triage family, in to_nice_json(indent=2) layout."""
    base = todays_file()
    assert (json.dumps(json.loads(base), indent=2) + "\n").encode() == base   # layout self-check
    v = site_vars(site)
    link = {e["family"]: e["host_fw_family"] for e in v["post_bios_fw_platforms"] if e.get("host_fw_family")}
    out = json.loads(base)
    for e in out:
        if e["family"] in link:
            tri = v["host_fw_platforms"][link[e["family"]]]
            e["target"] = tri["target_version"]
            e["bin_url"] = f"{SHARE_URL}/{tri['flash']['artifact']}"
    return (json.dumps(out, indent=2) + "\n").encode()


@pytest.mark.skipif(shutil.which("ansible") is None, reason="ansible not on PATH (reaper shell)")
@pytest.mark.parametrize("site", SITES)
def test_render_is_todays_file_with_the_sites_moved_target(site, tmp_path):
    assert render(site_var(site), tmp_path) == expected_file(site)


@pytest.mark.parametrize("site", SITES)
def test_every_site_links_tiogapass_bios(site):
    linked = [e for e in site_var(site) if e.get("host_fw_family")]
    assert [(e["family"], e["host_fw_family"]) for e in linked] == [("tiogapass", "tiogapass-bios")]
    assert all(e.get("updatable") is False for e in site_var(site) if not e.get("host_fw_family"))
