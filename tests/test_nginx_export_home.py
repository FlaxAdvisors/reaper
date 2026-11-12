"""nginx serves /export/ with autoindex on every operator path (mgmt, tailscale, public
IP). /export/home holds the users' homes on a bang whose /home was moved to the
export disk (bang-fiesta, 2026-11-09): it must 404, never list or serve a file."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "roles/apply_bang_services/templates/nginx-pxe.conf.j2"
TASKS = ROOT / "roles/apply_bang_services/tasks/main.yml"


def test_export_home_is_a_404_that_wins_over_the_export_tree():
    text = TPL.read_text()
    m = re.search(r"location \^~ /export/home/ \{([^}]*)\}", text)
    assert m and "return 404" in m.group(1)
    assert "alias" not in m.group(1) and "autoindex" not in m.group(1)


def test_the_nginx_tasks_ship_on_their_own_tag():
    tasks = yaml.safe_load(TASKS.read_text())
    tagged = {t["name"] for t in tasks if "nginx" in (t.get("tags") or [])}
    assert {"Deploy nginx PXE vhost", "Deploy nginx lab-UI vhosts (triage/post/view on :80)",
            "Enable + start nginx"} <= tagged
    comps = yaml.safe_load((ROOT / "files/reaper/components.yml").read_text())
    assert "nginx" in comps["bang_services"]["tags"]
