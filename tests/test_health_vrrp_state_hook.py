"""health hook vrrp_state.sh (FOLLOWUP-2026-11-08-health-noise §1): keepalived's notify
scripts write the state on a transition (with $1); a 30 s timer refreshes it from the
host (no $1): the VIP on an interface = MASTER, keepalived active without it = BACKUP,
keepalived not running = STOPPED (2, like FAULT). Runs the real script with ip and
systemctl stubbed on PATH."""
import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "roles/apply_health_metrics_hooks/files/health_hooks/vrrp_state.sh"


def run(tmp_path, *args, vip_here=False, ka="active"):
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    addr = "2: eth0    inet 192.168.88.10/24 scope global secondary eth0\n" if vip_here else ""
    (bin_ / "ip").write_text("#!/bin/sh\nprintf '%s' '"
                             + "1: eth0    inet 192.168.88.21/24 scope global eth0\n" + addr + "'\n")
    (bin_ / "systemctl").write_text(f"#!/bin/sh\necho {ka}\n[ {ka} = active ]\n")
    for f in bin_.iterdir():
        f.chmod(0o755)
    (tmp_path / "site.env").write_text("SITE=eindhoven\nMGMT_VIP=192.168.88.10\n")
    out = tmp_path / "tf"
    out.mkdir(exist_ok=True)
    env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", TEXTFILE_DIR=str(out),
               SITE_ENV=str(tmp_path / "site.env"))
    subprocess.run(["bash", str(HOOK), *args], env=env, check=True)
    return (out / "vrrp_state.prom").read_text().splitlines()[-1]


def test_vip_on_an_interface_is_master(tmp_path):
    assert run(tmp_path, vip_here=True).endswith('state="MASTER"} 1')


def test_keepalived_without_the_vip_is_backup(tmp_path):
    assert run(tmp_path).endswith('state="BACKUP"} 0')


def test_keepalived_not_running_is_stopped(tmp_path):
    assert run(tmp_path, ka="inactive").endswith('state="STOPPED"} 2')


def test_a_notify_state_is_written_as_given(tmp_path):
    assert run(tmp_path, "FAULT", vip_here=True).endswith('state="FAULT"} 2')


def test_the_hook_has_a_timer_and_the_health_component_lists_it():
    d = yaml.safe_load((ROOT / "roles/apply_health_metrics_hooks/defaults/main.yml").read_text())
    assert "vrrp_state" in {t["name"] for t in d["apply_health_metrics_hooks_timers"]}
    comps = yaml.safe_load((ROOT / "files/reaper/components.yml").read_text())
    assert "health-hook@vrrp_state" in comps["health"]["timers"]
