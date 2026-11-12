"""boot.ipxe.j2 passes flaxwipe=1 to the live image on triage VLANs only
(post.sh -> wipe_disks.sh on the inventory action), overridable per vlan."""
import pathlib

import jinja2
import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]

ROLE = (REPO / "roles/apply_pxe_payloads")
DEFAULTS = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())


def _bool(v):   # ansible's | bool
    return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")


def _render(vlan):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROLE / "templates" / "v")))
    env.filters["bool"] = _bool
    ctx = {k: v for k, v in DEFAULTS.items() if k.startswith("apply_pxe_payloads_pxe_")}
    return env.get_template("boot.ipxe.j2").render(vlans=[vlan], current_vid=vlan["vid"], **ctx)


def _flag(text):
    line = [l for l in text.splitlines() if l.startswith("isset ${flaxwipe}")]
    assert len(line) == 1, line
    return line[0].split()[-1]


def test_triage_vlan_wipes_by_default():
    assert _flag(_render({"vid": 17, "family": "tiogapass", "phase": "triage"})) == "1"


def test_post_vlan_does_not_wipe():
    assert _flag(_render({"vid": 25, "family": "tiogapass", "phase": "post"})) == "0"


def test_per_vlan_override_turns_it_off_and_on():
    assert _flag(_render({"vid": 17, "family": "tiogapass", "phase": "triage", "pxe": {"wipe": False}})) == "0"
    assert _flag(_render({"vid": 25, "family": "tiogapass", "phase": "post", "pxe": {"wipe": True}})) == "1"


def test_flag_reaches_the_live_kernel_cmdline():
    t = _render({"vid": 17, "family": "tiogapass", "phase": "triage"})
    live = [l for l in t.splitlines() if l.startswith("set live ")][0]
    assert "postaction=${postaction} flaxwipe=${flaxwipe}" in live
