# tests/test_apply_keepalived_notify_master.py
"""notify_master.sh must survive a cold boot where keepalived wins MASTER
before docker.service is up.

2026-10-28 bang-rhythm reboot: MASTER at 18:26:25, dockerd's first start lost a
race with the firewall (ip6tables-restore) and exited at 18:26:33, so
`systemctl restart kea-dhcp4` failed with result 'dependency'. notify_master
logged it and moved on; kea-dhcp4 is disabled (only notify_master starts it),
so the lab had no DHCPv4 until a hand start at 18:29.

The script is rendered and RUN with stub systemctl/logger/sleep/docker on PATH.
"""
import os
import pathlib
import subprocess

import jinja2

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CTX = dict(inventory_hostname="bang-rhythm", bang_dhcp_server="kea",
           apply_keepalived_compose_stacks=[],
           bang_mgmt_cidr="192.168.88.10/24", bang_mgmt_iface="mgmt")

# Stub ip: the mgmt VIP is on the interface unless $STUB/lost_vip exists.
IP = r'''#!/bin/bash
echo "ip $*" >> "$STUB/calls"
echo "2: mgmt: <BROADCAST,MULTICAST,UP> mtu 1500"
echo "    inet 192.168.88.21/24 scope global mgmt"
[ -e "$STUB/lost_vip" ] || echo "    inet 192.168.88.10/24 scope global secondary mgmt"
'''

# Stub systemctl. State lives in files under $STUB:
#   docker_inactive_polls  -> `is-active docker` answers inactive this many times
#   fail_<unit>            -> `restart <unit>` fails this many times first
#   missing_<unit>         -> `cat <unit>` fails (unit not installed)
# Every invocation is appended to $STUB/calls.
SYSTEMCTL = r'''#!/bin/bash
echo "$*" >> "$STUB/calls"
case "$1" in
  is-active)
    u=${@: -1}
    f="$STUB/${u}_inactive_polls"
    n=$(cat "$f" 2>/dev/null || echo 0)
    if [ "$n" -gt 0 ]; then echo $((n-1)) > "$f"; exit 3; fi
    exit 0 ;;
  cat)
    [ -e "$STUB/missing_$2" ] && exit 1
    exit 0 ;;
  restart)
    f="$STUB/fail_$2"
    n=$(cat "$f" 2>/dev/null || echo 0)
    if [ "$n" -gt 0 ]; then echo $((n-1)) > "$f"; exit 1; fi
    echo "$2" >> "$STUB/started"
    exit 0 ;;
esac
exit 0
'''


def _render(ctx):
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(f"{REPO}/roles/apply_keepalived/templates"),
        trim_blocks=True, lstrip_blocks=True, undefined=jinja2.StrictUndefined)
    return env.get_template("notify_master.sh.j2").render(**ctx)


def _run(tmp_path, docker_inactive_polls=0, fail=None, missing=(), lost_vip=False):
    stub = tmp_path / "stub"
    bindir = tmp_path / "bin"
    stub.mkdir()
    bindir.mkdir()
    for name, body in [("systemctl", SYSTEMCTL),
                       ("logger", '#!/bin/bash\necho "$*" >> "$STUB/log"\n'),
                       ("sleep", '#!/bin/bash\necho "$1" >> "$STUB/slept"\n'),
                       ("docker", "#!/bin/bash\nexit 0\n"),
                       ("ip", IP)]:
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)
    (stub / "docker_inactive_polls").write_text(str(docker_inactive_polls))
    for unit, n in (fail or {}).items():
        (stub / f"fail_{unit}").write_text(str(n))
    for unit in missing:
        (stub / f"missing_{unit}").write_text("")
    if lost_vip:
        (stub / "lost_vip").write_text("")
    script = tmp_path / "notify_master.sh"
    script.write_text(_render(CTX))
    env = dict(os.environ, PATH=f"{bindir}:/usr/bin:/bin", STUB=str(stub))
    r = subprocess.run(["bash", str(script)], env=env, capture_output=True,
                       text=True, timeout=30)

    def read(name):
        p = stub / name
        return p.read_text().splitlines() if p.exists() else []
    return r, read


def test_renders_valid_bash(tmp_path):
    p = tmp_path / "n.sh"
    p.write_text(_render(CTX))
    assert subprocess.run(["bash", "-n", str(p)]).returncode == 0


def test_kea_dhcp4_retried_after_dependency_failure(tmp_path):
    # The 2026-10-28 shape: the first restart fails (docker not up yet).
    r, read = _run(tmp_path, fail={"kea-dhcp4": 1})
    assert r.returncode == 0, r.stderr
    calls = read("calls")
    assert calls.count("restart kea-dhcp4") == 2
    assert "kea-dhcp4" in read("started")


def test_waits_for_docker_before_restarting_kea(tmp_path):
    r, read = _run(tmp_path, docker_inactive_polls=3)
    assert r.returncode == 0, r.stderr
    calls = read("calls")
    polls = [i for i, c in enumerate(calls) if c.startswith("is-active") and c.endswith("docker")]
    first_kea = calls.index("restart kea-dhcp4")
    assert len(polls) == 4                      # 3 inactive, then active
    assert polls[-1] < first_kea
    assert read("started")[0] == "kea-dhcp4"


def test_docker_wait_is_bounded(tmp_path):
    # Docker never comes up: give up waiting, log it, still try the units.
    r, read = _run(tmp_path, docker_inactive_polls=10_000)
    assert r.returncode == 0, r.stderr
    polls = [c for c in read("calls") if c.startswith("is-active") and c.endswith("docker")]
    assert len(polls) <= 120
    assert any("docker" in line and "not active" in line for line in read("log"))
    assert "restart kea-dhcp4" in read("calls")


def test_retries_are_bounded(tmp_path):
    r, read = _run(tmp_path, fail={"kea-dhcp4": 100})
    assert r.returncode == 0, r.stderr
    n = read("calls").count("restart kea-dhcp4")
    assert 2 <= n <= 6
    assert any("kea-dhcp4" in line and "failed" in line for line in read("log"))
    # One unit failing does not stop the rest of the promotion.
    assert "flax-observe" in read("started")


def test_missing_unit_is_skipped_without_retrying(tmp_path):
    r, read = _run(tmp_path, missing=("flax-post-sol",))
    assert r.returncode == 0, r.stderr
    assert "restart flax-post-sol" not in read("calls")
    assert read("slept") == []                  # no retry back-off for an absent unit


def test_every_promoted_unit_still_restarted(tmp_path):
    r, read = _run(tmp_path)
    started = read("started")
    for u in ["kea-dhcp4", "kea-dhcp6", "radvd", "flax-switch-sense",
              "flax-observe", "flax-classify", "flax-reconcile", "flax-post",
              "flax-post-nginx"]:
        assert u in started, u
    # Kea before the pipeline, as before.
    assert started.index("kea-dhcp4") < started.index("flax-observe")


def test_no_restart_once_the_vip_is_gone(tmp_path):
    # Demoted while waiting for docker: notify_backup has stopped the units;
    # starting Kea here would put two DHCP servers on the lab.
    r, read = _run(tmp_path, lost_vip=True)
    assert r.returncode == 0, r.stderr
    assert read("started") == []
    assert any("VIP" in line for line in read("log"))
